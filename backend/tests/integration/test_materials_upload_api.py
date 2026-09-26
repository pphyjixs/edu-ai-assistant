"""课件上传初始化与完成接口集成测试（真实 PostgreSQL + 内存对象存储假实现）。

覆盖 plan 第 4 步的两组验收：

- 初始化：创建教师 201；学生 / 其他教师 / 归档课程 / 非法类型 / 超限文件得到约定错误；
  失败请求不生成可用上传会话。
- 完成：未上传、篡改元数据、过期会话、上传后归档、重复与并发完成；
  每个 ``upload_id`` 最多对应一份资料与一个任务。

对象存储用 :class:`tests.storage_fake.FakeStorage` 替换（真实适配器的 SigV4 与 HTTP
行为分别在 ``tests/unit/test_storage_signing.py`` 与
``tests/integration/test_storage_minio.py`` 验证）。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.error_codes import ErrorCode
from app.storage.deps import get_storage_dep
from tests.storage_fake import FakeStorage

PASSWORD = "Demo password 2026!"

UPLOADS_URL = "/api/v1/courses/{course_id}/materials/uploads"
COMPLETE_URL = "/api/v1/courses/{course_id}/materials/uploads/{upload_id}/complete"

#: 默认 PDF 载荷（内容本身不重要，摘要与声明一致即可）
BODY = b"%PDF-1.7 chapter one\n"
SHA256_HEX = hashlib.sha256(BODY).hexdigest()
SIZE = len(BODY)

PDF_MIME = "application/pdf"

#: 默认上限（Settings.material_max_upload_bytes）
DEFAULT_MAX_BYTES = 5 * 1024 * 1024


# --------------------------------------------------------------------------- #
# 夹具与辅助
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def client(
    db_isolation: None, pg_app, fake_storage: FakeStorage
) -> Iterator[TestClient]:
    """把对象存储依赖换成内存假实现。"""
    app = pg_app()
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
async def async_client(
    db_isolation: None, pg_app, fake_storage: FakeStorage
) -> AsyncIterator[httpx.AsyncClient]:
    """异步客户端（并发完成用例）。"""
    app = pg_app()
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=30.0
    ) as async_http_client:
        yield async_http_client


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


def _create_course(client, token: str) -> dict:
    response = client.post(
        "/api/v1/courses",
        json={"name": "软件工程实验", "description": "课程说明"},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _archive_course(client, token: str, course_id: str) -> None:
    response = client.post(
        f"/api/v1/courses/{course_id}/archive", headers=_auth(token)
    )
    assert response.status_code == 200, response.text


def _init_payload(**overrides: object) -> dict:
    payload: dict[str, object] = {
        "filename": "chapter-1.pdf",
        "content_type": PDF_MIME,
        "size": SIZE,
        "sha256": SHA256_HEX,
    }
    payload.update(overrides)
    return payload


def _init_upload(client, token: str, course_id: str, **overrides: object):
    return client.post(
        UPLOADS_URL.format(course_id=course_id),
        json=_init_payload(**overrides),
        headers=_auth(token),
    )


def _complete_upload(client, token: str, course_id: str, upload_id: str):
    return client.post(
        COMPLETE_URL.format(course_id=course_id, upload_id=upload_id),
        headers=_auth(token),
    )


def _object_key_of(fake: FakeStorage, upload_url: str) -> str:
    """由预签名地址反查对象键。

    响应里刻意不返回对象键，因此只能这样取——这也顺带断言了
    "预签名地址确实指向服务端生成的键"。
    """
    for key, url in fake.presigned_urls.items():
        if url == upload_url:
            return key
    raise AssertionError(f"假存储中没有该地址对应的对象键：{upload_url}")


def _simulate_upload(
    fake: FakeStorage,
    upload_url: str,
    *,
    size: int | None = None,
    content_type: str = PDF_MIME,
    sha256_hex: str | None = None,
) -> str:
    """模拟浏览器直传：把对象放进假存储，可指定与声明不同的元数据。"""
    key = _object_key_of(fake, upload_url)
    fake.store_object(
        key,
        size=SIZE if size is None else size,
        content_type=content_type,
        sha256_hex=SHA256_HEX if sha256_hex is None else sha256_hex,
    )
    return key


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        )


def _expire_session(engine: Engine, upload_id: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE material_upload_sessions"
                " SET confirm_deadline_at = now() - interval '1 minute'"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": upload_id},
        )


def _session_row(engine: Engine, upload_id: str) -> dict:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT object_key, filename, size, sha256, completed_material_id"
                    " FROM material_upload_sessions WHERE id = CAST(:id AS uuid)"
                ),
                {"id": upload_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


# --------------------------------------------------------------------------- #
# 初始化：成功路径
# --------------------------------------------------------------------------- #
def test_creator_teacher_initializes_upload(
    client: TestClient, fake_storage: FakeStorage, pg_sync_engine: Engine
) -> None:
    token = _teacher_token(client)
    course = _create_course(client, token)

    response = _init_upload(client, token, course["id"])

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["method"] == "PUT"
    assert body["headers"] == {
        "Content-Type": PDF_MIME,
        "If-None-Match": "*",
        "x-amz-checksum-sha256": base64.b64encode(bytes.fromhex(SHA256_HEX)).decode(),
    }
    # expires_at 是 PUT 地址的到期时间（10 分钟），confirm_deadline_at 是 24 小时
    expires_at = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    deadline_at = datetime.fromisoformat(body["confirm_deadline_at"].replace("Z", "+00:00"))
    delta = (deadline_at - expires_at).total_seconds()
    assert 23 * 3600 < delta < 24 * 3600
    assert expires_at.tzinfo is not None

    # 上传会话已落库：对象键由后端生成，不含用户文件名
    row = _session_row(pg_sync_engine, body["upload_id"])
    assert row["filename"] == "chapter-1.pdf"
    assert row["size"] == SIZE
    assert row["sha256"] == SHA256_HEX
    assert row["completed_material_id"] is None
    assert row["object_key"].startswith(f"courses/{course['id']}/uploads/")
    assert "chapter-1" not in row["object_key"]
    assert row["object_key"] in body["upload_url"]

    # 后端不接收文件内容：签发地址不会往存储里写任何对象
    assert fake_storage.objects == {}


def test_configured_size_limit_is_honored(client: TestClient, pg_app) -> None:
    """上限可配置：改小后按新上限校验，并在错误里回显。"""
    app = pg_app(material_max_upload_bytes=1024)
    app.dependency_overrides[get_storage_dep] = lambda: FakeStorage()
    with TestClient(app) as limited_client:
        token = _teacher_token(limited_client, "limit@example.com")
        course = _create_course(limited_client, token)

        response = _init_upload(limited_client, token, course["id"], size=2048)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == ErrorCode.UPLOAD_INVALID.value
    assert error["details"]["reason"] == "SIZE_OUT_OF_RANGE"
    assert error["details"]["max_size_bytes"] == 1024


# --------------------------------------------------------------------------- #
# 初始化：权限与归档
# --------------------------------------------------------------------------- #
def test_student_cannot_initialize(client: TestClient) -> None:
    teacher = _teacher_token(client)
    course = _create_course(client, teacher)
    student = _student_token(client, "student@example.com")

    response = _init_upload(client, student, course["id"])

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.ROLE_FORBIDDEN.value


def test_other_teacher_cannot_initialize(client: TestClient) -> None:
    owner = _teacher_token(client, "owner@example.com")
    course = _create_course(client, owner)
    other = _teacher_token(client, "other@example.com")

    response = _init_upload(client, other, course["id"])

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.COURSE_FORBIDDEN.value


def test_archived_course_cannot_initialize(client: TestClient) -> None:
    token = _teacher_token(client)
    course = _create_course(client, token)
    _archive_course(client, token, course["id"])

    response = _init_upload(client, token, course["id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.COURSE_ARCHIVED.value


def test_missing_course_returns_404(client: TestClient) -> None:
    token = _teacher_token(client)

    response = _init_upload(client, token, str(uuid.uuid4()))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == ErrorCode.RESOURCE_NOT_FOUND.value


def test_anonymous_cannot_initialize(client: TestClient) -> None:
    response = client.post(
        UPLOADS_URL.format(course_id=uuid.uuid4()), json=_init_payload()
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == ErrorCode.AUTH_TOKEN_EXPIRED.value


# --------------------------------------------------------------------------- #
# 初始化：元数据校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("overrides", "reason", "field_name"),
    [
        ({"filename": "chapter-1.txt"}, "FILE_TYPE_NOT_ALLOWED", "filename"),
        ({"filename": "chapter-1"}, "FILE_TYPE_NOT_ALLOWED", "filename"),
        ({"filename": "a/b.pdf"}, "FILE_TYPE_NOT_ALLOWED", "filename"),
        ({"filename": "   "}, "FILE_TYPE_NOT_ALLOWED", "filename"),
        ({"filename": "x" * 300 + ".pdf"}, "FILE_TYPE_NOT_ALLOWED", "filename"),
        (
            {"content_type": "application/octet-stream"},
            "CONTENT_TYPE_MISMATCH",
            "content_type",
        ),
        ({"content_type": "application/pdf; charset=utf-8"}, "CONTENT_TYPE_MISMATCH", "content_type"),
        (
            {"content_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation"},
            "CONTENT_TYPE_MISMATCH",
            "content_type",
        ),
        ({"size": 0}, "SIZE_OUT_OF_RANGE", "size"),
        ({"size": -1}, "SIZE_OUT_OF_RANGE", "size"),
        ({"size": DEFAULT_MAX_BYTES + 1}, "SIZE_OUT_OF_RANGE", "size"),
        ({"sha256": "abc"}, "SHA256_INVALID", "sha256"),
        ({"sha256": "z" * 64}, "SHA256_INVALID", "sha256"),
    ],
)
def test_invalid_metadata_returns_upload_invalid(
    client: TestClient, overrides: dict, reason: str, field_name: str
) -> None:
    token = _teacher_token(client)
    course = _create_course(client, token)

    response = _init_upload(client, token, course["id"], **overrides)

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == ErrorCode.UPLOAD_INVALID.value
    assert error["details"]["reason"] == reason
    assert error["details"]["field"] == field_name


@pytest.mark.parametrize(
    "payload",
    [
        {"filename": "a.pdf", "content_type": PDF_MIME, "size": SIZE},  # 缺 sha256
        {"filename": "a.pdf", "content_type": PDF_MIME, "sha256": SHA256_HEX},  # 缺 size
        {**_init_payload(), "size": "big"},  # 类型错误
        {**_init_payload(), "sha256": None},  # 显式 null
        {**_init_payload(), "unexpected": 1},  # 未声明字段
        {**_init_payload(), "filename": 123},  # 类型错误
    ],
)
def test_structurally_invalid_body_returns_validation_error(
    client: TestClient, payload: dict
) -> None:
    token = _teacher_token(client)
    course = _create_course(client, token)

    response = client.post(
        UPLOADS_URL.format(course_id=course["id"]),
        json=payload,
        headers=_auth(token),
    )

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value


def test_failed_requests_create_no_upload_session(
    client: TestClient, pg_sync_engine: Engine
) -> None:
    """失败请求不得留下可用的上传会话（也不能留下任何会话记录）。"""
    token = _teacher_token(client)
    course = _create_course(client, token)
    archived = _create_course(client, token)
    _archive_course(client, token, archived["id"])

    student = _student_token(client, "student2@example.com")
    other = _teacher_token(client, "other2@example.com")

    attempts = [
        _init_upload(client, student, course["id"]),  # 平台角色不符
        _init_upload(client, other, course["id"]),  # 不是创建教师
        _init_upload(client, token, archived["id"]),  # 归档课程
        _init_upload(client, token, course["id"], filename="a.exe"),  # 类型不支持
        _init_upload(client, token, course["id"], content_type="text/plain"),  # MIME 不符
        _init_upload(client, token, course["id"], size=DEFAULT_MAX_BYTES + 1),  # 超限
        _init_upload(client, token, course["id"], sha256="abc"),  # 摘要格式错误
        _init_upload(
            client, token, course["id"], **{"filename": "a.pdf", "size": None}
        ),  # 结构错误
        _init_upload(client, token, str(uuid.uuid4())),  # 课程不存在
    ]

    assert [response.status_code for response in attempts] == [
        403,
        403,
        409,
        422,
        422,
        422,
        422,
        422,
        404,
    ]
    assert _count(pg_sync_engine, "material_upload_sessions") == 0


def test_storage_unavailable_on_init_returns_503(
    client: TestClient, fake_storage: FakeStorage, pg_sync_engine: Engine
) -> None:
    token = _teacher_token(client)
    course = _create_course(client, token)
    fake_storage.as_unavailable("not_configured")

    response = _init_upload(client, token, course["id"])

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == ErrorCode.SERVICE_UNAVAILABLE.value
    assert error["details"]["component"] == "storage"
    assert _count(pg_sync_engine, "material_upload_sessions") == 0


# --------------------------------------------------------------------------- #
# 完成：成功路径与幂等
# --------------------------------------------------------------------------- #
def test_complete_creates_material_and_pending_job(
    client: TestClient, fake_storage: FakeStorage, pg_sync_engine: Engine
) -> None:
    token = _teacher_token(client)
    course = _create_course(client, token)
    init = _init_upload(client, token, course["id"]).json()
    _simulate_upload(fake_storage, init["upload_url"])

    response = _complete_upload(client, token, course["id"], init["upload_id"])

    assert response.status_code == 202, response.text
    body = response.json()

    material = body["material"]
    assert material["course_id"] == course["id"]
    assert material["filename"] == "chapter-1.pdf"
    assert material["content_type"] == PDF_MIME
    assert material["size"] == SIZE
    # 第一版没有 Worker：资料停在 PROCESSING
    assert material["status"] == "PROCESSING"
    assert material["error_message"] is None

    job = body["job"]
    assert job["type"] == "MATERIAL_PARSE"
    # 任务如实停在 PENDING，进度 0，未开始也未结束
    assert job["status"] == "PENDING"
    assert job["progress"] == 0
    assert job["resource_type"] == "MATERIAL"
    assert job["resource_id"] == material["id"]
    assert job["error"] is None
    assert job["started_at"] is None
    assert job["finished_at"] is None

    # 上传会话记录了完成结果
    row = _session_row(pg_sync_engine, init["upload_id"])
    assert str(row["completed_material_id"]) == material["id"]
    assert _count(pg_sync_engine, "materials") == 1
    assert _count(pg_sync_engine, "jobs") == 1


def test_repeat_complete_returns_same_result(
    client: TestClient, fake_storage: FakeStorage, pg_sync_engine: Engine
) -> None:
    token = _teacher_token(client)
    course = _create_course(client, token)
    init = _init_upload(client, token, course["id"]).json()
    _simulate_upload(fake_storage, init["upload_url"])

    first = _complete_upload(client, token, course["id"], init["upload_id"])
    second = _complete_upload(client, token, course["id"], init["upload_id"])

    assert first.status_code == second.status_code == 202
    assert second.json() == first.json()
    assert _count(pg_sync_engine, "materials") == 1
    assert _count(pg_sync_engine, "jobs") == 1


def test_repeat_complete_after_deadline_still_idempotent(
    client: TestClient, fake_storage: FakeStorage, pg_sync_engine: Engine
) -> None:
    """已完成会话不受确认窗口限制（契约 4.6）。"""
    token = _teacher_token(client)
    course = _create_course(client, token)
    init = _init_upload(client, token, course["id"]).json()
    _simulate_upload(fake_storage, init["upload_url"])
    first = _complete_upload(client, token, course["id"], init["upload_id"])

    _expire_session(pg_sync_engine, init["upload_id"])
    second = _complete_upload(client, token, course["id"], init["upload_id"])

    assert second.status_code == 202, second.text
    assert second.json()["material"]["id"] == first.json()["material"]["id"]


def test_complete_without_stored_checksum_succeeds(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    """存储侧不返回校验值时只记日志，不因此拒绝（契约 4.5）。"""
    token = _teacher_token(client)
    course = _create_course(client, token)
    init = _init_upload(client, token, course["id"]).json()
    _simulate_upload(fake_storage, init["upload_url"])
    fake_storage.hide_checksum = True

    response = _complete_upload(client, token, course["id"], init["upload_id"])

    assert response.status_code == 202, response.text


# --------------------------------------------------------------------------- #
# 完成：失败场景
# --------------------------------------------------------------------------- #
def test_complete_rejects_undeclared_body_fields(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    """完成接口没有请求字段：多余字段必须拒绝，不能静默忽略。"""
    token = _teacher_token(client)
    course = _create_course(client, token)
    init = _init_upload(client, token, course["id"]).json()
    _simulate_upload(fake_storage, init["upload_url"])

    url = COMPLETE_URL.format(course_id=course["id"], upload_id=init["upload_id"])
    rejected = client.post(url, json={"unexpected": 1}, headers=_auth(token))
    accepted = client.post(url, json={}, headers=_auth(token))

    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == ErrorCode.VALIDATION_ERROR.value
    assert accepted.status_code == 202, accepted.text


def test_complete_without_uploaded_object(
    client: TestClient, pg_sync_engine: Engine
) -> None:
    token = _teacher_token(client)
    course = _create_course(client, token)
    init = _init_upload(client, token, course["id"]).json()

    response = _complete_upload(client, token, course["id"], init["upload_id"])

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == ErrorCode.UPLOAD_INVALID.value
    assert error["details"]["reason"] == "OBJECT_MISSING"
    assert _count(pg_sync_engine, "materials") == 0
    assert _count(pg_sync_engine, "jobs") == 0


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"size": SIZE + 10}, "OBJECT_SIZE_MISMATCH"),
        ({"content_type": "application/octet-stream"}, "OBJECT_TYPE_MISMATCH"),
        ({"sha256_hex": hashlib.sha256(b"tampered").hexdigest()}, "CHECKSUM_MISMATCH"),
    ],
)
def test_tampered_object_metadata_is_rejected(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_sync_engine: Engine,
    kwargs: dict,
    reason: str,
) -> None:
    token = _teacher_token(client)
    course = _create_course(client, token)
    init = _init_upload(client, token, course["id"]).json()
    _simulate_upload(fake_storage, init["upload_url"], **kwargs)

    response = _complete_upload(client, token, course["id"], init["upload_id"])

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == ErrorCode.UPLOAD_INVALID.value
    assert error["details"]["reason"] == reason
    # 失败不产生资料与任务
    assert _count(pg_sync_engine, "materials") == 0
    assert _count(pg_sync_engine, "jobs") == 0


def test_complete_after_confirm_deadline(
    client: TestClient, fake_storage: FakeStorage, pg_sync_engine: Engine
) -> None:
    token = _teacher_token(client)
    course = _create_course(client, token)
    init = _init_upload(client, token, course["id"]).json()
    _simulate_upload(fake_storage, init["upload_url"])
    _expire_session(pg_sync_engine, init["upload_id"])

    response = _complete_upload(client, token, course["id"], init["upload_id"])

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["details"]["reason"] == "UPLOAD_EXPIRED"
    assert _count(pg_sync_engine, "materials") == 0


def test_complete_after_course_archived(
    client: TestClient, fake_storage: FakeStorage, pg_sync_engine: Engine
) -> None:
    """上传成功后课程被归档：完成请求返回 409，且不创建资料。"""
    token = _teacher_token(client)
    course = _create_course(client, token)
    init = _init_upload(client, token, course["id"]).json()
    _simulate_upload(fake_storage, init["upload_url"])
    _archive_course(client, token, course["id"])

    response = _complete_upload(client, token, course["id"], init["upload_id"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == ErrorCode.COURSE_ARCHIVED.value
    assert _count(pg_sync_engine, "materials") == 0
    assert _count(pg_sync_engine, "jobs") == 0


def test_complete_permission_and_lookup_errors(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    owner = _teacher_token(client, "owner3@example.com")
    course = _create_course(client, owner)
    other = _teacher_token(client, "other3@example.com")
    student = _student_token(client, "student3@example.com")
    init = _init_upload(client, owner, course["id"]).json()
    _simulate_upload(fake_storage, init["upload_url"])

    # 学生：平台角色不符
    as_student = _complete_upload(client, student, course["id"], init["upload_id"])
    assert as_student.status_code == 403
    assert as_student.json()["error"]["code"] == ErrorCode.ROLE_FORBIDDEN.value

    # 其他教师：不是创建教师
    assert _complete_upload(
        client, other, course["id"], init["upload_id"]
    ).status_code == 403

    # 不存在的上传会话
    missing = _complete_upload(client, owner, course["id"], str(uuid.uuid4()))
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == ErrorCode.RESOURCE_NOT_FOUND.value

    # 会话不属于路径中的课程
    other_course = _create_course(client, owner)
    crossed = _complete_upload(
        client, owner, other_course["id"], init["upload_id"]
    )
    assert crossed.status_code == 404


def test_storage_unavailable_on_complete_returns_503(
    client: TestClient, fake_storage: FakeStorage, pg_sync_engine: Engine
) -> None:
    token = _teacher_token(client)
    course = _create_course(client, token)
    init = _init_upload(client, token, course["id"]).json()
    _simulate_upload(fake_storage, init["upload_url"])
    fake_storage.as_unavailable("timeout")

    response = _complete_upload(client, token, course["id"], init["upload_id"])

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == ErrorCode.SERVICE_UNAVAILABLE.value
    assert error["details"] == {"component": "storage", "reason": "timeout"}
    assert _count(pg_sync_engine, "materials") == 0


# --------------------------------------------------------------------------- #
# 完成：并发
# --------------------------------------------------------------------------- #
async def test_concurrent_complete_creates_single_material_and_job(
    async_client: httpx.AsyncClient, fake_storage: FakeStorage, pg_sync_engine: Engine
) -> None:
    """并发重复完成：全部返回 202，且只产生一份资料与一个任务。"""
    register = await async_client.post(
        "/api/v1/auth/register",
        json={
            "email": "race@example.com",
            "password": PASSWORD,
            "display_name": "张老师",
            "role": "TEACHER",
        },
    )
    assert register.status_code == 201, register.text
    token = (
        await async_client.post(
            "/api/v1/auth/login",
            json={"email": "race@example.com", "password": PASSWORD},
        )
    ).json()["access_token"]
    course = (
        await async_client.post(
            "/api/v1/courses",
            json={"name": "并发课程", "description": ""},
            headers=_auth(token),
        )
    ).json()
    init = (
        await async_client.post(
            UPLOADS_URL.format(course_id=course["id"]),
            json=_init_payload(),
            headers=_auth(token),
        )
    ).json()
    _simulate_upload(fake_storage, init["upload_url"])

    responses = await asyncio.gather(
        *[
            async_client.post(
                COMPLETE_URL.format(
                    course_id=course["id"], upload_id=init["upload_id"]
                ),
                headers=_auth(token),
            )
            for _ in range(8)
        ]
    )

    assert [response.status_code for response in responses] == [202] * 8
    material_ids = {response.json()["material"]["id"] for response in responses}
    job_ids = {response.json()["job"]["id"] for response in responses}
    assert len(material_ids) == 1
    assert len(job_ids) == 1

    assert _count(pg_sync_engine, "materials") == 1
    assert _count(pg_sync_engine, "jobs") == 1
    row = _session_row(pg_sync_engine, init["upload_id"])
    assert str(row["completed_material_id"]) == next(iter(material_ids))
