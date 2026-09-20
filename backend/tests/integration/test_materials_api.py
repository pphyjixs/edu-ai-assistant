"""课程资料接口集成测试（契约 5.1–5.5，真实 PostgreSQL + 内存对象存储）。

覆盖四个接口与解析 Worker：

- 列表（5.1）：成员可读、分页与排序、归档可读、不含已删除、非成员 404；
- 删除（5.2）：仅创建教师、归档 409、幂等 204、删除后所有读路径 404、对象被删；
- 重试解析（5.3）：失败 → 202 复用 job ID、就绪 → 409、处理中幂等 202；
- 大纲查询（5.4）：READY 200、PROCESSING 409、FAILED 502、删除后 404；
- Worker（5.5）：完成上传后推进 PENDING → RUNNING → SUCCEEDED 与
  PROCESSING → READY，章节/知识点落库；解析失败推进到 FAILED。
"""

from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.storage.deps import get_storage_dep
from tests.storage_fake import FakeStorage

PASSWORD = "Demo password 2026!"

PDF_MIME = "application/pdf"
PPTX_MIME = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

MATERIALS_URL = "/api/v1/courses/{course_id}/materials"
DELETE_URL = "/api/v1/materials/{material_id}"
PARSE_URL = "/api/v1/materials/{material_id}/parse"
OUTLINE_URL = "/api/v1/materials/{material_id}/outline"


# --------------------------------------------------------------------------- #
# 构造可解析的字节流
# --------------------------------------------------------------------------- #
def build_docx(paragraphs: list[tuple[str, str | None]]) -> bytes:
    """最小 DOCX：``(文本, Heading 样式或 None)`` 列表。"""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = ""
    for value, style in paragraphs:
        style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body += f"<w:p>{style_xml}<w:r><w:t>{value}</w:t></w:r></w:p>"
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


def build_pptx(slides: list[list[str]]) -> bytes:
    """最小 PPTX：每张幻灯片是一组文本块。"""
    ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index, texts in enumerate(slides, start=1):
            paragraphs = "".join(
                f"<a:p><a:r><a:t>{value}</a:t></a:r></a:p>" for value in texts
            )
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<p:sld xmlns:a="{ns}" xmlns:p="urn:x">'
                f"<p:cSld>{paragraphs}</p:cSld></p:sld>",
            )
    return buffer.getvalue()


#: 可解析的 DOCX 载荷（两个章节，含知识点）
PARSEABLE_DOCX = build_docx(
    [
        ("第一章 绪论", "Heading1"),
        ("软件工程是应用系统化的方法。", None),
        ("第二章 需求分析", "Heading1"),
        ("需求分析是软件生命周期的起点。", None),
    ]
)
PARSEABLE_SHA256 = hashlib.sha256(PARSEABLE_DOCX).hexdigest()
PARSEABLE_SIZE = len(PARSEABLE_DOCX)

#: 无法解析的载荷（不是合法文件）
UNPARSEABLE_DOCX = b"this is not a docx file"
UNPARSEABLE_SHA256 = hashlib.sha256(UNPARSEABLE_DOCX).hexdigest()


# --------------------------------------------------------------------------- #
# 夹具与辅助
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


def _make_client(
    db_isolation: None, pg_app, fake_storage: FakeStorage, *, worker: bool
) -> TestClient:
    """构造应用；``worker=True`` 时启用解析 Worker（契约 5.5 内联实现）。"""
    overrides = (
        {"material_parse_worker_enabled": True} if worker else {}
    )
    app = pg_app(**overrides)
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    return TestClient(app)


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage: FakeStorage) -> Iterator[TestClient]:
    """Worker 关闭：资料停留在 PROCESSING，供列表/删除/大纲分流测试。"""
    with _make_client(db_isolation, pg_app, fake_storage, worker=False) as test_client:
        yield test_client


@pytest.fixture
def worker_client(
    db_isolation: None, pg_app, fake_storage: FakeStorage
) -> Iterator[TestClient]:
    """Worker 开启：完成上传/重试后解析在响应返回前执行完毕。"""
    with _make_client(db_isolation, pg_app, fake_storage, worker=True) as test_client:
        yield test_client


def _register(client: TestClient, email: str, role: str) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": PASSWORD,
            "display_name": "用户",
            "role": role,
        },
    )
    assert response.status_code == 201, response.text


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_course(client: TestClient, token: str) -> str:
    response = client.post(
        "/api/v1/courses",
        json={"name": "软件工程实验", "description": "课程说明"},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _archive_course(client: TestClient, token: str, course_id: str) -> None:
    response = client.post(
        f"/api/v1/courses/{course_id}/archive", headers=_auth(token)
    )
    assert response.status_code == 200, response.text


def _object_key_of(fake: FakeStorage, upload_url: str) -> str:
    for key, url in fake.presigned_urls.items():
        if url == upload_url:
            return key
    raise AssertionError(f"假存储中没有该地址对应的对象键：{upload_url}")


def _join_course(
    client: TestClient, token: str, course_id: str, invite_code: str
) -> None:
    """用邀请码把用户拉进课程（成员资格前提）。"""
    response = client.post(
        "/api/v1/courses/join",
        json={"invite_code": invite_code},
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text


def _upload(
    client: TestClient,
    fake: FakeStorage,
    token: str,
    course_id: str,
    *,
    filename: str = "chapter-1.docx",
    content_type: str = DOCX_MIME,
    content: bytes = PARSEABLE_DOCX,
) -> dict:
    """完整走一遍初始化 → 模拟直传 → 完成；返回完成响应的 JSON。"""
    sha256 = hashlib.sha256(content).hexdigest()
    response = client.post(
        MATERIALS_URL.format(course_id=course_id) + "/uploads",
        json={
            "filename": filename,
            "content_type": content_type,
            "size": len(content),
            "sha256": sha256,
        },
        headers=_auth(token),
    )
    assert response.status_code == 201, response.text
    presigned = response.json()
    fake.store_object(
        _object_key_of(fake, presigned["upload_url"]),
        size=len(content),
        content_type=content_type,
        sha256_hex=sha256,
        content=content,
    )
    completed = client.post(
        MATERIALS_URL.format(course_id=course_id)
        + f"/uploads/{presigned['upload_id']}/complete",
        headers=_auth(token),
    )
    assert completed.status_code == 202, completed.text
    return completed.json()


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
        )


# --------------------------------------------------------------------------- #
# 5.1 资料列表
# --------------------------------------------------------------------------- #
def test_list_requires_membership_and_returns_ordered_page(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    _register(client, "student@example.com", "STUDENT")
    teacher = _login(client, "teacher@example.com")
    student = _login(client, "student@example.com")
    course_id = _create_course(client, teacher)

    _upload(client, fake_storage, teacher, course_id, filename="first.docx")
    _upload(client, fake_storage, teacher, course_id, filename="second.docx")

    # 成员可读：倒序（后上传的在前）
    response = client.get(
        MATERIALS_URL.format(course_id=course_id), headers=_auth(teacher)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["filename"] for item in body["items"]] == ["second.docx", "first.docx"]
    assert all(item["status"] == "PROCESSING" for item in body["items"])
    assert set(body["items"][0]) == {
        "id",
        "course_id",
        "filename",
        "content_type",
        "size",
        "status",
        "uploaded_by",
        "error_message",
        "created_at",
        "updated_at",
    }

    # 分页参数生效
    paged = client.get(
        MATERIALS_URL.format(course_id=course_id),
        params={"page": 2, "page_size": 1},
        headers=_auth(teacher),
    )
    assert paged.status_code == 200
    assert paged.json()["total"] == 2
    assert [item["filename"] for item in paged.json()["items"]] == ["first.docx"]

    # 匿名 401；非成员 404；课程不存在 404
    assert client.get(MATERIALS_URL.format(course_id=course_id)).status_code == 401
    forbidden = client.get(
        MATERIALS_URL.format(course_id=course_id), headers=_auth(student)
    )
    assert forbidden.status_code == 404
    assert forbidden.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    missing = client.get(
        MATERIALS_URL.format(course_id=str(uuid.uuid4())), headers=_auth(teacher)
    )
    assert missing.status_code == 404


def test_list_rejects_invalid_pagination(client: TestClient) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)

    for params in ({"page": 0}, {"page_size": 0}, {"page_size": 101}):
        response = client.get(
            MATERIALS_URL.format(course_id=course_id),
            params=params,
            headers=_auth(teacher),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_list_includes_archived_course_and_excludes_deleted(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    uploaded = _upload(client, fake_storage, teacher, course_id)
    material_id = uploaded["material"]["id"]

    # 删除必须在归档前：归档课程的删除被 409 拒绝（契约 5.2）
    deleted = client.delete(
        DELETE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert deleted.status_code == 204

    _archive_course(client, teacher, course_id)
    # 归档课程的列表仍可读，且不含已删除资料
    response = client.get(
        MATERIALS_URL.format(course_id=course_id), headers=_auth(teacher)
    )
    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert response.json()["items"] == []


# --------------------------------------------------------------------------- #
# 5.2 删除资料
# --------------------------------------------------------------------------- #
def test_delete_by_creator_is_idempotent_and_hides_material(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    uploaded = _upload(client, fake_storage, teacher, course_id)
    material_id = uploaded["material"]["id"]
    job_id = uploaded["job"]["id"]

    first = client.delete(
        DELETE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert first.status_code == 204
    assert first.text == ""

    # 幂等：同一教师重复删除 → 204
    repeat = client.delete(
        DELETE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert repeat.status_code == 204

    # 对象存储中的文件被尽力删除
    assert fake_storage.objects == {}

    # 详情、任务、大纲、重试全部 404
    assert (
        client.get(
            f"/api/v1/materials/{material_id}", headers=_auth(teacher)
        ).status_code
        == 404
    )
    assert (
        client.get(f"/api/v1/jobs/{job_id}", headers=_auth(teacher)).status_code == 404
    )
    assert (
        client.get(
            OUTLINE_URL.format(material_id=material_id), headers=_auth(teacher)
        ).status_code
        == 404
    )
    assert (
        client.post(
            PARSE_URL.format(material_id=material_id), headers=_auth(teacher)
        ).status_code
        == 404
    )


def test_delete_permissions_and_archived_conflict(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    _register(client, "student@example.com", "STUDENT")
    teacher = _login(client, "teacher@example.com")
    student = _login(client, "student@example.com")

    course_id = _create_course(client, teacher)
    uploaded = _upload(client, fake_storage, teacher, course_id)
    material_id = uploaded["material"]["id"]

    # 非成员学生：404（先判成员资格，不泄露资料存在性）
    non_member = client.delete(
        DELETE_URL.format(material_id=material_id), headers=_auth(student)
    )
    assert non_member.status_code == 404

    # 学生加入课程后：403 ROLE_FORBIDDEN
    detail = client.get(f"/api/v1/courses/{course_id}", headers=_auth(teacher))
    _join_course(client, student, course_id, detail.json()["invite_code"])
    student_response = client.delete(
        DELETE_URL.format(material_id=material_id), headers=_auth(student)
    )
    assert student_response.status_code == 403
    assert student_response.json()["error"]["code"] == "ROLE_FORBIDDEN"

    # 匿名 401
    assert client.delete(DELETE_URL.format(material_id=material_id)).status_code == 401

    # 归档后首次删除 → 409；资料未删除，重复仍 409
    _archive_course(client, teacher, course_id)
    archived = client.delete(
        DELETE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert archived.status_code == 409
    assert archived.json()["error"]["code"] == "COURSE_ARCHIVED"


def test_deleted_material_is_invisible_to_others(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    _register(client, "student@example.com", "STUDENT")
    teacher = _login(client, "teacher@example.com")
    student = _login(client, "student@example.com")
    course_id = _create_course(client, teacher)
    uploaded = _upload(client, fake_storage, teacher, course_id)
    material_id = uploaded["material"]["id"]

    detail = client.get(f"/api/v1/courses/{course_id}", headers=_auth(teacher))
    _join_course(client, student, course_id, detail.json()["invite_code"])

    client.delete(DELETE_URL.format(material_id=material_id), headers=_auth(teacher))

    # 学生对已删除资料：读 404；删除请求也 404（视同不存在，契约 5.2）
    assert (
        client.get(
            f"/api/v1/materials/{material_id}", headers=_auth(student)
        ).status_code
        == 404
    )
    student_delete = client.delete(
        DELETE_URL.format(material_id=material_id), headers=_auth(student)
    )
    assert student_delete.status_code == 404


# --------------------------------------------------------------------------- #
# 5.3 重试解析
# --------------------------------------------------------------------------- #
def test_retry_reuses_job_id_after_failure(
    worker_client: TestClient, fake_storage: FakeStorage
) -> None:
    client = worker_client
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)

    completed = _upload(
        client, fake_storage, teacher, course_id, content=UNPARSEABLE_DOCX
    )
    material_id = completed["material"]["id"]
    failed_job_id = completed["job"]["id"]

    detail = client.get(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert detail.json()["status"] == "FAILED"
    assert detail.json()["error_message"]

    # 重试：202、复用原 job ID、任务重置为 PENDING
    retried = client.post(
        PARSE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert retried.status_code == 202
    job = retried.json()
    assert job["id"] == failed_job_id
    assert job["type"] == "MATERIAL_PARSE"
    assert job["resource_id"] == material_id
    assert job["status"] == "PENDING"
    assert job["progress"] == 0
    assert job["error"] is None
    assert job["started_at"] is None
    assert job["finished_at"] is None

    # 同步 Worker 在响应后立即重新解析：内容仍无法解析 → 再次 FAILED。
    # 重置动作本身（PENDING、清空 error、复用 ID）已由上面的响应断言验证。
    detail = client.get(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert detail.json()["status"] == "FAILED"

    # 学生与非成员：403 / 404
    _register(client, "student@example.com", "STUDENT")
    student = _login(client, "student@example.com")
    assert (
        client.post(PARSE_URL.format(material_id=material_id), headers=_auth(student)).status_code
        == 404
    )


def test_retry_on_ready_returns_409(
    worker_client: TestClient, fake_storage: FakeStorage
) -> None:
    client = worker_client
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(client, fake_storage, teacher, course_id)
    material_id = completed["material"]["id"]

    detail = client.get(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert detail.json()["status"] == "READY"

    response = client.post(
        PARSE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MATERIAL_ALREADY_READY"


def test_retry_while_processing_is_idempotent(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    """任务 PENDING / 资料 PROCESSING 时重复调用返回原样任务（契约 5.3）。"""
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(client, fake_storage, teacher, course_id)
    material_id = completed["material"]["id"]
    original_job = completed["job"]

    response = client.post(
        PARSE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert response.status_code == 202
    job = response.json()
    # 原样返回：同一 ID、同一状态，时间戳不被重置
    assert job == original_job

    # 归档课程的重试 → 409 COURSE_ARCHIVED
    _archive_course(client, teacher, course_id)
    archived = client.post(
        PARSE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert archived.status_code == 409
    assert archived.json()["error"]["code"] == "COURSE_ARCHIVED"


# --------------------------------------------------------------------------- #
# 5.4 大纲查询
# --------------------------------------------------------------------------- #
def test_outline_not_ready_returns_409(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(client, fake_storage, teacher, course_id)
    material_id = completed["material"]["id"]

    response = client.get(
        OUTLINE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MATERIAL_NOT_READY"

    # 匿名 401；不存在 404
    assert client.get(OUTLINE_URL.format(material_id=material_id)).status_code == 401
    missing = client.get(
        OUTLINE_URL.format(material_id=str(uuid.uuid4())), headers=_auth(teacher)
    )
    assert missing.status_code == 404


def test_outline_failed_returns_502_with_job_id(
    worker_client: TestClient, fake_storage: FakeStorage
) -> None:
    client = worker_client
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(
        client, fake_storage, teacher, course_id, content=UNPARSEABLE_DOCX
    )
    material_id = completed["material"]["id"]
    job_id = completed["job"]["id"]

    response = client.get(
        OUTLINE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "AI_JOB_FAILED"
    assert error["details"]["job_id"] == job_id


def test_outline_returns_sections_with_locations_and_quotes(
    worker_client: TestClient, fake_storage: FakeStorage
) -> None:
    client = worker_client
    _register(client, "teacher@example.com", "TEACHER")
    _register(client, "student@example.com", "STUDENT")
    teacher = _login(client, "teacher@example.com")
    student = _login(client, "student@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(
        client,
        fake_storage,
        teacher,
        course_id,
        filename="chapter-1.pptx",
        content_type=PPTX_MIME,
        content=build_pptx(
            [
                ["软件工程概述", "定义与范围", "历史沿革"],
                ["需求工程", "需求获取", "需求验证"],
            ]
        ),
    )
    material_id = completed["material"]["id"]

    detail = client.get(f"/api/v1/courses/{course_id}", headers=_auth(teacher))
    _join_course(client, student, course_id, detail.json()["invite_code"])

    # 教师可读
    response = client.get(
        OUTLINE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert response.status_code == 200
    outline = response.json()
    assert outline["material_id"] == material_id
    assert [section["title"] for section in outline["sections"]] == [
        "软件工程概述",
        "需求工程",
    ]
    first = outline["sections"][0]
    assert first["source_type"] == "PPTX_SLIDE"
    assert first["location_start"] == first["location_end"] == 1
    assert [point["order"] for point in first["knowledge_points"]] == [1, 2]
    assert first["knowledge_points"][0]["quote"] == "定义与范围"
    assert first["knowledge_points"][0]["location_start"] == 1

    # 学生（课程成员）同样可读，含归档课程
    _archive_course(client, teacher, course_id)
    student_view = client.get(
        OUTLINE_URL.format(material_id=material_id), headers=_auth(student)
    )
    assert student_view.status_code == 200
    assert student_view.json() == outline


def test_worker_advances_material_and_job_to_succeeded(
    worker_client: TestClient, fake_storage: FakeStorage, pg_sync_engine: Engine
) -> None:
    client = worker_client
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(client, fake_storage, teacher, course_id)
    material_id = completed["material"]["id"]
    job_id = completed["job"]["id"]

    # Worker 在响应返回前执行完毕：资料 READY、任务 SUCCEEDED
    detail = client.get(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert detail.json()["status"] == "READY"
    assert detail.json()["error_message"] is None

    job = client.get(f"/api/v1/jobs/{job_id}", headers=_auth(teacher)).json()
    assert job["status"] == "SUCCEEDED"
    assert job["progress"] == 100
    assert job["started_at"] is not None
    assert job["finished_at"] is not None

    # 章节与知识点落库（1 张表 2 章节、2 知识点）
    assert _count(pg_sync_engine, "material_sections") == 2
    assert _count(pg_sync_engine, "material_knowledge_points") == 2


def test_worker_failure_marks_material_and_job_failed(
    worker_client: TestClient, fake_storage: FakeStorage, pg_sync_engine: Engine
) -> None:
    client = worker_client
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(
        client, fake_storage, teacher, course_id, content=UNPARSEABLE_DOCX
    )
    material_id = completed["material"]["id"]
    job_id = completed["job"]["id"]

    detail = client.get(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert detail.json()["status"] == "FAILED"
    message = detail.json()["error_message"]
    assert message  # 有可安全展示的失败原因
    assert "not a docx" not in message  # 不回显原始内容

    job = client.get(f"/api/v1/jobs/{job_id}", headers=_auth(teacher)).json()
    assert job["status"] == "FAILED"
    assert job["error"] == message
    assert job["finished_at"] is not None

    # 失败不落库：没有部分章节
    assert _count(pg_sync_engine, "material_sections") == 0
    assert _count(pg_sync_engine, "material_knowledge_points") == 0
