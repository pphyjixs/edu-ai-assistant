"""报告批改 Worker 的行为测试（``docs/api-contract.md`` 9.11）。

覆盖：

- 非法模型输出（评分项缺失、证据为幻觉）**不写部分批改结果**；
- 旧运行令牌与过期租约的执行者都不能回写；
- 课程在批改期间归档 → 任务 ``CANCELLED``、提交 ``FAILED``、不留下批改草稿；
- 批改始终按**提交固定的**评分版本，教师之后修改 Rubric 不影响历史批改；
- 并发领取互斥，每轮 ``attempts`` 只递增一次；
- **模型调用期间不持有数据库事务**：批改过程中另一条连接可以正常写入该提交行。

模型为本地假 HTTP 服务（``httpx.MockTransport``），真实模型效果未验收。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.config import Settings
from app.core.time import utc_now
from app.modules.grading import worker
from app.modules.grading.grading_ai import (
    RubricItemSnapshot,
    ValidatedGrade,
    ValidatedGradeItem,
)
from app.storage.deps import get_storage_dep
from tests.integration.test_grading_api import (
    GRADE_URL,
    REPORT_LINES,
    REVIEW_URL,
    _auth,
    _grading_settings,
    _open_assignment,
    _submit_report,
    build_pdf,
)
from tests.integration.test_materials_api import DOCX_MIME, build_docx
from tests.storage_fake import FakeStorage


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage: FakeStorage) -> Iterator[TestClient]:
    app = pg_app()
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    with TestClient(app) as test_client:
        yield test_client


def _submit_and_trigger(client, fake_storage) -> tuple[str, str, str]:
    """返回 ``(任务 ID, 教师令牌, 提交 ID)``；提交已生成并触发批改。"""
    _course, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])
    triggered = client.post(
        f"/api/v1/submissions/{detail['id']}/grade", headers=_auth(teacher)
    )
    assert triggered.status_code == 202, triggered.text
    return assignment["id"], teacher, detail["id"]


def _run_worker(
    session_factory,
    settings: Settings,
    storage: FakeStorage,
    handler,
    *,
    max_jobs: int = 1,
) -> int:
    return asyncio.run(
        worker.run_pending_batch(
            session_factory,
            settings=settings,
            storage=storage,
            ai_client_factory=lambda: httpx.Client(
                transport=httpx.MockTransport(handler), timeout=10.0
            ),
            max_jobs=max_jobs,
        )
    )


def _item_ids_from_prompt(prompt: str) -> list[str]:
    return [
        line.split("=", 1)[1].split("｜")[0]
        for line in prompt.splitlines()
        if line.startswith("- rubric_item_id=")
    ]


def _model_handler(payload_builder):
    """按提示词里的 rubric_item_id 产出结果的假模型 handler。"""

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content.decode())["messages"][-1]["content"]
        body = payload_builder(_item_ids_from_prompt(prompt))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}]},
        )

    return handler


def _valid_payload(item_ids: list[str]) -> dict:
    return {
        "summary": "整体完成度较好",
        "items": [
            {
                "rubric_item_id": item_id,
                "score": 10,
                "comment": "覆盖较完整",
                "evidence_quote": REPORT_LINES[0],
                "location_start": 1,
                "location_end": 1,
                "error_type": "",
                "improvement_suggestion": "补充异常流程",
            }
            for item_id in item_ids
        ],
    }


def _review_counts(engine: Engine) -> tuple[int, int]:
    with engine.connect() as connection:
        reviews = int(
            connection.execute(text("SELECT count(*) FROM grade_reviews")).scalar_one()
        )
        items = int(
            connection.execute(text("SELECT count(*) FROM grade_items")).scalar_one()
        )
    return reviews, items


def _attempt_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text("SELECT count(*) FROM submission_grade_attempts")
            ).scalar_one()
        )


def _submission_status(engine: Engine, submission_id: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text("SELECT status FROM submissions WHERE id = CAST(:id AS uuid)"),
                {"id": submission_id},
            ).scalar_one()
        )


def _job(engine: Engine) -> dict:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT id, status, attempts, progress, run_token FROM jobs"
                " WHERE type = 'SUBMISSION_GRADE'"
            )
        ).mappings().one()
    return dict(row)


def _awarded_rubric_item_ids(engine: Engine) -> list[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text('SELECT rubric_item_id FROM grade_items ORDER BY "order"')
        ).scalars().all()
    return [str(row) for row in rows]


# --------------------------------------------------------------------------- #
# 成功路径与审计
# --------------------------------------------------------------------------- #
def test_worker_grades_and_records_attempt_once(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    _assignment_id, _teacher, submission_id = _submit_and_trigger(client, fake_storage)

    processed = _run_worker(
        pg_session_factory,
        _grading_settings(make_settings),
        fake_storage,
        _model_handler(_valid_payload),
    )

    assert processed == 1
    assert _submission_status(pg_sync_engine, submission_id) == "REVIEW_REQUIRED"
    assert _review_counts(pg_sync_engine) == (1, 2)
    assert _attempt_count(pg_sync_engine) == 1
    job = _job(pg_sync_engine)
    assert job["status"] == "SUCCEEDED"
    assert job["attempts"] == 1
    assert job["progress"] == 100


def test_worker_grades_docx_report_with_paragraph_evidence(
    client, fake_storage, pg_session_factory, make_settings
) -> None:
    """DOCX 报告批改：批改成功且证据来源类型为 ``DOCX_PARAGRAPH``（契约 9.11）。

    PDF 路径已有等价覆盖（``test_grading_api.py``）；这里补 DOCX 的端到端：
    提取文本、证据核对与段落定位都按 DOCX 规则执行。
    """
    _course, teacher, student, assignment = _open_assignment(client)
    body = build_docx([(REPORT_LINES[0], None), (REPORT_LINES[1], None)])
    detail = _submit_report(
        client,
        fake_storage,
        student,
        assignment["id"],
        body=body,
        filename="report.docx",
        content_type=DOCX_MIME,
    )
    triggered = client.post(
        GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher)
    )
    assert triggered.status_code == 202, triggered.text

    assert _run_worker(
        pg_session_factory,
        _grading_settings(make_settings),
        fake_storage,
        _model_handler(_valid_payload),
    ) == 1

    review = client.get(
        REVIEW_URL.format(submission_id=detail["id"]), headers=_auth(teacher)
    )
    assert review.status_code == 200, review.text
    body_json = review.json()
    assert body_json["items"]
    for item in body_json["items"]:
        # 证据摘录来自报告原文，来源类型按 MIME 由服务端确定为 DOCX 段落
        assert item["evidence_quote"] in (REPORT_LINES[0], REPORT_LINES[1])
        assert item["evidence_source_type"] == "DOCX_PARAGRAPH"
        assert item["evidence_location_start"] >= 1
        assert item["evidence_location_end"] >= item["evidence_location_start"]


def test_worker_uses_submission_fixed_rubric_version(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """提交固定版本 1；教师在批改前把 Rubric 改成版本 2，批改仍按版本 1 评分。"""
    assignment_id, teacher, _submission_id = _submit_and_trigger(client, fake_storage)

    updated = client.patch(
        f"/api/v1/assignments/{assignment_id}",
        json={
            "rubric_items": [
                {"title": "新规则 A", "description": "", "max_score": 70, "order": 1},
                {"title": "新规则 B", "description": "", "max_score": 30, "order": 2},
            ]
        },
        headers=_auth(teacher),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["rubric_version"] == 2

    assert _run_worker(
        pg_session_factory,
        _grading_settings(make_settings),
        fake_storage,
        _model_handler(_valid_payload),
    ) == 1

    with pg_sync_engine.connect() as connection:
        version_1_items = {
            str(row)
            for row in connection.execute(
                text(
                    "SELECT ai.id FROM assignment_rubric_items ai"
                    " JOIN assignment_rubric_versions av ON av.id = ai.rubric_version_id"
                    " WHERE av.assignment_id = CAST(:id AS uuid) AND av.version = 1"
                ),
                {"id": assignment_id},
            ).scalars().all()
        }
        version_2_items = {
            str(row)
            for row in connection.execute(
                text(
                    "SELECT ai.id FROM assignment_rubric_items ai"
                    " JOIN assignment_rubric_versions av ON av.id = ai.rubric_version_id"
                    " WHERE av.assignment_id = CAST(:id AS uuid) AND av.version = 2"
                ),
                {"id": assignment_id},
            ).scalars().all()
        }

    awarded = set(_awarded_rubric_item_ids(pg_sync_engine))
    assert awarded == version_1_items
    assert awarded.isdisjoint(version_2_items)


def test_worker_does_not_hold_transaction_during_model_call(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine, pg_test_url
) -> None:
    """模型调用期间不得持有数据库事务。

    假模型在被调用的瞬间用**另一条同步连接**更新该提交行，并把 ``statement_timeout``
    设为 2 秒：若 Worker 此时仍持有提交行锁（或长时间开着写事务），这条 UPDATE 会
    超时失败，测试即失败。
    """
    from tests import pg_support

    # psycopg2 只认标准 DSN，去掉 SQLAlchemy 的驱动后缀
    sync_url = pg_support.to_sync(pg_test_url).replace(
        "postgresql+psycopg2://", "postgresql://"
    )
    _assignment_id, _teacher, submission_id = _submit_and_trigger(client, fake_storage)
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import psycopg2

        try:
            connection = psycopg2.connect(sync_url)
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = 2000")
                cursor.execute(
                    "UPDATE submissions SET updated_at = now()"
                    " WHERE id = CAST(%s AS uuid)",
                    (submission_id,),
                )
            connection.close()
            observed["wrote"] = True
        except Exception as exc:  # noqa: BLE001 - 只要失败就说明持有了事务
            observed["wrote"] = False
            observed["error"] = f"{type(exc).__name__}"

        prompt = json.loads(request.content.decode())["messages"][-1]["content"]
        body = _valid_payload(_item_ids_from_prompt(prompt))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(body, ensure_ascii=False)}}]},
        )

    assert _run_worker(
        pg_session_factory,
        _grading_settings(make_settings),
        fake_storage,
        handler,
    ) == 1

    assert observed.get("wrote") is True, (
        f"模型调用期间仍持有数据库事务：{observed.get('error')}"
    )
    assert _submission_status(pg_sync_engine, submission_id) == "REVIEW_REQUIRED"


# --------------------------------------------------------------------------- #
# 非法输出：不留部分结果
# --------------------------------------------------------------------------- #
def test_worker_leaves_no_partial_review_on_fabricated_evidence(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """证据摘录不在报告原文中：整次失败，**不写任何**批改结果。"""

    def fabricated(item_ids: list[str]) -> dict:
        payload = _valid_payload(item_ids)
        payload["items"][0]["evidence_quote"] = "this sentence is not in the report"
        return payload

    _assignment_id, _teacher, submission_id = _submit_and_trigger(client, fake_storage)

    assert _run_worker(
        pg_session_factory,
        _grading_settings(make_settings),
        fake_storage,
        _model_handler(fabricated),
    ) == 1

    assert _submission_status(pg_sync_engine, submission_id) == "FAILED"
    assert _review_counts(pg_sync_engine) == (0, 0)
    assert _job(pg_sync_engine)["status"] == "FAILED"
    assert _attempt_count(pg_sync_engine) == 1


def test_worker_leaves_no_partial_review_on_missing_rubric_item(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """模型漏掉一个评分项：整次失败，不写部分批改结果。"""

    def only_first(item_ids: list[str]) -> dict:
        return _valid_payload(item_ids[:1])

    _assignment_id, _teacher, submission_id = _submit_and_trigger(client, fake_storage)

    assert _run_worker(
        pg_session_factory,
        _grading_settings(make_settings),
        fake_storage,
        _model_handler(only_first),
    ) == 1

    assert _submission_status(pg_sync_engine, submission_id) == "FAILED"
    assert _review_counts(pg_sync_engine) == (0, 0)


def test_worker_fails_safely_when_report_has_no_text(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """扫描版 / 无文本 PDF：安全失败，不调用模型也不写批改结果。"""
    blank = build_pdf([" "])
    _course, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"], body=blank)
    client.post(f"/api/v1/submissions/{detail['id']}/grade", headers=_auth(teacher))

    assert _run_worker(
        pg_session_factory,
        _grading_settings(make_settings),
        fake_storage,
        _model_handler(_valid_payload),
    ) == 1

    assert _submission_status(pg_sync_engine, detail["id"]) == "FAILED"
    assert _review_counts(pg_sync_engine) == (0, 0)


# --------------------------------------------------------------------------- #
# 旧执行者不能回写
# --------------------------------------------------------------------------- #
def test_stale_run_token_cannot_write_back(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """运行令牌被重置（教师重试）后，旧执行者的回写必须放弃。"""
    _assignment_id, _teacher, submission_id = _submit_and_trigger(client, fake_storage)

    async def claim() -> object:
        return await worker.claim_next(
            pg_session_factory, now=utc_now(), lease_seconds=300
        )

    claimed = asyncio.run(claim())
    assert claimed is not None

    # 模拟"教师重试"：任务被重置，运行令牌换新（旧执行者的令牌失效）
    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET status = 'PENDING', run_token = 'new-token',"
                " lease_expires_at = NULL WHERE type = 'SUBMISSION_GRADE'"
            )
        )

    async def write_back() -> bool:
        rubric = [
            RubricItemSnapshot(
                rubric_item_id=uuid.uuid4(),
                order=1,
                title="需求完整性",
                description="",
                max_score=Decimal("60"),
            )
        ]
        validated = ValidatedGrade(
            summary="旧执行者的结果",
            items=[
                ValidatedGradeItem(
                    rubric_item_id=rubric[0].rubric_item_id,
                    order=1,
                    score=Decimal("10"),
                    comment="旧执行者",
                    evidence_quote=REPORT_LINES[0],
                    evidence_source_type="PDF_PAGE",
                    evidence_location_start=1,
                    evidence_location_end=1,
                    error_type="",
                    improvement_suggestion="",
                )
            ],
        )
        return await worker._write_success(
            pg_session_factory,
            claimed=claimed,
            rubric=rubric,
            validated=validated,
            settings=_grading_settings(make_settings),
            duration_ms=1,
            now=utc_now(),
        )

    assert asyncio.run(write_back()) is False
    assert _review_counts(pg_sync_engine) == (0, 0)
    assert _submission_status(pg_sync_engine, submission_id) == "GRADING"
    assert _job(pg_sync_engine)["run_token"] == "new-token"


def test_expired_lease_cannot_write_back(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """租约已过期的旧执行者不得写失败结果（交给重试后的新执行）。"""
    _assignment_id, _teacher, submission_id = _submit_and_trigger(client, fake_storage)

    claimed = asyncio.run(
        worker.claim_next(pg_session_factory, now=utc_now(), lease_seconds=300)
    )
    assert claimed is not None

    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET lease_expires_at = now() - interval '1 hour'"
                " WHERE type = 'SUBMISSION_GRADE'"
            )
        )

    asyncio.run(
        worker._write_failure(
            pg_session_factory,
            claimed=claimed,
            message="旧执行者的失败",
            settings=_grading_settings(make_settings),
            duration_ms=1,
            cancelled=False,
            now=utc_now(),
        )
    )

    assert _submission_status(pg_sync_engine, submission_id) == "GRADING"
    assert _job(pg_sync_engine)["status"] == "RUNNING"
    assert _attempt_count(pg_sync_engine) == 0


def test_archived_course_cancels_job_without_draft(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """课程在批改期间归档：任务 CANCELLED、提交 FAILED，且不留下批改草稿。"""
    course_id, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])
    client.post(f"/api/v1/submissions/{detail['id']}/grade", headers=_auth(teacher))

    claimed = asyncio.run(
        worker.claim_next(pg_session_factory, now=utc_now(), lease_seconds=300)
    )
    assert claimed is not None

    archived = client.post(
        f"/api/v1/courses/{course_id}/archive", headers=_auth(teacher)
    )
    assert archived.status_code == 200, archived.text

    rubric = [
        RubricItemSnapshot(
            rubric_item_id=uuid.uuid4(),
            order=1,
            title="需求完整性",
            description="",
            max_score=Decimal("60"),
        )
    ]
    validated = ValidatedGrade(
        summary="结果",
        items=[
            ValidatedGradeItem(
                rubric_item_id=rubric[0].rubric_item_id,
                order=1,
                score=Decimal("10"),
                comment="说明",
                evidence_quote=REPORT_LINES[0],
                evidence_source_type="PDF_PAGE",
                evidence_location_start=1,
                evidence_location_end=1,
                error_type="",
                improvement_suggestion="",
            )
        ],
    )

    written = asyncio.run(
        worker._write_success(
            pg_session_factory,
            claimed=claimed,
            rubric=rubric,
            validated=validated,
            settings=_grading_settings(make_settings),
            duration_ms=1,
            now=utc_now(),
        )
    )

    assert written is False
    assert _review_counts(pg_sync_engine) == (0, 0)
    assert _submission_status(pg_sync_engine, detail["id"]) == "FAILED"
    assert _job(pg_sync_engine)["status"] == "CANCELLED"


# --------------------------------------------------------------------------- #
# 领取互斥
# --------------------------------------------------------------------------- #
def test_claim_is_exclusive_and_increments_attempts(
    client, fake_storage, pg_session_factory, pg_sync_engine
) -> None:
    _assignment_id, _teacher, _submission_id = _submit_and_trigger(client, fake_storage)

    first = asyncio.run(
        worker.claim_next(pg_session_factory, now=utc_now(), lease_seconds=300)
    )
    second = asyncio.run(
        worker.claim_next(pg_session_factory, now=utc_now(), lease_seconds=300)
    )

    assert first is not None
    assert second is None
    job = _job(pg_sync_engine)
    assert job["status"] == "RUNNING"
    assert job["attempts"] == 1

