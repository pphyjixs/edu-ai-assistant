"""任务重试接口的关联资源可见性与权限优先级（契约 10.1）。

回归：非成员不能借 ``POST /jobs/{job_id}/retry`` 探测任务是否存在或其类型。
``MATERIAL_PARSE`` 任务也必须先按关联资料做可见性（404）→ 角色（403）→
归档（409），最后才返回 ``409 JOB_NOT_RETRYABLE``。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.integration.test_chat_api import _make_chat_client
from tests.integration.test_practice_api import RETRY_URL, _ready_course, practice_model_factory
from tests.integration.test_materials_api import _auth, _login, _register
from tests.integration.test_materials_api import fake_storage as fake_storage  # noqa: F401


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage) -> Iterator[TestClient]:
    with _make_chat_client(
        db_isolation, pg_app, fake_storage, practice_model_factory()
    ) as test_client:
        yield test_client


def _parse_job_id(pg_sync_engine, material_id: str) -> str:
    with pg_sync_engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT id FROM jobs"
                " WHERE type = 'MATERIAL_PARSE' AND resource_id = CAST(:id AS uuid)"
            ),
            {"id": material_id},
        ).scalar_one()


def _error(response) -> str:
    return response.json()["error"]["code"]


def test_non_member_cannot_probe_material_parse_job(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """非成员 + MATERIAL_PARSE 任务 → 404 RESOURCE_NOT_FOUND（而非 409）。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"rv-nonmember-{suffix}@example.com",
    )
    job_id = _parse_job_id(pg_sync_engine, material_id)

    outsider_email = f"rv-outsider-{suffix}@example.com"
    _register(client, outsider_email, "TEACHER")
    outsider = _login(client, outsider_email)

    response = client.post(RETRY_URL.format(job_id=job_id), headers=_auth(outsider))

    assert response.status_code == 404, response.text
    assert _error(response) == "RESOURCE_NOT_FOUND"


def test_student_member_gets_role_forbidden_for_material_parse_job(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """学生课程成员 + MATERIAL_PARSE 任务 → 403 ROLE_FORBIDDEN。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"rv-student-{suffix}@example.com",
    )
    job_id = _parse_job_id(pg_sync_engine, material_id)

    student_email = f"rv-student-{suffix}-s@example.com"
    _register(client, student_email, "STUDENT")
    student = _login(client, student_email)
    detail = client.get(f"/api/v1/courses/{course_id}", headers=_auth(teacher))
    joined = client.post(
        "/api/v1/courses/join",
        json={"invite_code": detail.json()["invite_code"]},
        headers=_auth(student),
    )
    assert joined.status_code == 201

    response = client.post(RETRY_URL.format(job_id=job_id), headers=_auth(student))

    assert response.status_code == 403, response.text
    assert _error(response) == "ROLE_FORBIDDEN"


def test_creator_teacher_gets_job_not_retryable_for_material_parse_job(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """活动课程的创建教师 + MATERIAL_PARSE 任务 → 409 JOB_NOT_RETRYABLE。"""
    suffix = uuid.uuid4().hex[:8]
    _course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"rv-creator-{suffix}@example.com",
    )
    job_id = _parse_job_id(pg_sync_engine, material_id)

    response = client.post(RETRY_URL.format(job_id=job_id), headers=_auth(teacher))

    assert response.status_code == 409, response.text
    assert _error(response) == "JOB_NOT_RETRYABLE"


def test_archived_course_creator_teacher_gets_course_archived(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """归档课程的创建教师 + MATERIAL_PARSE 任务 → 409 COURSE_ARCHIVED。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"rv-archived-{suffix}@example.com",
    )
    job_id = _parse_job_id(pg_sync_engine, material_id)

    archived = client.post(
        f"/api/v1/courses/{course_id}/archive", headers=_auth(teacher)
    )
    assert archived.status_code == 200

    response = client.post(RETRY_URL.format(job_id=job_id), headers=_auth(teacher))

    assert response.status_code == 409, response.text
    assert _error(response) == "COURSE_ARCHIVED"
