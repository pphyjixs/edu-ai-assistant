"""资料详情与任务状态查询接口集成测试（真实 PostgreSQL + 内存对象存储假实现）。

覆盖契约 4.7 的两个状态查询接口：

- ``GET /materials/{material_id}``：课程成员可读（教师与学生），非成员 / 不存在统一 404；
- ``GET /jobs/{job_id}``：``MATERIAL_PARSE`` 任务可见性等同于资料所属课程成员。

两者都验证：匿名 401、成员 200 且状态符合契约（资料 PROCESSING、任务 PENDING）、
非成员与不存在 404、归档课程仍可读、且响应不含内部字段（storage_key / object_key）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.error_codes import ErrorCode
from app.storage.deps import get_storage_dep
from tests.storage_fake import FakeStorage

PASSWORD = "Demo password 2026!"

MATERIAL_URL = "/api/v1/materials/{material_id}"
JOB_URL = "/api/v1/jobs/{job_id}"


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def client(
    db_isolation: None, pg_app, fake_storage: FakeStorage
) -> Iterator[TestClient]:
    app = pg_app()
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    with TestClient(app) as test_client:
        yield test_client


def _register(client, email: str, role: str, *, name: str) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "display_name": name,
            "role": role,
        },
    )
    assert response.status_code == 201, response.text


def _login(client, email: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _teacher_token(client, email: str = "teacher@example.com") -> str:
    _register(client, email, "TEACHER", name="张老师")
    return _login(client, email)


def _student_token(client, email: str) -> str:
    _register(client, email, "STUDENT", name="学生")
    return _login(client, email)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_course(client, token: str, name: str = "软件工程实验") -> dict:
    response = client.post(
        "/api/v1/courses",
        json={"name": name, "description": ""},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _join_course(client, token: str, invite_code: str) -> None:
    response = client.post(
        "/api/v1/courses/join", json={"invite_code": invite_code}, headers=_auth(token)
    )
    assert response.status_code in (200, 201), response.text


def _upload_material(
    client, token: str, course_id: str, fake_storage: FakeStorage
) -> dict:
    """走完整上传流程：初始化 → 模拟直传 → 完成，返回完成响应体。"""
    import hashlib

    body = b"%PDF-1.7 query test\n"
    sha256 = hashlib.sha256(body).hexdigest()
    init = client.post(
        f"/api/v1/courses/{course_id}/materials/uploads",
        json={
            "filename": "chapter-1.pdf",
            "content_type": "application/pdf",
            "size": len(body),
            "sha256": sha256,
        },
        headers=_auth(token),
    ).json()

    # 反查对象键并放入假存储
    for key, url in fake_storage.presigned_urls.items():
        if url == init["upload_url"]:
            fake_storage.store_object(
                key,
                size=len(body),
                content_type="application/pdf",
                sha256_hex=sha256,
            )
            break

    response = client.post(
        f"/api/v1/courses/{course_id}/materials/uploads/{init['upload_id']}/complete",
        headers=_auth(token),
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_member_can_read_material_and_job(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    teacher = _teacher_token(client)
    course = _create_course(client, teacher)
    completed = _upload_material(client, teacher, course["id"], fake_storage)

    material_id = completed["material"]["id"]
    job_id = completed["job"]["id"]

    # 教师（创建教师）可读资料详情
    material_resp = client.get(
        MATERIAL_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert material_resp.status_code == 200, material_resp.text
    material = material_resp.json()
    assert material["id"] == material_id
    assert material["course_id"] == course["id"]
    assert material["status"] == "PROCESSING"
    assert material["error_message"] is None
    # 内部字段不泄露
    assert "storage_key" not in material
    assert "object_key" not in material

    # 教师可读任务状态
    job_resp = client.get(JOB_URL.format(job_id=job_id), headers=_auth(teacher))
    assert job_resp.status_code == 200, job_resp.text
    job = job_resp.json()
    assert job["id"] == job_id
    assert job["type"] == "MATERIAL_PARSE"
    assert job["status"] == "PENDING"
    assert job["progress"] == 0
    assert job["resource_type"] == "MATERIAL"
    assert job["resource_id"] == material_id


def test_student_member_can_read(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    teacher = _teacher_token(client)
    course = _create_course(client, teacher)
    student = _student_token(client, "student@example.com")
    _join_course(client, student, course["invite_code"])

    completed = _upload_material(client, teacher, course["id"], fake_storage)
    material_id = completed["material"]["id"]
    job_id = completed["job"]["id"]

    assert (
        client.get(
            MATERIAL_URL.format(material_id=material_id), headers=_auth(student)
        ).status_code
        == 200
    )
    assert (
        client.get(JOB_URL.format(job_id=job_id), headers=_auth(student)).status_code
        == 200
    )


def test_non_member_gets_404_for_material_and_job(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    """非成员与「不存在」统一返回 404，不区分两者，避免枚举资源。"""
    teacher = _teacher_token(client)
    course = _create_course(client, teacher)
    completed = _upload_material(client, teacher, course["id"], fake_storage)
    material_id = completed["material"]["id"]
    job_id = completed["job"]["id"]

    # 一个未加入课程的学生，以及另一个教师（非成员）
    outsider_student = _student_token(client, "outsider-student@example.com")
    outsider_teacher = _teacher_token(client, "outsider-teacher@example.com")

    for outsider in (outsider_student, outsider_teacher):
        material_resp = client.get(
            MATERIAL_URL.format(material_id=material_id), headers=_auth(outsider)
        )
        assert material_resp.status_code == 404, material_resp.text
        assert (
            material_resp.json()["error"]["code"]
            == ErrorCode.RESOURCE_NOT_FOUND.value
        )

        job_resp = client.get(JOB_URL.format(job_id=job_id), headers=_auth(outsider))
        assert job_resp.status_code == 404, job_resp.text
        assert job_resp.json()["error"]["code"] == ErrorCode.RESOURCE_NOT_FOUND.value


def test_missing_material_or_job_returns_404(client: TestClient) -> None:
    token = _teacher_token(client)
    assert (
        client.get(
            MATERIAL_URL.format(material_id=uuid.uuid4()), headers=_auth(token)
        ).status_code
        == 404
    )
    assert (
        client.get(JOB_URL.format(job_id=uuid.uuid4()), headers=_auth(token)).status_code
        == 404
    )


def test_anonymous_cannot_read(client: TestClient) -> None:
    assert client.get(MATERIAL_URL.format(material_id=uuid.uuid4())).status_code == 401
    assert client.get(JOB_URL.format(job_id=uuid.uuid4())).status_code == 401


def test_archived_course_material_and_job_still_readable(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    """归档课程的资料与任务仍可读（契约 4.7）。"""
    teacher = _teacher_token(client)
    course = _create_course(client, teacher)
    completed = _upload_material(client, teacher, course["id"], fake_storage)
    material_id = completed["material"]["id"]
    job_id = completed["job"]["id"]

    assert (
        client.post(
            f"/api/v1/courses/{course['id']}/archive", headers=_auth(teacher)
        ).status_code
        == 200
    )

    assert (
        client.get(
            MATERIAL_URL.format(material_id=material_id), headers=_auth(teacher)
        ).status_code
        == 200
    )
    assert (
        client.get(JOB_URL.format(job_id=job_id), headers=_auth(teacher)).status_code
        == 200
    )
