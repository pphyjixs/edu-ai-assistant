"""课程练习接口的集成测试（专用测试库 + 假模型服务）。

覆盖契约第 7 节的完整闭环：生成 → 发布 → 提交 → 结果，以及权限、
状态分流、并发与 Worker 回写回归。模型侧全部使用本地假模型
（``httpx.MockTransport``）；**真实模型生成质量不在本文件验收范围**。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.errors import PracticeAlreadyAttemptedError
from app.core.time import utc_now
from app.modules.auth.models import User
from app.modules.practice import generation_ai
from app.modules.practice import service as practice_service
from app.modules.practice import worker as practice_worker
from app.modules.practice.models import PracticeDifficulty
from app.modules.practice.schemas import PracticeAttemptSubmitRequest
from tests.integration.test_chat_api import (
    _create_course_with_material,
    _make_chat_client,
    make_answering_factory,
)
from tests.integration.test_materials_api import (
    VALID_OUTLINE_PAYLOAD,
    _auth,
    _fake_model_client,
    _login,
    _model_json_response,
    _register,
)
from tests.integration.test_materials_api import fake_storage as fake_storage  # noqa: F401

GENERATE_URL = "/api/v1/courses/{course_id}/practice-sets/generate"
SETS_URL = "/api/v1/courses/{course_id}/practice-sets"
SET_URL = "/api/v1/practice-sets/{set_id}"
PUBLISH_URL = "/api/v1/practice-sets/{set_id}/publish"
ATTEMPTS_URL = "/api/v1/practice-sets/{set_id}/attempts"
RESULT_URL = "/api/v1/practice-attempts/{attempt_id}"
RETRY_URL = "/api/v1/jobs/{job_id}/retry"
JOB_URL = "/api/v1/jobs/{job_id}"

_CHUNK_ID = re.compile(r"chunk_id=([0-9a-fA-F-]{36})")
_CHUNK_CONTENT = re.compile(r"原文：\n(.*?)(?=\n\n\[片段 |\Z)", re.DOTALL)
_ALLOCATION = re.compile(
    r"(SINGLE_CHOICE|TRUE_FALSE|SHORT_ANSWER)（[^）]*）：(\d+) 题"
)


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage) -> Iterator[TestClient]:
    with _make_chat_client(
        db_isolation, pg_app, fake_storage, make_answering_factory()
    ) as test_client:
        yield test_client


def _drive_practice_worker(
    pg_session_factory, make_settings, ai_client_factory=None, **overrides
):
    """在测试库上驱动练习生成 Worker（同步包装）。"""
    overrides.setdefault("ai_base_url", "http://fake-model.local/v1")
    overrides.setdefault("ai_model", "fake-model")
    settings = make_settings(**overrides)

    async def run() -> int:
        return await practice_worker.run_pending_batch(
            pg_session_factory, settings=settings, ai_client_factory=ai_client_factory
        )

    return asyncio.run(run())


def _parse_context(prompt: str) -> tuple[list[str], str]:
    chunk_ids = _CHUNK_ID.findall(prompt)
    content_match = _CHUNK_CONTENT.search(prompt)
    content = content_match.group(1) if content_match else ""
    quote = next((line.strip() for line in content.splitlines() if line.strip()), "")
    return chunk_ids, quote


def _question_payload(question_type: str, chunk_id: str, quote: str) -> dict:
    base = {
        "type": question_type,
        "prompt": f"{question_type} 题目",
        "explanation": "依课件内容。",
        "knowledge_point": "软件工程",
        "source_chunk_id": chunk_id,
        "source_quote": quote,
    }
    if question_type == "SINGLE_CHOICE":
        base.update(
            {
                "options": [{"text": "正确项"}, {"text": "干扰项"}],
                "correct_option_index": 0,
            }
        )
    elif question_type == "TRUE_FALSE":
        base["correct_boolean"] = True
    else:
        base.update(
            {
                "correct_text": "应用系统化的方法。",
                "grading_points": [
                    {"point": "系统化", "accepted": ["系统化", "规范化"]},
                    {"point": "方法", "accepted": ["方法"]},
                ],
            }
        )
    return base


def practice_model_factory():
    """假模型：按提示词中的配额与片段返回合法题目。"""

    def responder(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompt = payload["messages"][1]["content"]
        chunk_ids, quote = _parse_context(prompt)
        if not chunk_ids:
            return _model_json_response({"title": "空", "questions": []})
        questions = []
        for question_type, count in _ALLOCATION.findall(prompt):
            for _ in range(int(count)):
                questions.append(
                    _question_payload(question_type, chunk_ids[0], quote)
                )
        return _model_json_response({"title": "自动生成练习", "questions": questions})

    return _fake_model_client(responder)


def _ready_course(
    client: TestClient, fake_storage, pg_session_factory, make_settings, *, email: str
) -> tuple[str, str, str]:
    """建课程 + 一份 READY 资料（有片段），返回 ``(课程 ID, 教师令牌, 资料 ID)``。"""
    course_id, teacher, material_id = _create_course_with_material(
        client, fake_storage, pg_session_factory, make_settings, email=email
    )
    detail = client.get(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert detail.json()["status"] == "READY", detail.text
    return course_id, teacher, material_id


def _join(client: TestClient, course_id: str, teacher: str, student: str) -> None:
    detail = client.get(f"/api/v1/courses/{course_id}", headers=_auth(teacher)).json()
    joined = client.post(
        "/api/v1/courses/join",
        json={"invite_code": detail["invite_code"]},
        headers=_auth(student),
    )
    assert joined.status_code == 201, joined.text


def _answers_from_teacher_view(questions: list[dict]) -> list[dict]:
    """按教师视角的标准答案构造满分提交。"""
    answers = []
    for question in questions:
        if question["type"] == "SHORT_ANSWER":
            answer: object = " ".join(
                point["accepted"][0] for point in question["grading_points"]
            )
        else:
            answer = question["correct_answer"]
        answers.append({"question_id": question["id"], "answer": answer})
    return answers


def _generate(
    client: TestClient,
    teacher: str,
    course_id: str,
    material_ids: list[str],
    *,
    question_count: int = 3,
    question_types: list[str] | None = None,
) -> httpx.Response:
    return client.post(
        GENERATE_URL.format(course_id=course_id),
        json={
            "material_ids": material_ids,
            "question_count": question_count,
            "question_types": question_types
            or ["SINGLE_CHOICE", "TRUE_FALSE", "SHORT_ANSWER"],
            "difficulty": "MEDIUM",
        },
        headers=_auth(teacher),
    )


def test_generate_then_worker_then_publish(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """生成 → Worker 出题 → 发布（含状态分流与学生可见性）。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"practice-a-{suffix}@example.com",
    )
    student_email = f"practice-a-student-{suffix}@example.com"
    _register(client, student_email, "STUDENT")
    student = _login(client, student_email)
    _join(client, course_id, teacher, student)

    generated = _generate(client, teacher, course_id, [material_id])
    assert generated.status_code == 202, generated.text
    job = generated.json()
    set_id = job["resource_id"]

    # 生成中：教师可见，学生 404；未就绪发布 409
    assert (
        client.get(SET_URL.format(set_id=set_id), headers=_auth(teacher)).json()["status"]
        == "GENERATING"
    )
    assert (
        client.get(SET_URL.format(set_id=set_id), headers=_auth(student)).status_code
        == 404
    )
    assert (
        client.post(
            PUBLISH_URL.format(set_id=set_id), headers=_auth(teacher)
        ).status_code
        == 409
    )

    processed = _drive_practice_worker(
        pg_session_factory, make_settings, practice_model_factory()
    )
    assert processed == 1

    view = client.get(SET_URL.format(set_id=set_id), headers=_auth(teacher)).json()
    assert view["status"] == "DRAFT"
    assert view["question_count"] == 3
    assert [item["type"] for item in view["questions"]] == [
        "SINGLE_CHOICE",
        "TRUE_FALSE",
        "SHORT_ANSWER",
    ]
    # 服务端生成选项 ID，并作为单选的标准答案；来源快照齐全
    single = view["questions"][0]
    assert single["correct_answer"] == single["options"][0]["id"]
    assert single["grading_points"] == []
    assert view["questions"][2]["grading_points"]

    job_view = client.get(JOB_URL.format(job_id=job["id"]), headers=_auth(teacher))
    assert job_view.json()["status"] == "SUCCEEDED"
    assert job_view.json()["progress"] == 100

    # 草稿对学生不可见
    assert (
        client.get(SET_URL.format(set_id=set_id), headers=_auth(student)).status_code
        == 404
    )

    # 发布（幂等：published_at 不变）
    published = client.post(PUBLISH_URL.format(set_id=set_id), headers=_auth(teacher))
    assert published.status_code == 200
    published_at = published.json()["published_at"]
    assert published_at is not None
    again = client.post(PUBLISH_URL.format(set_id=set_id), headers=_auth(teacher))
    assert again.status_code == 200
    assert again.json()["published_at"] == published_at

    # 列表只列已发布
    listing = client.get(SETS_URL.format(course_id=course_id), headers=_auth(student))
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["id"] == set_id


def test_submit_and_read_result(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """提交答案 → 百分制结果；重复提交 409；结果仅本人与创建教师可读。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"practice-b-{suffix}@example.com",
    )
    student_email = f"practice-b-student-{suffix}@example.com"
    outsider_email = f"practice-b-outsider-{suffix}@example.com"
    _register(client, student_email, "STUDENT")
    _register(client, outsider_email, "STUDENT")
    student = _login(client, student_email)
    outsider = _login(client, outsider_email)
    _join(client, course_id, teacher, student)

    generated = _generate(client, teacher, course_id, [material_id])
    set_id = generated.json()["resource_id"]
    _drive_practice_worker(
        pg_session_factory, make_settings, practice_model_factory()
    )
    client.post(PUBLISH_URL.format(set_id=set_id), headers=_auth(teacher))

    # 学生详情不含答案、评分要点与解析（先提交前）
    student_view = client.get(SET_URL.format(set_id=set_id), headers=_auth(student))
    assert student_view.status_code == 200
    for question in student_view.json()["questions"]:
        assert question["correct_answer"] is None
        assert question["explanation"] is None
        assert question["grading_points"] == []

    # 教师提交被拒（403），未知题目 / 缺题 / 类型不符 422
    teacher_view = client.get(SET_URL.format(set_id=set_id), headers=_auth(teacher)).json()
    assert (
        client.post(
            ATTEMPTS_URL.format(set_id=set_id),
            json={"answers": _answers_from_teacher_view(teacher_view["questions"])},
            headers=_auth(teacher),
        ).status_code
        == 403
    )
    assert (
        client.post(
            ATTEMPTS_URL.format(set_id=set_id),
            json={"answers": []},
            headers=_auth(student),
        ).status_code
        == 422
    )
    assert (
        client.post(
            ATTEMPTS_URL.format(set_id=set_id),
            json={
                "answers": [
                    {
                        "question_id": str(uuid.uuid4()),
                        "answer": True,
                    }
                ]
            },
            headers=_auth(student),
        ).status_code
        == 422
    )

    # 满分提交
    submitted = client.post(
        ATTEMPTS_URL.format(set_id=set_id),
        json={"answers": _answers_from_teacher_view(teacher_view["questions"])},
        headers=_auth(student),
    )
    assert submitted.status_code == 201, submitted.text
    result = submitted.json()
    assert result["total_score"] == 100.0
    assert len(result["answers"]) == 3
    assert all(item["is_correct"] for item in result["answers"])
    assert result["answers"][0]["correct_answer"]
    assert result["answers"][2]["score"] > 0

    # 重复提交：409
    repeated = client.post(
        ATTEMPTS_URL.format(set_id=set_id),
        json={"answers": _answers_from_teacher_view(teacher_view["questions"])},
        headers=_auth(student),
    )
    assert repeated.status_code == 409
    assert repeated.json()["error"]["code"] == "PRACTICE_ALREADY_ATTEMPTED"

    # 结果可见性：本人 200、创建教师 200、非成员 404、匿名 401
    attempt_id = result["id"]
    assert (
        client.get(RESULT_URL.format(attempt_id=attempt_id), headers=_auth(student))
        .status_code
        == 200
    )
    teacher_result = client.get(
        RESULT_URL.format(attempt_id=attempt_id), headers=_auth(teacher)
    )
    assert teacher_result.status_code == 200
    assert (
        client.get(RESULT_URL.format(attempt_id=attempt_id), headers=_auth(outsider))
        .status_code
        == 404
    )
    assert client.get(RESULT_URL.format(attempt_id=attempt_id)).status_code == 401

    # 刷新后仍可读，且提交响应与持久化数据逐字段一致
    reread = client.get(
        RESULT_URL.format(attempt_id=attempt_id), headers=_auth(student)
    ).json()
    assert reread["total_score"] == result["total_score"]
    assert reread["submitted_at"] == result["submitted_at"]
    assert len(reread["answers"]) == len(result["answers"])
    for fresh, original in zip(reread["answers"], result["answers"]):
        for field in (
            "question_id",
            "question_order",
            "type",
            "prompt",
            "submitted_answer",
            "is_correct",
            "score",
            "correct_answer",
            "explanation",
            "knowledge_point",
        ):
            assert fresh[field] == original[field], field


def test_practice_permissions_and_material_guards(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """生成接口的角色、可见性与资料状态守卫（契约 7.1 / 7.2）。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"practice-c-{suffix}@example.com",
    )
    student_email = f"practice-c-student-{suffix}@example.com"
    outsider_email = f"practice-c-outsider-{suffix}@example.com"
    _register(client, student_email, "STUDENT")
    _register(client, outsider_email, "STUDENT")
    student = _login(client, student_email)
    outsider = _login(client, outsider_email)
    _join(client, course_id, teacher, student)

    url = GENERATE_URL.format(course_id=course_id)
    payload = {
        "material_ids": [material_id],
        "question_count": 2,
        "question_types": ["SINGLE_CHOICE", "TRUE_FALSE"],
        "difficulty": "EASY",
    }

    # 匿名 401
    assert client.post(url, json=payload).status_code == 401

    # 学生 403 ROLE_FORBIDDEN
    student_call = client.post(url, json=payload, headers=_auth(student))
    assert student_call.status_code == 403
    assert student_call.json()["error"]["code"] == "ROLE_FORBIDDEN"

    # 非成员 404（不区分「不存在」与「不可见」）
    assert client.post(url, json=payload, headers=_auth(outsider)).status_code == 404

    # 资料不存在/不属于本课程 → 404
    missing = client.post(
        url,
        json={**payload, "material_ids": [str(uuid.uuid4())]},
        headers=_auth(teacher),
    )
    assert missing.status_code == 404

    # 请求边界：题数少于题型数 → 422
    assert _generate(
        client, teacher, course_id, [material_id],
        question_count=1, question_types=["SINGLE_CHOICE", "TRUE_FALSE"],
    ).status_code == 422

    # 资料未就绪（只上传未解析）：409 MATERIAL_NOT_READY
    from tests.integration.test_materials_api import _upload

    uploaded = _upload(client, fake_storage, teacher, course_id)
    not_ready = _generate(
        client, teacher, course_id, [uploaded["material"]["id"]]
    )
    assert not_ready.status_code == 409
    assert not_ready.json()["error"]["code"] == "MATERIAL_NOT_READY"


def test_archived_course_blocks_writes_but_allows_reads(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """归档课程：生成/发布/提交 409，列表/详情/结果仍可读（契约 7.1）。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"practice-d-{suffix}@example.com",
    )
    student_email = f"practice-d-student-{suffix}@example.com"
    _register(client, student_email, "STUDENT")
    student = _login(client, student_email)
    _join(client, course_id, teacher, student)

    generated = _generate(client, teacher, course_id, [material_id])
    set_id = generated.json()["resource_id"]
    job_id = generated.json()["id"]
    _drive_practice_worker(
        pg_session_factory, make_settings, practice_model_factory()
    )
    assert (
        client.post(
            PUBLISH_URL.format(set_id=set_id), headers=_auth(teacher)
        ).status_code
        == 200
    )
    teacher_view = client.get(SET_URL.format(set_id=set_id), headers=_auth(teacher)).json()

    archived = client.post(
        f"/api/v1/courses/{course_id}/archive", headers=_auth(teacher)
    )
    assert archived.status_code == 200

    # 归档后禁止生成、发布与提交
    blocked_generate = _generate(client, teacher, course_id, [material_id])
    assert blocked_generate.status_code == 409
    assert blocked_generate.json()["error"]["code"] == "COURSE_ARCHIVED"

    blocked_publish = client.post(
        PUBLISH_URL.format(set_id=set_id), headers=_auth(teacher)
    )
    assert blocked_publish.status_code == 409

    blocked_submit = client.post(
        ATTEMPTS_URL.format(set_id=set_id),
        json={"answers": _answers_from_teacher_view(teacher_view["questions"])},
        headers=_auth(student),
    )
    assert blocked_submit.status_code == 409
    assert blocked_submit.json()["error"]["code"] == "COURSE_ARCHIVED"

    # 但读取历史仍然可用
    assert (
        client.get(SETS_URL.format(course_id=course_id), headers=_auth(student))
        .status_code
        == 200
    )
    assert (
        client.get(SET_URL.format(set_id=set_id), headers=_auth(student)).status_code
        == 200
    )
    assert (
        client.get(JOB_URL.format(job_id=job_id), headers=_auth(teacher)).status_code
        == 200
    )


def _empty_question_model_factory():
    """假模型：返回空题目 → 服务端校验失败（整次生成失败）。"""

    def responder(request: httpx.Request) -> httpx.Response:
        return _model_json_response({"title": "无效", "questions": []})

    return _fake_model_client(responder)


def test_worker_failure_then_retry_recovers(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """失败不留部分题目 → 重试复用 ID → 成功后发布；旧令牌无法回写。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"practice-e-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    job = generated.json()
    set_id = job["resource_id"]

    # 第一次运行：模型输出非法 → FAILED，不落库任何题目
    assert (
        _drive_practice_worker(
            pg_session_factory, make_settings, _empty_question_model_factory()
        )
        == 1
    )
    failed_view = client.get(SET_URL.format(set_id=set_id), headers=_auth(teacher)).json()
    assert failed_view["status"] == "FAILED"
    assert failed_view["questions"] == []
    assert failed_view["question_count"] == 0
    job_view = client.get(JOB_URL.format(job_id=job["id"]), headers=_auth(teacher)).json()
    assert job_view["status"] == "FAILED"
    assert job_view["error"]

    # 重试：复用 job 与练习 ID，重置为 PENDING / GENERATING
    retried = client.post(RETRY_URL.format(job_id=job["id"]), headers=_auth(teacher))
    assert retried.status_code == 202, retried.text
    assert retried.json()["id"] == job["id"]
    assert retried.json()["status"] == "PENDING"
    assert (
        client.get(SET_URL.format(set_id=set_id), headers=_auth(teacher)).json()["status"]
        == "GENERATING"
    )

    # 旧执行者（旧运行令牌）无法回写：直接调用回写函数应被拒绝
    stale_token = "0" * 32
    validated_stub = generation_ai.ValidatedPractice(title="旧结果", questions=[])
    written = asyncio.run(
        practice_worker._write_success(
            pg_session_factory,
            claimed=practice_worker.ClaimedPracticeJob(
                job_id=uuid.UUID(job["id"]),
                practice_set_id=uuid.UUID(set_id),
                course_id=uuid.UUID(course_id),
                run_token=stale_token,
                requested_question_count=3,
                question_types=["SINGLE_CHOICE", "TRUE_FALSE", "SHORT_ANSWER"],
                difficulty=PracticeDifficulty.MEDIUM,
            ),
            validated=validated_stub,
            settings=make_settings(
                ai_base_url="http://fake-model.local/v1", ai_model="fake-model"
            ),
            duration_ms=1,
            now=utc_now(),
        )
    )
    assert written is False
    assert (
        client.get(SET_URL.format(set_id=set_id), headers=_auth(teacher)).json()["status"]
        == "GENERATING"
    )

    # 重新驱动 Worker（正常模型）→ DRAFT
    assert (
        _drive_practice_worker(
            pg_session_factory, make_settings, practice_model_factory()
        )
        == 1
    )
    recovered = client.get(SET_URL.format(set_id=set_id), headers=_auth(teacher)).json()
    assert recovered["status"] == "DRAFT"
    assert recovered["question_count"] == 3

    # 已成功的任务不可重试；资料解析任务也不走这个接口
    not_retryable = client.post(
        RETRY_URL.format(job_id=job["id"]), headers=_auth(teacher)
    )
    assert not_retryable.status_code == 409
    assert not_retryable.json()["error"]["code"] == "JOB_NOT_RETRYABLE"

    with pg_sync_engine.connect() as connection:
        parse_job_id = connection.execute(
            text(
                "SELECT id FROM jobs WHERE type = 'MATERIAL_PARSE' LIMIT 1"
            )
        ).scalar_one()
    parse_retry = client.post(
        RETRY_URL.format(job_id=str(parse_job_id)), headers=_auth(teacher)
    )
    assert parse_retry.status_code == 409
    assert parse_retry.json()["error"]["code"] == "JOB_NOT_RETRYABLE"

    # 匿名与学生都不能重试
    assert client.post(RETRY_URL.format(job_id=job["id"])).status_code == 401


# --------------------------------------------------------------------------- #
# Worker 领取互斥与并发提交
# --------------------------------------------------------------------------- #
def test_worker_claim_is_mutually_exclusive(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """两个 Worker 并发领取：每个任务恰好被领取一次（不重复、不漏领）。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"practice-f-{suffix}@example.com",
    )
    for _ in range(2):
        assert _generate(client, teacher, course_id, [material_id]).status_code == 202

    settings = make_settings(
        ai_base_url="http://fake-model.local/v1", ai_model="fake-model"
    )

    async def claim_concurrently():
        return await asyncio.gather(
            practice_worker.claim_next(
                pg_session_factory,
                now=utc_now(),
                lease_seconds=settings.practice_generate_lease_seconds,
            ),
            practice_worker.claim_next(
                pg_session_factory,
                now=utc_now(),
                lease_seconds=settings.practice_generate_lease_seconds,
            ),
        )

    results = asyncio.run(claim_concurrently())
    claimed = [item for item in results if item is not None]

    assert len(claimed) == 2, "两个任务都应被领取"
    assert len({item.job_id for item in claimed}) == 2, "不得重复领取同一任务"
    assert len({item.practice_set_id for item in claimed}) == 2
    assert len({item.run_token for item in claimed}) == 2, "运行令牌必须互不相同"
    assert all(item.course_id == uuid.UUID(course_id) for item in claimed)

    # 每个任务的 attempts 恰好递增一次（并发领取没有重复计数）
    with pg_sync_engine.connect() as connection:
        attempts = (
            connection.execute(
                text(
                    "SELECT attempts FROM jobs"
                    " WHERE type = 'PRACTICE_GENERATE' ORDER BY attempts"
                )
            )
            .scalars()
            .all()
        )
    assert list(attempts) == [1, 1]


def test_concurrent_submit_only_one_succeeds(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """并发提交：唯一约束保证每生每题集只有一次成功。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"practice-g-{suffix}@example.com",
    )
    student_email = f"practice-g-student-{suffix}@example.com"
    _register(client, student_email, "STUDENT")
    student = _login(client, student_email)
    _join(client, course_id, teacher, student)

    generated = _generate(client, teacher, course_id, [material_id])
    set_id = generated.json()["resource_id"]
    _drive_practice_worker(
        pg_session_factory, make_settings, practice_model_factory()
    )
    client.post(PUBLISH_URL.format(set_id=set_id), headers=_auth(teacher))
    teacher_view = client.get(SET_URL.format(set_id=set_id), headers=_auth(teacher)).json()
    answers = _answers_from_teacher_view(teacher_view["questions"])
    student_id = uuid.UUID(
        client.get("/api/v1/users/me", headers=_auth(student)).json()["id"]
    )

    async def submit_once() -> str:
        async with pg_session_factory() as session:
            user = await session.get(User, student_id)
            assert user is not None
            try:
                locked = await practice_service.lock_set_for_submit(
                    session, user=user, set_id=uuid.UUID(set_id)
                )
                await practice_service.submit_attempt(
                    session,
                    practice_set=locked,
                    user=user,
                    payload=PracticeAttemptSubmitRequest.model_validate(
                        {"answers": answers}
                    ),
                )
                return "ok"
            except PracticeAlreadyAttemptedError:
                return "duplicate"

    async def run() -> list[str]:
        return sorted(await asyncio.gather(submit_once(), submit_once()))

    assert asyncio.run(run()) == ["duplicate", "ok"]

    # 库里只留一份答题记录与一份明细
    with pg_sync_engine.connect() as connection:
        attempt_count = connection.execute(
            text(
                "SELECT count(*) FROM practice_attempts"
                " WHERE practice_set_id = CAST(:id AS uuid)"
            ),
            {"id": set_id},
        ).scalar_one()
        answer_count = connection.execute(
            text("SELECT count(*) FROM practice_attempt_answers")
        ).scalar_one()
    assert attempt_count == 1
    assert answer_count == len(answers)


# --------------------------------------------------------------------------- #
# 生成期间的竞态：资料删除、课程归档、模型调用的事务边界
# --------------------------------------------------------------------------- #
def test_material_deleted_during_generation_fails_without_questions(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """生成期间来源资料被删除：整次生成失败，不发布过期结果。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"practice-h-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    job = generated.json()
    set_id = job["resource_id"]

    deleted = client.delete(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert deleted.status_code == 204

    assert (
        _drive_practice_worker(
            pg_session_factory, make_settings, practice_model_factory()
        )
        == 1
    )

    view = client.get(SET_URL.format(set_id=set_id), headers=_auth(teacher)).json()
    assert view["status"] == "FAILED"
    assert view["questions"] == []
    job_view = client.get(JOB_URL.format(job_id=job["id"]), headers=_auth(teacher)).json()
    assert job_view["status"] == "FAILED"


def test_course_archived_during_generation_cancels(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """生成期间课程被归档：写入阶段复查后置为 CANCELLED，不留题目。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"practice-i-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    job = generated.json()
    set_id = job["resource_id"]

    archived = client.post(
        f"/api/v1/courses/{course_id}/archive", headers=_auth(teacher)
    )
    assert archived.status_code == 200

    assert (
        _drive_practice_worker(
            pg_session_factory, make_settings, practice_model_factory()
        )
        == 1
    )

    view = client.get(SET_URL.format(set_id=set_id), headers=_auth(teacher)).json()
    assert view["status"] == "CANCELLED"
    assert view["questions"] == []
    job_view = client.get(JOB_URL.format(job_id=job["id"]), headers=_auth(teacher)).json()
    assert job_view["status"] == "CANCELLED"


def test_worker_calls_model_without_active_transaction(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型调用期间没有任何"事务中空闲"的连接（不持有数据库事务）。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"practice-j-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    assert generated.status_code == 202

    observed: dict[str, int] = {}
    original = generation_ai.generate_practice

    def spy(*args, **kwargs):
        # 该函数由 Worker 通过线程池调用；此刻若仍持有事务，会看到 idle in transaction
        with pg_sync_engine.connect() as connection:
            observed["idle_in_transaction"] = connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = current_database()"
                    " AND state = 'idle in transaction'"
                )
            ).scalar_one()
        return original(*args, **kwargs)

    monkeypatch.setattr(practice_worker.generation_ai, "generate_practice", spy)

    assert (
        _drive_practice_worker(
            pg_session_factory, make_settings, practice_model_factory()
        )
        == 1
    )

    assert observed.get("idle_in_transaction") == 0, "模型调用期间不应持有事务"
