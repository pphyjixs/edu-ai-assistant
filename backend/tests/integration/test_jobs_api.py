"""通用异步任务接口的集成测试（``docs/api-contract.md`` 第 10 节）。

覆盖三块：

1. **查询可见性矩阵**：三类公开任务各自的成员/角色/删除/归档规则，
   以及 ``AGENT_RUN`` 与未知任务一律 ``404``（与"不存在"不可区分）；
2. **重试分流与状态**：资料（改走资料接口）、练习（仅 FAILED 或过期租约）、
   提交批改（与 ``POST /submissions/{id}/grade`` 共用逻辑）三条分支；
3. **请求体与错误优先级**：省略/``{}``/``null``/数组/标量/多余字段/非法 JSON/非法 UTF-8，
   并叠加匿名、不可见、角色不符、归档与状态不可重试。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator

import pytest
from app.modules.grading import worker as grading_worker
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.integration.test_agent_api import (
    _create_run,
    _create_session,
    _teacher_with_ready_material,
)
from tests.integration.test_chat_api import _make_chat_client
from tests.integration.test_grading_api import (
    GRADE_URL,
    _grading_settings,
    _open_assignment,
    _submit_report,
)
from tests.integration.test_materials_api import (
    _auth,
    _login,
    _register,
)
from tests.integration.test_materials_api import (
    fake_storage as fake_storage,  # noqa: PLC0414 - pytest fixture registration
)
from tests.integration.test_practice_api import (
    JOB_URL,
    RETRY_URL,
    _drive_practice_worker,
    _empty_question_model_factory,
    _generate,
    _join,
    _ready_course,
    practice_model_factory,
)

#: 契约 10.0 的响应字段（不得多也不少）
EXPECTED_FIELDS = {
    "id",
    "type",
    "status",
    "progress",
    "resource_type",
    "resource_id",
    "error",
    "created_at",
    "started_at",
    "finished_at",
}
#: 内部调度字段：任何响应都不得包含
INTERNAL_FIELDS = {"attempts", "run_token", "lease_expires_at"}


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage) -> Iterator[TestClient]:
    with _make_chat_client(
        db_isolation, pg_app, fake_storage, practice_model_factory()
    ) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# 库内助手（表名与列名均来自测试常量，不接受外部输入）
# --------------------------------------------------------------------------- #
def _job_id(pg_sync_engine, job_type: str, resource_id: str) -> str:
    with pg_sync_engine.connect() as connection:
        return str(
            connection.execute(
                text(
                    "SELECT id FROM jobs"
                    " WHERE type = CAST(:job_type AS job_type)"
                    " AND resource_id = CAST(:rid AS uuid)"
                ),
                {"job_type": job_type, "rid": resource_id},
            ).scalar_one()
        )


def _job_row(pg_sync_engine, job_id: str) -> dict:
    with pg_sync_engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT status, progress, error, run_token, lease_expires_at,"
                    " started_at, finished_at FROM jobs WHERE id = CAST(:id AS uuid)"
                ),
                {"id": job_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def _set_job_status(pg_sync_engine, job_id: str, status: str) -> None:
    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET status = CAST(:status AS job_status)"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"status": status, "id": job_id},
        )


def _set_job_running(pg_sync_engine, job_id: str, *, lease_offset: str) -> None:
    """把任务置为 RUNNING 并带运行令牌；租约偏移由测试常量给出（如 ``-1 hour``）。"""
    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET status = 'RUNNING'::job_status,"
                " run_token = 'test-token',"
                f" lease_expires_at = now() + interval '{lease_offset}'"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": job_id},
        )


def _clear_run_fields(pg_sync_engine, job_id: str) -> None:
    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET run_token = 'stale-token', lease_expires_at = NULL"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": job_id},
        )


def _error(response) -> str:
    return response.json()["error"]["code"]


def _new_student(client: TestClient, course_id: str, teacher: str, suffix: str) -> str:
    email = f"jobs-student-{suffix}@example.com"
    _register(client, email, "STUDENT")
    student = _login(client, email)
    _join(client, course_id, teacher, student)
    return student


def _outsider(client: TestClient, suffix: str, *, role: str = "TEACHER") -> str:
    email = f"jobs-outsider-{suffix}@example.com"
    _register(client, email, role)
    return _login(client, email)


def _ready_material(
    client: TestClient, fake_storage, pg_session_factory, make_settings, suffix: str
) -> tuple[str, str, str]:
    """课程 + READY 资料，返回 ``(课程, 教师, 资料)``。"""
    return _ready_course(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email=f"jobs-material-{suffix}@example.com",
    )


# --------------------------------------------------------------------------- #
# 10.1 查询任务状态：可见性矩阵
# --------------------------------------------------------------------------- #
def test_anonymous_is_rejected_with_401(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    _course, _teacher, material_id = _ready_material(
        client, fake_storage, pg_session_factory, make_settings, "anon"
    )
    job_id = _job_id(pg_sync_engine, "MATERIAL_PARSE", material_id)

    assert client.get(JOB_URL.format(job_id=job_id)).status_code == 401
    assert client.post(RETRY_URL.format(job_id=job_id)).status_code == 401


def test_unknown_job_is_404_for_get_and_retry(
    client: TestClient, db_isolation: None
) -> None:
    """不存在与不可见完全一致：两者都是 404 RESOURCE_NOT_FOUND。"""
    email = f"jobs-unknown-{uuid.uuid4().hex[:8]}@example.com"
    _register(client, email, "TEACHER")
    teacher = _login(client, email)
    missing = uuid.uuid4()

    get_response = client.get(JOB_URL.format(job_id=missing), headers=_auth(teacher))
    assert get_response.status_code == 404, get_response.text
    assert _error(get_response) == "RESOURCE_NOT_FOUND"

    retry_response = client.post(RETRY_URL.format(job_id=missing), headers=_auth(teacher))
    assert retry_response.status_code == 404, retry_response.text
    assert _error(retry_response) == "RESOURCE_NOT_FOUND"


def test_material_parse_visibility(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """资料任务：成员可读、非成员 404、归档课程仍可读。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_material(
        client, fake_storage, pg_session_factory, make_settings, f"mat-vis-{suffix}"
    )
    job_id = _job_id(pg_sync_engine, "MATERIAL_PARSE", material_id)
    student = _new_student(client, course_id, teacher, suffix)
    outsider = _outsider(client, suffix)

    member_response = client.get(JOB_URL.format(job_id=job_id), headers=_auth(student))
    assert member_response.status_code == 200, member_response.text
    payload = member_response.json()
    assert set(payload) == EXPECTED_FIELDS
    assert payload["type"] == "MATERIAL_PARSE"
    assert payload["resource_type"] == "MATERIAL"
    assert payload["progress"] == 100  # 已 READY

    assert (
        client.get(JOB_URL.format(job_id=job_id), headers=_auth(outsider)).status_code == 404
    )

    archived = client.post(f"/api/v1/courses/{course_id}/archive", headers=_auth(teacher))
    assert archived.status_code == 200, archived.text
    assert client.get(JOB_URL.format(job_id=job_id), headers=_auth(teacher)).status_code == 200


def test_material_parse_job_unreadable_after_material_deleted(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    suffix = uuid.uuid4().hex[:8]
    _course_id, teacher, material_id = _ready_material(
        client, fake_storage, pg_session_factory, make_settings, f"mat-del-{suffix}"
    )
    job_id = _job_id(pg_sync_engine, "MATERIAL_PARSE", material_id)

    deleted = client.delete(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert deleted.status_code == 204, deleted.text

    assert client.get(JOB_URL.format(job_id=job_id), headers=_auth(teacher)).status_code == 404


def test_practice_generate_visibility(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """练习任务：创建教师可读全部状态；学生仅发布后可读；非成员 404。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_material(
        client, fake_storage, pg_session_factory, make_settings, f"prac-vis-{suffix}"
    )
    student = _new_student(client, course_id, teacher, suffix)
    outsider = _outsider(client, suffix)

    generated = _generate(client, teacher, course_id, [material_id])
    assert generated.status_code == 202, generated.text
    job_id = generated.json()["id"]
    set_id = generated.json()["resource_id"]

    assert client.get(JOB_URL.format(job_id=job_id), headers=_auth(teacher)).status_code == 200
    assert client.get(JOB_URL.format(job_id=job_id), headers=_auth(student)).status_code == 404
    assert client.get(JOB_URL.format(job_id=job_id), headers=_auth(outsider)).status_code == 404

    assert _drive_practice_worker(
        pg_session_factory, make_settings, practice_model_factory()
    ) == 1
    # 出题成功但仍为 DRAFT：学生依旧 404
    assert client.get(JOB_URL.format(job_id=job_id), headers=_auth(student)).status_code == 404

    published = client.post(
        f"/api/v1/practice-sets/{set_id}/publish", headers=_auth(teacher)
    )
    assert published.status_code == 200, published.text

    response = client.get(JOB_URL.format(job_id=job_id), headers=_auth(student))
    assert response.status_code == 200, response.text
    assert response.json()["type"] == "PRACTICE_GENERATE"
    assert response.json()["resource_type"] == "PRACTICE_SET"


def test_submission_grade_visibility(
    client: TestClient, fake_storage
) -> None:
    """批改任务：仅课程创建教师与提交本人可读，其他一律 404。"""
    suffix = uuid.uuid4().hex[:8]
    _course, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])
    triggered = client.post(
        GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher)
    )
    assert triggered.status_code == 202, triggered.text
    job_id = triggered.json()["id"]

    assert client.get(JOB_URL.format(job_id=job_id), headers=_auth(teacher)).status_code == 200
    owner = client.get(JOB_URL.format(job_id=job_id), headers=_auth(student))
    assert owner.status_code == 200, owner.text
    assert owner.json()["type"] == "SUBMISSION_GRADE"
    assert owner.json()["resource_type"] == "SUBMISSION"

    other_student = _outsider(client, f"{suffix}-other", role="STUDENT")
    other_teacher = _outsider(client, f"{suffix}-t")
    assert (
        client.get(JOB_URL.format(job_id=job_id), headers=_auth(other_student)).status_code
        == 404
    )
    assert (
        client.get(JOB_URL.format(job_id=job_id), headers=_auth(other_teacher)).status_code
        == 404
    )


def test_agent_run_job_is_404_for_every_caller(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """AGENT_RUN 不通过通用 Jobs 接口暴露：本人与其他人都得到 404。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, _material_id = _teacher_with_ready_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email=f"jobs-agent-{suffix}@example.com",
    )
    session_id = _create_session(client, teacher, course_id)
    created = _create_run(client, teacher, session_id)
    assert created.status_code == 202, created.text
    run_id = created.json()["id"]
    job_id = _job_id(pg_sync_engine, "AGENT_RUN", run_id)

    outsider = _outsider(client, suffix)
    for token in (teacher, outsider):
        get_response = client.get(JOB_URL.format(job_id=job_id), headers=_auth(token))
        assert get_response.status_code == 404, get_response.text
        assert _error(get_response) == "RESOURCE_NOT_FOUND"
        retry_response = client.post(
            RETRY_URL.format(job_id=job_id), headers=_auth(token)
        )
        assert retry_response.status_code == 404, retry_response.text


# --------------------------------------------------------------------------- #
# 10.2 重试：资料任务
# --------------------------------------------------------------------------- #
def test_material_parse_retry_priority_matrix(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """资料任务重试：非成员（含其他教师）404 → 学生 403 → 409 → 归档 409。

    其他教师无法通过接口加入课程（``POST /courses/join`` 对学生开放），
    因此"非创建教师成员"这一态不存在；非成员教师与普通非成员同样得到 404，
    这正是"不可见与不存在不可区分"的要求。
    """
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_material(
        client, fake_storage, pg_session_factory, make_settings, f"mat-retry-{suffix}"
    )
    job_id = _job_id(pg_sync_engine, "MATERIAL_PARSE", material_id)
    url = RETRY_URL.format(job_id=job_id)

    outsider = _outsider(client, suffix)
    non_member = client.post(url, headers=_auth(outsider))
    assert non_member.status_code == 404, non_member.text
    assert _error(non_member) == "RESOURCE_NOT_FOUND"

    other_teacher = _outsider(client, f"{suffix}-t2")
    other_response = client.post(url, headers=_auth(other_teacher))
    assert other_response.status_code == 404, other_response.text
    assert _error(other_response) == "RESOURCE_NOT_FOUND"

    student = _new_student(client, course_id, teacher, suffix)
    student_response = client.post(url, headers=_auth(student))
    assert student_response.status_code == 403, student_response.text
    assert _error(student_response) == "ROLE_FORBIDDEN"

    creator_response = client.post(url, headers=_auth(teacher))
    assert creator_response.status_code == 409, creator_response.text
    assert _error(creator_response) == "JOB_NOT_RETRYABLE"

    assert (
        client.post(f"/api/v1/courses/{course_id}/archive", headers=_auth(teacher)).status_code
        == 200
    )
    archived = client.post(url, headers=_auth(teacher))
    assert archived.status_code == 409, archived.text
    assert _error(archived) == "COURSE_ARCHIVED"


# --------------------------------------------------------------------------- #
# 10.2 重试：练习生成任务
# --------------------------------------------------------------------------- #
def _practice_job(
    client: TestClient, fake_storage, pg_session_factory, make_settings, suffix: str
) -> tuple[str, str, str]:
    """建课程 + 生成练习，返回 ``(教师, 课程, 任务)``（任务处于 PENDING）。"""
    course_id, teacher, material_id = _ready_material(
        client, fake_storage, pg_session_factory, make_settings, f"prac-{suffix}"
    )
    generated = _generate(client, teacher, course_id, [material_id])
    assert generated.status_code == 202, generated.text
    return teacher, course_id, generated.json()["id"]


def _fail_practice_job(pg_session_factory, make_settings) -> None:
    """让模型返回空题目，使任务进入 FAILED。"""
    assert _drive_practice_worker(
        pg_session_factory, make_settings, _empty_question_model_factory()
    ) == 1


def test_practice_retry_resets_failed_job(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """FAILED → 202：复用原 job，清除令牌/租约/错误并回到 PENDING。"""
    suffix = uuid.uuid4().hex[:8]
    teacher, _course_id, job_id = _practice_job(
        client, fake_storage, pg_session_factory, make_settings, suffix
    )
    _fail_practice_job(pg_session_factory, make_settings)
    assert _job_row(pg_sync_engine, job_id)["status"] == "FAILED"
    _clear_run_fields(pg_sync_engine, job_id)

    response = client.post(RETRY_URL.format(job_id=job_id), headers=_auth(teacher))

    assert response.status_code == 202, response.text
    assert response.json()["id"] == job_id
    assert response.json()["status"] == "PENDING"
    assert response.json()["progress"] == 0
    assert response.json()["error"] is None
    row = _job_row(pg_sync_engine, job_id)
    assert row["run_token"] is None
    assert row["lease_expires_at"] is None
    assert row["started_at"] is None
    assert row["finished_at"] is None


@pytest.mark.parametrize("status", ["PENDING", "RUNNING", "SUCCEEDED", "CANCELLED"])
def test_practice_retry_rejects_unretryable_status(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    status: str,
) -> None:
    """PENDING / 有效 RUNNING / SUCCEEDED / CANCELLED 一律 409 JOB_NOT_RETRYABLE。"""
    suffix = uuid.uuid4().hex[:8]
    teacher, _course_id, job_id = _practice_job(
        client, fake_storage, pg_session_factory, make_settings, suffix
    )
    if status == "RUNNING":
        _set_job_running(pg_sync_engine, job_id, lease_offset="10 minutes")
    else:
        _set_job_status(pg_sync_engine, job_id, status)

    response = client.post(RETRY_URL.format(job_id=job_id), headers=_auth(teacher))

    assert response.status_code == 409, response.text
    assert _error(response) == "JOB_NOT_RETRYABLE"
    assert _job_row(pg_sync_engine, job_id)["status"] == status


def test_practice_retry_accepts_expired_lease(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """租约已过期的 RUNNING（崩溃遗留）→ 202 重置。"""
    suffix = uuid.uuid4().hex[:8]
    teacher, _course_id, job_id = _practice_job(
        client, fake_storage, pg_session_factory, make_settings, suffix
    )
    _set_job_running(pg_sync_engine, job_id, lease_offset="-1 hour")

    response = client.post(RETRY_URL.format(job_id=job_id), headers=_auth(teacher))

    assert response.status_code == 202, response.text
    assert response.json()["id"] == job_id
    assert _job_row(pg_sync_engine, job_id)["status"] == "PENDING"


# --------------------------------------------------------------------------- #
# 10.2 重试：提交批改任务
# --------------------------------------------------------------------------- #
def _failed_grade_job(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> tuple[str, str, str]:
    """准备一个批改失败的任务，返回 ``(教师, 提交, 任务)``。"""
    _course, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])
    triggered = client.post(
        GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher)
    )
    assert triggered.status_code == 202, triggered.text
    job_id = triggered.json()["id"]

    fake_storage.as_unavailable()
    assert asyncio.run(
        grading_worker.run_pending_batch(
            pg_session_factory,
            settings=_grading_settings(make_settings),
            storage=fake_storage,
            ai_client_factory=None,
            max_jobs=1,
        )
    ) == 1
    fake_storage.as_available()
    return teacher, detail["id"], job_id


def test_submission_grade_retry_resets_failed_job(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """批改失败 → 202 复用原 job 并重置；提交回到 GRADING。"""
    teacher, submission_id, job_id = _failed_grade_job(
        client, fake_storage, pg_session_factory, make_settings
    )
    assert _job_row(pg_sync_engine, job_id)["status"] == "FAILED"

    response = client.post(RETRY_URL.format(job_id=job_id), headers=_auth(teacher))

    assert response.status_code == 202, response.text
    assert response.json()["id"] == job_id
    assert response.json()["type"] == "SUBMISSION_GRADE"
    assert response.json()["resource_id"] == submission_id
    assert _job_row(pg_sync_engine, job_id)["status"] == "PENDING"
    with pg_sync_engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM submissions WHERE id = CAST(:id AS uuid)"),
            {"id": submission_id},
        ).scalar_one()
    assert status == "GRADING"


def test_submission_grade_retry_is_idempotent_while_pending(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """排队中重复重试：幂等返回同一个任务（不产生第二个 job）。"""
    teacher, _submission_id, job_id = _failed_grade_job(
        client, fake_storage, pg_session_factory, make_settings
    )
    first = client.post(RETRY_URL.format(job_id=job_id), headers=_auth(teacher))
    second = client.post(RETRY_URL.format(job_id=job_id), headers=_auth(teacher))

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json()["id"] == second.json()["id"] == job_id
    with pg_sync_engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM jobs WHERE type = 'SUBMISSION_GRADE'")
        ).scalar_one()
    assert count == 1


def test_submission_grade_retry_rejected_after_review(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """已有批改结果（REVIEW_REQUIRED / SUCCEEDED）→ 409 SUBMISSION_NOT_READY。"""
    teacher, _submission_id, job_id = _failed_grade_job(
        client, fake_storage, pg_session_factory, make_settings
    )
    _set_job_status(pg_sync_engine, job_id, "SUCCEEDED")

    response = client.post(RETRY_URL.format(job_id=job_id), headers=_auth(teacher))

    assert response.status_code == 409, response.text
    assert _error(response) == "SUBMISSION_NOT_READY"


# --------------------------------------------------------------------------- #
# 10.2 请求体与错误优先级
# --------------------------------------------------------------------------- #
_BAD_BODIES: list[tuple[str, object]] = [
    ("null", None),
    ("array", []),
    ("scalar", 42),
    ("string", "retry"),
    ("extra", {"unexpected": True}),
]


@pytest.mark.parametrize(("name", "payload"), _BAD_BODIES, ids=[n for n, _ in _BAD_BODIES])
def test_retry_request_body_variants_are_422(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    name: str,
    payload: object,
) -> None:
    """非法请求体：显式 null / 数组 / 标量 / 多余字段 → 422，且状态不变。"""
    suffix = uuid.uuid4().hex[:8]
    teacher, _course_id, job_id = _practice_job(
        client, fake_storage, pg_session_factory, make_settings, suffix
    )
    _fail_practice_job(pg_session_factory, make_settings)

    response = client.post(
        RETRY_URL.format(job_id=job_id),
        content=json.dumps(payload).encode("utf-8"),
        headers={**_auth(teacher), "Content-Type": "application/json"},
    )

    assert response.status_code == 422, f"{name}: {response.text}"
    assert _error(response) == "VALIDATION_ERROR"
    assert _job_row(pg_sync_engine, job_id)["status"] == "FAILED"


def test_retry_accepts_omitted_body_and_empty_object(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """省略请求体与 ``{}`` 都成功。"""
    suffix = uuid.uuid4().hex[:8]
    teacher, _course_id, job_id = _practice_job(
        client, fake_storage, pg_session_factory, make_settings, suffix
    )
    _fail_practice_job(pg_session_factory, make_settings)

    omitted = client.post(RETRY_URL.format(job_id=job_id), headers=_auth(teacher))
    assert omitted.status_code == 202, omitted.text

    _fail_practice_job(pg_session_factory, make_settings)
    empty_object = client.post(
        RETRY_URL.format(job_id=job_id), json={}, headers=_auth(teacher)
    )
    assert empty_object.status_code == 202, empty_object.text


def test_retry_rejects_invalid_json_and_utf8(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """非法 JSON 与非法 UTF-8 → 422 VALIDATION_ERROR（任务已处于可重试状态）。"""
    suffix = uuid.uuid4().hex[:8]
    teacher, _course_id, job_id = _practice_job(
        client, fake_storage, pg_session_factory, make_settings, suffix
    )
    _fail_practice_job(pg_session_factory, make_settings)
    url = RETRY_URL.format(job_id=job_id)

    invalid_json = client.post(
        url,
        content=b"{not json",
        headers={**_auth(teacher), "Content-Type": "application/json"},
    )
    assert invalid_json.status_code == 422, invalid_json.text
    assert _error(invalid_json) == "VALIDATION_ERROR"

    invalid_utf8 = client.post(
        url,
        content=b'{"a": "\xff\xfe"}',
        headers={**_auth(teacher), "Content-Type": "application/json"},
    )
    assert invalid_utf8.status_code == 422, invalid_utf8.text
    assert _error(invalid_utf8) == "VALIDATION_ERROR"


def test_error_priority_outranks_body_validation(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """畸形请求体叠加越权/归档时，返回更高优先级的错误码而不是 422。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_material(
        client, fake_storage, pg_session_factory, make_settings, f"priority-{suffix}"
    )
    job_id = _job_id(pg_sync_engine, "MATERIAL_PARSE", material_id)
    url = RETRY_URL.format(job_id=job_id)
    bad_body = {"unexpected": True}

    anonymous = client.post(url, json=bad_body)
    assert anonymous.status_code == 401, anonymous.text

    outsider = _outsider(client, suffix)
    assert client.post(url, json=bad_body, headers=_auth(outsider)).status_code == 404

    student = _new_student(client, course_id, teacher, suffix)
    assert client.post(url, json=bad_body, headers=_auth(student)).status_code == 403

    creator = client.post(url, json=bad_body, headers=_auth(teacher))
    assert creator.status_code == 409, creator.text
    assert _error(creator) == "JOB_NOT_RETRYABLE"

    assert (
        client.post(f"/api/v1/courses/{course_id}/archive", headers=_auth(teacher)).status_code
        == 200
    )
    archived = client.post(url, json=bad_body, headers=_auth(teacher))
    assert archived.status_code == 409, archived.text
    assert _error(archived) == "COURSE_ARCHIVED"


def test_retry_body_validated_after_state_check(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> None:
    """状态不可重试（409）优先于请求体结构（422）。"""
    suffix = uuid.uuid4().hex[:8]
    teacher, _course_id, job_id = _practice_job(
        client, fake_storage, pg_session_factory, make_settings, suffix
    )
    response = client.post(
        RETRY_URL.format(job_id=job_id),
        json={"unexpected": True},
        headers=_auth(teacher),
    )
    assert response.status_code == 409, response.text
    assert _error(response) == "JOB_NOT_RETRYABLE"


def test_job_status_never_exposes_internal_fields(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """响应只包含契约 10.0 的字段；内部调度字段不出现。"""
    suffix = uuid.uuid4().hex[:8]
    _course_id, teacher, material_id = _ready_material(
        client, fake_storage, pg_session_factory, make_settings, f"fields-{suffix}"
    )
    job_id = _job_id(pg_sync_engine, "MATERIAL_PARSE", material_id)

    payload = client.get(JOB_URL.format(job_id=job_id), headers=_auth(teacher)).json()

    assert set(payload) == EXPECTED_FIELDS
    assert not (INTERNAL_FIELDS & set(payload))
