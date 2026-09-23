"""提交与批改的协议回归（``docs/api-contract.md`` 9.2 / 9.3 的状态与不变量）。

覆盖修复计划要求的永久回归：

- 旧上传被新上传替代后，旧会话完成返回 ``UPLOAD_SUPERSEDED``；
- 新会话先完成、旧会话后完成（以及反向顺序）都只有新会话成功；
- 已开始批改后，旧上传不能改写 Submission 的文件、状态或固定评分版本；
- ``expired_at`` 会话即使对象重新出现也不能完成；
- 非目标唯一约束的 ``IntegrityError`` 不得转换为成功响应；
- 数据库不变量：两个已完成上传会话不能指向同一 Submission；非法证据来源/位置被拒绝。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.config import Settings
from app.modules.grading import service as grading_service
from app.modules.grading import worker
from app.storage.deps import get_storage_dep
from tests.integration.test_grading_api import (
    COMPLETE_URL,
    _auth,
    _grading_settings,
    _open_assignment,
    _simulate_put,
    _submit_report,
    fake_model_response,
    PDF_MIME,
    REPORT_BODY,
    REPORT_SHA256,
    REPORT_SIZE,
)
from tests.storage_fake import FakeStorage

UPLOADS_URL = "/api/v1/assignments/{assignment_id}/submissions/uploads"
GRADE_URL = "/api/v1/submissions/{submission_id}/grade"
REVIEW_URL = "/api/v1/submissions/{submission_id}/grade-review"


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage: FakeStorage) -> Iterator[TestClient]:
    app = pg_app()
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    with TestClient(app) as test_client:
        yield test_client


def _init(client: TestClient, token: str, assignment_id: str) -> dict:
    response = client.post(
        UPLOADS_URL.format(assignment_id=assignment_id),
        json={
            "filename": "report.pdf",
            "content_type": PDF_MIME,
            "size": REPORT_SIZE,
            "sha256": REPORT_SHA256,
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _complete(client: TestClient, token: str, assignment_id: str, upload_id: str):
    return client.post(
        COMPLETE_URL.format(assignment_id=assignment_id, upload_id=upload_id),
        headers=_auth(token),
    )


def _upload_and_complete(
    client: TestClient, fake: FakeStorage, token: str, assignment_id: str
) -> dict:
    init = _init(client, token, assignment_id)
    _simulate_put(client, fake, init)
    completed = _complete(client, token, assignment_id, init["upload_id"])
    assert completed.status_code == 201, completed.text
    return completed.json()


def _session_row(engine: Engine, upload_id: str) -> dict:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT id, submission_id, object_key, completed_at, expired_at,"
                " superseded_at FROM submission_upload_sessions"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": upload_id},
        ).mappings().one()
    return dict(row)


def _submission_row(engine: Engine, submission_id: str) -> dict:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT status, object_key, rubric_version_id, filename, size"
                " FROM submissions WHERE id = CAST(:id AS uuid)"
            ),
            {"id": submission_id},
        ).mappings().one()
    return dict(row)


# --------------------------------------------------------------------------- #
# 替代 / 过期 / 双会话完成
# --------------------------------------------------------------------------- #
def test_superseded_upload_cannot_complete(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """旧上传被新上传替代后，旧会话完成返回 ``UPLOAD_SUPERSEDED``。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    first = _init(client, student, assignment["id"])
    _simulate_put(client, fake_storage, first)
    second = _init(client, student, assignment["id"])
    _simulate_put(client, fake_storage, second)

    old = _complete(client, student, assignment["id"], first["upload_id"])

    assert old.status_code == 422, old.text
    assert old.json()["error"]["code"] == "UPLOAD_INVALID"
    assert old.json()["error"]["details"]["reason"] == "UPLOAD_SUPERSEDED"

    # 新会话仍然可以正常完成
    new = _complete(client, student, assignment["id"], second["upload_id"])
    assert new.status_code == 201, new.text


def test_new_session_wins_in_both_orders(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """新会话先完成、旧会话后完成：只有新会话成功（旧会话被替代）。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    old = _init(client, student, assignment["id"])
    _simulate_put(client, fake_storage, old)
    new = _init(client, student, assignment["id"])
    _simulate_put(client, fake_storage, new)

    first = _complete(client, student, assignment["id"], new["upload_id"])
    assert first.status_code == 201, first.text

    late = _complete(client, student, assignment["id"], old["upload_id"])
    assert late.status_code == 422, late.text
    assert late.json()["error"]["details"]["reason"] == "UPLOAD_SUPERSEDED"

    with pg_sync_engine.connect() as connection:
        completed = connection.execute(
            text(
                "SELECT count(*) FROM submission_upload_sessions"
                " WHERE completed_at IS NOT NULL"
            )
        ).scalar_one()
    assert completed == 1


def test_completing_session_of_already_submitted_submission_is_rejected(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """提交已由其他会话正式提交后，另一会话的完成请求被拒绝且无副作用。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    active = _init(client, student, assignment["id"])
    _simulate_put(client, fake_storage, active)
    stray = _init(client, student, assignment["id"])
    _simulate_put(client, fake_storage, stray)

    detail = _complete(
        client, student, assignment["id"], stray["upload_id"]
    ).json()
    assert detail["status"] == "SUBMITTED", detail

    response = _complete(client, student, assignment["id"], active["upload_id"])

    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["reason"] == "UPLOAD_SUPERSEDED"
    assert _submission_row(pg_sync_engine, detail["id"])["status"] == "SUBMITTED"


def test_stale_upload_cannot_rewrite_submission_after_grading_started(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine: Engine
) -> None:
    """已开始批改后，旧上传不能改写 Submission 的文件、状态或固定评分版本。"""
    _course, teacher, student, assignment = _open_assignment(client)
    active = _init(client, student, assignment["id"])
    _simulate_put(client, fake_storage, active)
    stale = _init(client, student, assignment["id"])
    _simulate_put(client, fake_storage, stale)

    detail = _complete(client, student, assignment["id"], stale["upload_id"]).json()

    # 教师触发批改：提交进入 GRADING
    client.post(GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher))
    before = _submission_row(pg_sync_engine, detail["id"])

    response = _complete(client, student, assignment["id"], active["upload_id"])

    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["reason"] == "UPLOAD_SUPERSEDED"
    after = _submission_row(pg_sync_engine, detail["id"])
    assert after["status"] == before["status"]
    assert after["object_key"] == before["object_key"]
    assert after["rubric_version_id"] == before["rubric_version_id"]
    assert after["filename"] == before["filename"]
    assert after["size"] == before["size"]


def test_expired_session_cannot_complete_even_if_object_reappears(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine: Engine
) -> None:
    """``expired_at`` 会话即使对象重新出现也不能完成。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    init = _init(client, student, assignment["id"])
    object_key = _simulate_put(client, fake_storage, init)

    # 清理：标记 expired_at 并删除对象
    async def run() -> int:
        async with pg_session_factory() as session:
            return await grading_service.cleanup_expired_submission_uploads(
                session,
                storage=fake_storage,
                settings=_cleanup_settings(make_settings),
            )

    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE submission_upload_sessions"
                " SET upload_url_expires_at = now() - interval '2 hours',"
                "     confirm_deadline_at = now() - interval '1 hour'"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": init["upload_id"]},
        )
    assert asyncio.run(run()) == 1
    assert object_key not in fake_storage.objects

    # 模拟晚到 PUT：对象重新出现
    fake_storage.store_object(
        object_key,
        size=REPORT_SIZE,
        content_type=PDF_MIME,
        sha256_hex=REPORT_SHA256,
        content=REPORT_BODY,
    )

    response = _complete(client, student, assignment["id"], init["upload_id"])

    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["reason"] == "UPLOAD_EXPIRED"


# --------------------------------------------------------------------------- #
# 数据库不变量
# --------------------------------------------------------------------------- #
def test_two_completed_sessions_cannot_share_submission(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """部分唯一索引：两个已完成上传会话不能指向同一 Submission。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    active = _init(client, student, assignment["id"])
    _simulate_put(client, fake_storage, active)
    stray = _init(client, student, assignment["id"])
    _simulate_put(client, fake_storage, stray)

    completed = _complete(client, student, assignment["id"], stray["upload_id"])
    assert completed.status_code == 201, completed.text

    with pytest.raises(Exception) as excinfo:
        with pg_sync_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE submission_upload_sessions"
                    " SET completed_at = now()"
                    " WHERE id = CAST(:id AS uuid)"
                ),
                {"id": active["upload_id"]},
            )

    assert "uq_submission_upload_sessions_submission_completed" in str(excinfo.value)


def test_invalid_evidence_values_are_rejected_by_database(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine: Engine
) -> None:
    """来源类型白名单与位置区间约束在数据库生效。"""
    _course, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])
    client.post(GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher))
    assert (
        asyncio.run(
            worker.run_pending_batch(
                pg_session_factory,
                settings=_grading_settings(make_settings),
                storage=fake_storage,
                ai_client_factory=lambda: __import__("httpx").Client(
                    transport=fake_model_response(), timeout=10.0
                ),
                max_jobs=1,
            )
        )
        == 1
    )

    with pytest.raises(Exception) as excinfo:
        with pg_sync_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE grade_items SET evidence_source_type = 'PPTX_SLIDE'"
                    " WHERE review_id IN"
                    " (SELECT id FROM grade_reviews WHERE submission_id = CAST(:sid AS uuid))"
                ),
                {"sid": detail["id"]},
            )
    assert "ck_grade_items" in str(excinfo.value)

    with pytest.raises(Exception) as excinfo:
        with pg_sync_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE grade_items SET evidence_location_end = 0"
                    " WHERE review_id IN"
                    " (SELECT id FROM grade_reviews WHERE submission_id = CAST(:sid AS uuid))"
                ),
                {"sid": detail["id"]},
            )
    assert "ck_grade_items" in str(excinfo.value)


def test_non_target_integrity_error_is_not_converted_to_success(
    client, fake_storage, pg_sync_engine: Engine, monkeypatch
) -> None:
    """非目标约束的 ``IntegrityError`` 不得转换为成功响应。

    在完成事务内注入一个**其他约束名**的 ``IntegrityError``：
    它不能被吞掉后伪装成 `201`/`409`，而必须原样抛出（500）。
    """
    from app.modules.grading import service as grading_service

    _course, _teacher, student, assignment = _open_assignment(client)
    init = _init(client, student, assignment["id"])
    _simulate_put(client, fake_storage, init)

    def boom(*args: object, **kwargs: object) -> None:
        raise _FakeIntegrityError("fk_somewhere_else")

    monkeypatch.setattr(grading_service.repo, "save_completion_snapshot", boom)

    response = _complete(client, student, assignment["id"], init["upload_id"])

    assert response.status_code >= 500, response.text
    body = response.json()["error"]
    assert body["code"] == "INTERNAL_ERROR"


class _FakeOrigin:
    """模拟驱动层异常的 ``constraint_name``。"""

    def __init__(self, name: str | None) -> None:
        self.constraint_name = name


class _FakeIntegrityError(Exception):
    """模拟 SQLAlchemy 的 ``IntegrityError``（仅供分类函数使用）。"""

    def __init__(self, name: str | None) -> None:
        super().__init__(f"integrity error ({name})")
        self.orig = _FakeOrigin(name)


def test_only_the_completion_unique_index_is_classified_as_idempotent() -> None:
    """只有"每份提交只能有一个完成会话"的约束被识别为幂等冲突。"""
    from app.modules.grading.service import (
        COMPLETION_UNIQUE_INDEX,
        _is_submission_completed_conflict,
    )

    assert _is_submission_completed_conflict(
        _FakeIntegrityError(COMPLETION_UNIQUE_INDEX)
    )
    assert not _is_submission_completed_conflict(
        _FakeIntegrityError("fk_submissions_rubric_version_id_versions")
    )
    assert not _is_submission_completed_conflict(
        _FakeIntegrityError("uq_submissions_object_key")
    )
    assert not _is_submission_completed_conflict(_FakeIntegrityError(None))


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _cleanup_settings(make_settings) -> Settings:
    """清理用例需要的配置：默认删除缓冲期。"""
    return make_settings(submission_upload_delete_buffer_seconds=3600)


def _backdate_put_expiry(engine: Engine, upload_ids: list[str]) -> None:
    """把指定上传会话的 PUT 到期时间拨回 2 小时前（越过删除缓冲期）。"""
    with engine.begin() as connection:
        for upload_id in upload_ids:
            connection.execute(
                text(
                    "UPDATE submission_upload_sessions"
                    " SET upload_url_expires_at = now() - interval '2 hours'"
                    " WHERE id = CAST(:id AS uuid)"
                ),
                {"id": upload_id},
            )
