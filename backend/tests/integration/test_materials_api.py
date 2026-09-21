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
from collections.abc import Callable, Iterator

import httpx

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from app.core.time import utc_now
from app.modules.materials.models import Material
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
    """用 python-docx 生成真实 DOCX：``(文本, Heading 样式或 None)`` 列表。"""
    import docx

    document = docx.Document()
    for text, style in paragraphs:
        paragraph = document.add_paragraph()
        if style is not None:
            paragraph.style = document.styles[style]
        paragraph.add_run(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def build_pptx(slides: list[list[str]]) -> bytes:
    """用 python-pptx 生成真实 PPTX：每张幻灯片是「标题 + 内容条目」。"""
    from pptx import Presentation

    presentation = Presentation()
    for texts in slides:
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = texts[0]
        body = slide.placeholders[1].text_frame
        body.text = texts[1] if len(texts) > 1 else " "
        for extra in texts[2:]:
            body.add_paragraph().text = extra
    buffer = io.BytesIO()
    presentation.save(buffer)
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
    db_isolation: None, pg_app, fake_storage: FakeStorage
) -> TestClient:
    """构造应用（解析由独立 Worker 进程领取，测试中手动驱动）。"""
    app = pg_app()
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    return TestClient(app)


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage: FakeStorage) -> Iterator[TestClient]:
    """应用客户端：资料停留在 PROCESSING，由用例手动驱动 Worker。"""
    with _make_client(db_isolation, pg_app, fake_storage) as test_client:
        yield test_client


def _drive_worker(
    pg_session_factory,
    fake_storage: FakeStorage,
    make_settings,
    ai_client_factory=None,
    **overrides: object,
) -> int:
    """在测试库上驱动 Worker 领取并执行一批任务（同步包装）。

    ``ai_client_factory`` 可注入本地假模型 HTTP 服务
    （``httpx.MockTransport`` 支撑的 ``httpx.Client``）。默认提供假模型的
    端点与模型名配置；需要验证「未配置模型」分支时显式传 ``ai_base_url=""``。
    """
    import asyncio

    from app.modules.materials import worker

    overrides.setdefault("ai_base_url", "http://fake-model.local/v1")
    overrides.setdefault("ai_model", "fake-model")
    settings = make_settings(**overrides)

    async def run() -> int:
        return await worker.run_pending_batch(
            pg_session_factory,
            storage=fake_storage,
            settings=settings,
            ai_client_factory=ai_client_factory,
        )

    return asyncio.run(run())


def _fake_model_client(responder):
    """构造注入假模型 HTTP 服务的客户端工厂。"""
    import httpx

    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(responder))

    return factory


def _model_json_response(payload: dict) -> httpx.Response:
    import json

    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": json.dumps(payload, ensure_ascii=False)}}
            ]
        },
    )


# 合法模型输出：摘录逐字取自 PARSEABLE_DOCX 的提取文本
VALID_OUTLINE_PAYLOAD = {
    "sections": [
        {
            "title": "绪论",
            "location_start": 1,
            "location_end": 2,
            "knowledge_points": [
                {
                    "title": "系统化方法",
                    "description": "软件工程是应用系统化的方法。",
                    "quote": "软件工程是应用系统化的方法。",
                    "location_start": 2,
                    "location_end": 2,
                }
            ],
        },
        {
            "title": "需求分析",
            "location_start": 3,
            "location_end": 4,
            "knowledge_points": [
                {
                    "title": "生命周期起点",
                    "description": "需求分析是起点。",
                    "quote": "需求分析是软件生命周期的起点。",
                    "location_start": 4,
                    "location_end": 4,
                }
            ],
        },
    ]
}


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

    # 对象不立即删除（PUT 地址过期前可能有晚到 PUT），等待办清理
    assert len(fake_storage.objects) == 1

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
    client: TestClient,
    fake_storage: FakeStorage,
    pg_session_factory,
    make_settings,
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)

    completed = _upload(
        client, fake_storage, teacher, course_id, content=UNPARSEABLE_DOCX
    )
    material_id = completed["material"]["id"]
    failed_job_id = completed["job"]["id"]

    # 手动驱动 Worker：无效内容 → 提取失败 → 任务与资料 FAILED
    assert _drive_worker(pg_session_factory, fake_storage, make_settings) == 1
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

    # 重置后的任务等待 Worker 领取：本轮未驱动，资料回到 PROCESSING
    detail = client.get(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert detail.json()["status"] == "PROCESSING"
    assert detail.json()["error_message"] is None

    # 学生与非成员：403 / 404
    _register(client, "student@example.com", "STUDENT")
    student = _login(client, "student@example.com")
    assert (
        client.post(PARSE_URL.format(material_id=material_id), headers=_auth(student)).status_code
        == 404
    )


def test_retry_on_ready_returns_409(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_session_factory,
    make_settings,
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(client, fake_storage, teacher, course_id)
    material_id = completed["material"]["id"]

    ai_factory = _fake_model_client(
        lambda request: _model_json_response(VALID_OUTLINE_PAYLOAD)
    )
    assert _drive_worker(
        pg_session_factory, fake_storage, make_settings, ai_factory
    ) == 1
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
    client: TestClient,
    fake_storage: FakeStorage,
    pg_session_factory,
    make_settings,
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(
        client, fake_storage, teacher, course_id, content=UNPARSEABLE_DOCX
    )
    material_id = completed["material"]["id"]
    job_id = completed["job"]["id"]

    assert _drive_worker(pg_session_factory, fake_storage, make_settings) == 1

    response = client.get(
        OUTLINE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "AI_JOB_FAILED"
    assert error["details"]["job_id"] == job_id


def test_outline_returns_sections_with_locations_and_quotes(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_session_factory,
    make_settings,
) -> None:
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

    pptx_outline = {
        "sections": [
            {
                "title": "软件工程概述",
                "location_start": 1,
                "location_end": 1,
                "knowledge_points": [
                    {
                        "title": "定义与范围",
                        "description": "定义与范围。",
                        "quote": "定义与范围",
                        "location_start": 1,
                        "location_end": 1,
                    }
                ],
            },
            {
                "title": "需求工程",
                "location_start": 2,
                "location_end": 2,
                "knowledge_points": [
                    {
                        "title": "需求获取",
                        "description": "需求获取。",
                        "quote": "需求获取",
                        "location_start": 2,
                        "location_end": 2,
                    }
                ],
            },
        ]
    }
    ai_factory = _fake_model_client(
        lambda request: _model_json_response(pptx_outline)
    )
    assert _drive_worker(
        pg_session_factory, fake_storage, make_settings, ai_client_factory=ai_factory
    ) == 1

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
    assert [point["order"] for point in first["knowledge_points"]] == [1]
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
    client: TestClient,
    fake_storage: FakeStorage,
    pg_sync_engine: Engine,
    pg_session_factory,
    make_settings,
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(client, fake_storage, teacher, course_id)
    material_id = completed["material"]["id"]
    job_id = completed["job"]["id"]

    # 手动驱动 Worker（本地假模型 HTTP 服务）：资料 READY、任务 SUCCEEDED
    ai_factory = _fake_model_client(
        lambda request: _model_json_response(VALID_OUTLINE_PAYLOAD)
    )
    assert _drive_worker(
        pg_session_factory, fake_storage, make_settings, ai_factory
    ) == 1
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
    client: TestClient,
    fake_storage: FakeStorage,
    pg_sync_engine: Engine,
    pg_session_factory,
    make_settings,
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(
        client, fake_storage, teacher, course_id, content=UNPARSEABLE_DOCX
    )
    material_id = completed["material"]["id"]
    job_id = completed["job"]["id"]

    assert _drive_worker(pg_session_factory, fake_storage, make_settings) == 1
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


# --------------------------------------------------------------------------- #
# 删除流水线：对象删除待办 + 维护命令（契约 5.2）
# --------------------------------------------------------------------------- #
def _expire_put_urls(pg_sync_engine: Engine) -> None:
    """把 PUT 地址过期时间拨到过去（越过缓冲期）。

    上传会话与删除待办各自冗余保存该时间，两处都要更新。
    """
    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE material_upload_sessions"
                " SET upload_url_expires_at = now() - interval '2 hours'"
            )
        )
        connection.execute(
            text(
                "UPDATE material_delete_todos"
                " SET upload_expires_at = now() - interval '2 hours'"
            )
        )


def _run_cleanup(make_settings, pg_test_url: str, fake_storage: FakeStorage) -> tuple[int, int]:
    """在测试库上执行一次对象删除待办维护命令（同步包装）。

    每次调用自建独立引擎（NullPool）：``asyncio.run`` 的循环每次新建，
    不能复用全局缓存的引擎（其连接绑定在旧循环上）。
    """
    import asyncio

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.db.session import normalize_database_url
    from app.modules.materials import service as materials_service

    settings = make_settings(database_url=pg_test_url)

    async def run() -> tuple[int, int]:
        engine = create_async_engine(
            normalize_database_url(pg_test_url).url, poolclass=NullPool
        )
        try:
            factory = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            async with factory() as session:
                return await materials_service.cleanup_deleted_materials(
                    session, storage=fake_storage, settings=settings
                )
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_cleanup_command_deletes_object_and_verifies(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_sync_engine: Engine,
    pg_test_url: str,
    make_settings,
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    uploaded = _upload(client, fake_storage, teacher, course_id)
    material_id = uploaded["material"]["id"]

    deleted = client.delete(
        DELETE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert deleted.status_code == 204
    assert len(fake_storage.objects) == 1  # 对象尚未删除

    with pg_sync_engine.connect() as connection:
        todo_rows = connection.execute(
            text("SELECT status FROM material_delete_todos")
        ).scalars().all()
    assert todo_rows == ["PENDING"], "删除后应写入 PENDING 状态的对象删除待办"

    # 缓冲期内（PUT 地址未过期）：维护命令不处理
    assert _run_cleanup(make_settings, pg_test_url, fake_storage) == (0, 0)
    assert len(fake_storage.objects) == 1

    # PUT 地址过期：维护命令删除对象并核查通过 → DONE
    _expire_put_urls(pg_sync_engine)
    done, pending = _run_cleanup(make_settings, pg_test_url, fake_storage)
    assert (done, pending) == (1, 0)
    assert fake_storage.objects == {}

    # 幂等：已完成的待办不再处理
    assert _run_cleanup(make_settings, pg_test_url, fake_storage) == (0, 0)


def test_cleanup_retries_late_put_until_verified(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_sync_engine: Engine,
    pg_test_url: str,
    make_settings,
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    uploaded = _upload(client, fake_storage, teacher, course_id)
    material_id = uploaded["material"]["id"]
    client.delete(DELETE_URL.format(material_id=material_id), headers=_auth(teacher))
    _expire_put_urls(pg_sync_engine)

    # 晚到 PUT：删除后对象立即被重建 → 本轮保持 PENDING，继续重试
    fake_storage.recreate_after_delete = True
    done, pending = _run_cleanup(make_settings, pg_test_url, fake_storage)
    assert (done, pending) == (0, 1)
    assert len(fake_storage.objects) == 1  # 重建的对象仍在

    # 下一轮（晚到 PUT 不再发生）：删除并核查通过 → DONE，不留孤立对象
    fake_storage.recreate_after_delete = False
    done, pending = _run_cleanup(make_settings, pg_test_url, fake_storage)
    assert (done, pending) == (1, 0)
    assert fake_storage.objects == {}


def test_cleanup_survives_storage_outage_and_retries(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_sync_engine: Engine,
    pg_test_url: str,
    make_settings,
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    uploaded = _upload(client, fake_storage, teacher, course_id)
    material_id = uploaded["material"]["id"]
    client.delete(DELETE_URL.format(material_id=material_id), headers=_auth(teacher))
    _expire_put_urls(pg_sync_engine)

    # 存储故障：待办保留、记录错误，对象未被删除
    fake_storage.as_unavailable()
    done, pending = _run_cleanup(make_settings, pg_test_url, fake_storage)
    assert (done, pending) == (0, 1)
    assert len(fake_storage.objects) == 1

    # 恢复后重试成功
    fake_storage.as_available()
    done, pending = _run_cleanup(make_settings, pg_test_url, fake_storage)
    assert (done, pending) == (1, 0)
    assert fake_storage.objects == {}


def test_repeat_complete_returns_first_snapshot_after_deletion(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_session_factory,
    make_settings,
) -> None:
    """删除或状态变化后重复完成上传，仍返回首次响应快照（契约 4.6 / 5.2）。"""
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)

    sha256 = hashlib.sha256(PARSEABLE_DOCX).hexdigest()
    init = client.post(
        MATERIALS_URL.format(course_id=course_id) + "/uploads",
        json={
            "filename": "chapter-1.docx",
            "content_type": DOCX_MIME,
            "size": len(PARSEABLE_DOCX),
            "sha256": sha256,
        },
        headers=_auth(teacher),
    )
    assert init.status_code == 201
    presigned = init.json()
    fake_storage.store_object(
        _object_key_of(fake_storage, presigned["upload_url"]),
        size=len(PARSEABLE_DOCX),
        content_type=DOCX_MIME,
        sha256_hex=sha256,
        content=PARSEABLE_DOCX,
    )
    upload_id = presigned["upload_id"]
    complete_url = (
        MATERIALS_URL.format(course_id=course_id) + f"/uploads/{upload_id}/complete"
    )
    first = client.post(complete_url, headers=_auth(teacher))
    assert first.status_code == 202
    first_body = first.json()
    # 快照记录首次完成时刻的状态（PROCESSING）——Worker 随后才把它推进到 READY
    assert first_body["material"]["status"] == "PROCESSING"
    material_id = first_body["material"]["id"]

    ai_factory = _fake_model_client(
        lambda request: _model_json_response(VALID_OUTLINE_PAYLOAD)
    )
    assert _drive_worker(
        pg_session_factory, fake_storage, make_settings, ai_factory
    ) == 1
    current = client.get(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert current.json()["status"] == "READY"

    # 删除资料后重复确认：仍返回首次快照（PROCESSING 那份），202 不变
    assert (
        client.delete(
            DELETE_URL.format(material_id=material_id), headers=_auth(teacher)
        ).status_code
        == 204
    )
    replay_after_delete = client.post(complete_url, headers=_auth(teacher))
    assert replay_after_delete.status_code == 202
    assert replay_after_delete.json() == first_body


def test_repeat_complete_returns_first_snapshot_after_status_change(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    """解析状态变化（Worker 关闭 → 资料停在 PROCESSING）不改变重复确认结果。

    本用例在 Worker 关闭下验证快照回填路径本身：重复确认返回与首次
    相同的 material/job，而不是重新读取当前状态。
    """
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    sha256 = hashlib.sha256(PARSEABLE_DOCX).hexdigest()
    init = client.post(
        MATERIALS_URL.format(course_id=course_id) + "/uploads",
        json={
            "filename": "chapter-1.docx",
            "content_type": DOCX_MIME,
            "size": len(PARSEABLE_DOCX),
            "sha256": sha256,
        },
        headers=_auth(teacher),
    )
    presigned = init.json()
    fake_storage.store_object(
        _object_key_of(fake_storage, presigned["upload_url"]),
        size=len(PARSEABLE_DOCX),
        content_type=DOCX_MIME,
        sha256_hex=sha256,
        content=PARSEABLE_DOCX,
    )
    complete_url = (
        MATERIALS_URL.format(course_id=course_id)
        + f"/uploads/{presigned['upload_id']}/complete"
    )
    first = client.post(complete_url, headers=_auth(teacher))
    assert first.status_code == 202

    # Worker 关闭下资料停留 PROCESSING；重复确认返回首次响应快照，
    # 而不是重新读取当前资料与任务状态（快照回填路径）
    replay = client.post(complete_url, headers=_auth(teacher))
    assert replay.status_code == 202
    assert replay.json() == first.json()


def test_delete_cancels_pending_parse_job(
    client: TestClient, fake_storage: FakeStorage, pg_sync_engine: Engine
) -> None:
    """删除资料时取消未完成的解析任务（PENDING → CANCELLED）。"""
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    uploaded = _upload(client, fake_storage, teacher, course_id)
    material_id = uploaded["material"]["id"]
    job_id = uploaded["job"]["id"]

    response = client.delete(
        DELETE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert response.status_code == 204

    with pg_sync_engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM jobs WHERE id = CAST(:id AS uuid)"),
            {"id": job_id},
        ).scalar_one()
    assert status == "CANCELLED"


# --------------------------------------------------------------------------- #
# Worker 失败路径：模型输出校验、超限、扫描 PDF、AI 未配置、超时
# --------------------------------------------------------------------------- #
def _failed_detail(client: TestClient, teacher: str, material_id: str) -> dict:
    detail = client.get(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert detail.json()["status"] == "FAILED", detail.json()
    return detail.json()


def test_worker_fails_when_model_not_configured(
    client: TestClient, fake_storage: FakeStorage, pg_session_factory, make_settings
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(client, fake_storage, teacher, course_id)

    assert (
        _drive_worker(
            pg_session_factory,
            fake_storage,
            make_settings,
            ai_base_url="",  # 未配置模型端点
        )
        == 1
    )
    detail = _failed_detail(client, teacher, completed["material"]["id"])
    assert "未配置" in detail["error_message"]


def test_worker_fails_on_invalid_model_json(
    client: TestClient, fake_storage: FakeStorage, pg_session_factory, make_settings
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(client, fake_storage, teacher, course_id)

    import httpx as httpx_module

    def responder(request):
        return httpx_module.Response(200, text="这不是 JSON")

    assert (
        _drive_worker(
            pg_session_factory,
            fake_storage,
            make_settings,
            ai_client_factory=_fake_model_client(responder),
        )
        == 1
    )
    detail = _failed_detail(client, teacher, completed["material"]["id"])
    assert "格式无效" in detail["error_message"]

    # 模型回复正文不是 JSON → 同样 FAILED
    completed2 = _upload(client, fake_storage, teacher, course_id)
    assert (
        _drive_worker(
            pg_session_factory,
            fake_storage,
            make_settings,
            ai_client_factory=_fake_model_client(
                lambda request: httpx_module.Response(
                    200,
                    json={"choices": [{"message": {"content": "{invalid json"}}]},
                )
            ),
        )
        == 1
    )
    detail2 = _failed_detail(client, teacher, completed2["material"]["id"])
    assert "JSON" in detail2["error_message"]


def test_worker_fails_on_model_http_error(
    client: TestClient, fake_storage: FakeStorage, pg_session_factory, make_settings
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(client, fake_storage, teacher, course_id)

    import httpx as httpx_module

    assert (
        _drive_worker(
            pg_session_factory,
            fake_storage,
            make_settings,
            ai_client_factory=_fake_model_client(
                lambda request: httpx_module.Response(500)
            ),
        )
        == 1
    )
    detail = _failed_detail(client, teacher, completed["material"]["id"])
    assert "500" in detail["error_message"]


def test_worker_fails_on_hallucinated_quote(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_sync_engine: Engine,
    pg_session_factory,
    make_settings,
) -> None:
    """摘录不在来源文本中（模型幻觉）→ 整体无效，FAILED。"""
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(client, fake_storage, teacher, course_id)

    import copy

    bad_payload = copy.deepcopy(VALID_OUTLINE_PAYLOAD)
    bad_payload["sections"][0]["knowledge_points"][0]["quote"] = (
        "这句话在原文里根本不存在"
    )
    assert (
        _drive_worker(
            pg_session_factory,
            fake_storage,
            make_settings,
            ai_client_factory=_fake_model_client(
                lambda request: _model_json_response(bad_payload)
            ),
        )
        == 1
    )
    detail = _failed_detail(client, teacher, completed["material"]["id"])
    assert "摘录" in detail["error_message"]

    # 失败不落库：没有部分章节
    with pg_sync_engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM material_sections")
        ).scalar_one()
    assert count == 0


def test_worker_fails_on_model_timeout(
    client: TestClient, fake_storage: FakeStorage, pg_session_factory, make_settings
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(client, fake_storage, teacher, course_id)

    import httpx as httpx_module

    def timeout_responder(request):
        raise httpx_module.ConnectTimeout("timed out")

    assert (
        _drive_worker(
            pg_session_factory,
            fake_storage,
            make_settings,
            ai_client_factory=_fake_model_client(timeout_responder),
        )
        == 1
    )
    detail = _failed_detail(client, teacher, completed["material"]["id"])
    assert "超时" in detail["error_message"]


def test_worker_fails_when_text_exceeds_limit(
    client: TestClient, fake_storage: FakeStorage, pg_session_factory, make_settings
) -> None:
    """全文超过 120,000 字符上限 → 直接 FAILED，不截断后宣称成功（契约 5.5）。"""
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    # 20 段 × 7,000 字符（低于单段截断阈值）= 140,000 字符
    big_content = build_docx([(f"段{i}。" + "内容" * 3500, None) for i in range(20)])
    completed = _upload(
        client, fake_storage, teacher, course_id, content=big_content
    )

    # 无需模型：上限检查发生在提取阶段
    assert _drive_worker(pg_session_factory, fake_storage, make_settings) == 1
    detail = _failed_detail(client, teacher, completed["material"]["id"])
    assert "上限" in detail["error_message"]


def test_worker_marks_scanned_pdf_failed_without_ocr(
    client: TestClient, fake_storage: FakeStorage, pg_session_factory, make_settings
) -> None:
    """扫描版（图片型）PDF 无可提取文本：明确 FAILED，不做 OCR（契约 5.5）。"""
    import io

    from pypdf import PdfWriter

    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buffer = io.BytesIO()
    writer.write(buffer)
    blank_pdf = buffer.getvalue()

    completed = _upload(
        client,
        fake_storage,
        teacher,
        course_id,
        filename="scanned.pdf",
        content_type=PDF_MIME,
        content=blank_pdf,
    )

    assert _drive_worker(pg_session_factory, fake_storage, make_settings) == 1
    detail = _failed_detail(client, teacher, completed["material"]["id"])
    assert "扫描" in detail["error_message"] or "OCR" in detail["error_message"]


# --------------------------------------------------------------------------- #
# 崩溃恢复与解析中删除
# --------------------------------------------------------------------------- #
def test_retry_recovers_job_after_lease_expiry(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_session_factory,
    make_settings,
) -> None:
    """Worker 崩溃后 RUNNING 租约过期 → 重试解析回收任务（契约 5.3/5.5）。"""
    import asyncio
    import time as time_module

    from app.modules.materials import worker

    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(client, fake_storage, teacher, course_id)
    material_id = completed["material"]["id"]
    job_id = completed["job"]["id"]

    # 模拟 Worker 崩溃：领取（1 秒租约）后不再执行
    settings = make_settings(material_parse_lease_seconds=1)

    async def claim() -> None:
        await worker.claim_next(
            pg_session_factory, now=utc_now(), lease_seconds=1
        )

    asyncio.run(claim())
    job = client.get(f"/api/v1/jobs/{job_id}", headers=_auth(teacher)).json()
    assert job["status"] == "RUNNING"

    # 租约未过期时重试：幂等返回 RUNNING 任务
    immediate = client.post(
        PARSE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert immediate.status_code == 202
    assert immediate.json()["status"] == "RUNNING"

    # 租约过期后重试：回收任务 → PENDING
    time_module.sleep(1.1)
    recovered = client.post(
        PARSE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert recovered.status_code == 202
    assert recovered.json()["status"] == "PENDING"
    assert recovered.json()["id"] == job_id

    detail = client.get(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert detail.json()["status"] == "PROCESSING"


def test_job_aborts_when_material_deleted_mid_parse(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_sync_engine: Engine,
    pg_session_factory,
    make_settings,
) -> None:
    """解析中删除资料：Worker 回写前发现删除标记，放弃发布（契约 5.5 第 5 步）。"""
    import asyncio

    from app.modules.materials import worker

    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(client, fake_storage, teacher, course_id)
    material_id = completed["material"]["id"]
    job_id = completed["job"]["id"]

    # 手动领取（模拟 Worker 已开始执行）
    settings = make_settings(material_parse_lease_seconds=300)

    async def claim():
        return await worker.claim_next(
            pg_session_factory, now=utc_now(), lease_seconds=300
        )

    claimed = asyncio.run(claim())
    assert claimed is not None and claimed.material.id == uuid.UUID(material_id)

    # 解析执行期间删除资料（事务取消任务 + 标记删除）
    deleted = client.delete(
        DELETE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert deleted.status_code == 204

    # Worker 携带旧令牌回写成功结果：因资料已删除被放弃
    ai_factory = _fake_model_client(
        lambda request: _model_json_response(VALID_OUTLINE_PAYLOAD)
    )
    asyncio.run(
        worker.run_job(
            pg_session_factory,
            claimed=claimed,
            storage=fake_storage,
            settings=settings,
            ai_client_factory=ai_factory,
        )
    )

    # 资料仍处于已删除状态，没有章节被发布，任务保持 CANCELLED
    assert (
        client.get(f"/api/v1/materials/{material_id}", headers=_auth(teacher)).status_code
        == 404
    )
    with pg_sync_engine.connect() as connection:
        job_status = connection.execute(
            text("SELECT status FROM jobs WHERE id = CAST(:id AS uuid)"),
            {"id": job_id},
        ).scalar_one()
        section_count = connection.execute(
            text(
                "SELECT count(*) FROM material_sections "
                "WHERE material_id = CAST(:id AS uuid)"
            ),
            {"id": material_id},
        ).scalar_one()
    assert job_status == "CANCELLED"
    assert section_count == 0


def test_retry_revokes_stale_worker_write_back(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_sync_engine: Engine,
    pg_session_factory,
    make_settings,
) -> None:
    """竞态回归：租约过期被重试回收后，旧执行者不能再回写（契约 5.3/5.5）。

    场景：Worker 领取后崩溃（租约过期但令牌仍在）；重试接口把任务重置为
    ``PENDING`` 并**清空运行令牌**；此时旧执行者恢复，分别尝试**成功回写**
    与**失败回写**——两者都必须被拒绝；随后新 Worker 领取并正常完成。
    """
    import asyncio
    from datetime import timedelta

    from app.modules.materials import worker
    from app.modules.materials.outline_ai import GeneratedKnowledgePoint, GeneratedSection, GeneratedOutline

    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(client, fake_storage, teacher, course_id)
    material_id = completed["material"]["id"]
    job_id = completed["job"]["id"]

    # 用过去的时间领取：租约从领取时刻起算，天然已过期（不依赖 sleep）
    stale_claim_time = utc_now() - timedelta(seconds=7200)

    async def stale_claim():
        return await worker.claim_next(
            pg_session_factory,
            now=stale_claim_time,
            lease_seconds=3600,  # 租约在 1 小时前已到期
        )

    claimed = asyncio.run(stale_claim())
    assert claimed is not None
    assert claimed.job_id == uuid.UUID(job_id)
    stale_token = claimed.run_token
    material_uuid = uuid.UUID(material_id)

    # 重试回收：任务重置为 PENDING、令牌被清空
    retried = client.post(
        PARSE_URL.format(material_id=material_id), headers=_auth(teacher)
    )
    assert retried.status_code == 202
    assert retried.json()["status"] == "PENDING"
    assert retried.json()["id"] == job_id

    # 旧执行者恢复：尝试成功回写（携带过期前取得的令牌）
    old_outline = GeneratedOutline(
        sections=[
            GeneratedSection(
                title="旧执行者的章节",
                location_start=1,
                location_end=1,
                knowledge_points=[
                    GeneratedKnowledgePoint(
                        title="旧知识点",
                        description="不应被发布。",
                        quote="软件工程是应用系统化的方法。",
                        location_start=1,
                        location_end=1,
                    )
                ],
            )
        ]
    )

    async def stale_write_success() -> bool:
        async with pg_session_factory() as session:
            material = (
                (
                    await session.execute(
                        select(Material).where(Material.id == material_uuid)
                    )
                )
                .scalars()
                .one()
            )
            return await worker._write_success(
                session,
                material=material,
                outline=old_outline,
                # 旧执行者连检索片段一起带回：必须同样被拒绝（契约 6.1）
                retrieval_chunks=[
                    worker.extraction.RetrievalChunk(
                        index=1,
                        location_start=1,
                        location_end=1,
                        content="旧执行者的片段，不应落库。",
                    )
                ],
                run_token=stale_token,
                now=utc_now(),
            )

    assert asyncio.run(stale_write_success()) is False

    # 旧执行者尝试失败回写：同样被拒绝
    async def stale_write_failure() -> None:
        await worker._write_failure(
            pg_session_factory,
            material_id=material_uuid,
            run_token=stale_token,
            message="旧执行者的失败",
            now=utc_now(),
        )

    asyncio.run(stale_write_failure())

    # 任务仍为 PENDING、资料仍为 PROCESSING、没有旧解析产物
    detail = client.get(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert detail.json()["status"] == "PROCESSING"
    assert detail.json()["error_message"] is None
    job = client.get(f"/api/v1/jobs/{job_id}", headers=_auth(teacher)).json()
    assert job["status"] == "PENDING"
    assert job["error"] is None
    with pg_sync_engine.connect() as connection:
        section_count = connection.execute(
            text(
                "SELECT count(*) FROM material_sections "
                "WHERE material_id = CAST(:id AS uuid)"
            ),
            {"id": material_id},
        ).scalar_one()
        chunk_count = connection.execute(
            text(
                "SELECT count(*) FROM material_chunks "
                "WHERE material_id = CAST(:id AS uuid)"
            ),
            {"id": material_id},
        ).scalar_one()
    assert section_count == 0
    # 旧执行者的片段同样不得出现
    assert chunk_count == 0

    # 新 Worker 领取：新令牌（与旧令牌不同）、原 job ID 不变，并正常完成
    ai_factory = _fake_model_client(
        lambda request: _model_json_response(VALID_OUTLINE_PAYLOAD)
    )
    assert (
        _drive_worker(pg_session_factory, fake_storage, make_settings, ai_factory)
        == 1
    )
    detail = client.get(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert detail.json()["status"] == "READY"
    job = client.get(f"/api/v1/jobs/{job_id}", headers=_auth(teacher)).json()
    assert job["id"] == job_id
    assert job["status"] == "SUCCEEDED"

    # 令牌确实更换：直接查库比对
    with pg_sync_engine.connect() as connection:
        token = connection.execute(
            text("SELECT run_token FROM jobs WHERE id = CAST(:id AS uuid)"),
            {"id": job_id},
        ).scalar_one()
    assert token is not None
    assert token != stale_token
    assert job["progress"] == 100


# --------------------------------------------------------------------------- #
# 可检索原文片段（契约 6.1）：解析成功落库、失败不落库、重复解析不累积、删除即清空
# --------------------------------------------------------------------------- #
def _chunk_rows(pg_sync_engine: Engine, material_id: str) -> list[dict]:
    with pg_sync_engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    'SELECT "order", content, location_start, location_end'
                    " FROM material_chunks WHERE material_id = CAST(:id AS uuid)"
                    ' ORDER BY "order"'
                ),
                {"id": material_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _material_id_after_upload(
    client: TestClient, fake_storage: FakeStorage, *, email: str = "chunks@example.com"
) -> tuple[str, str]:
    """注册教师、建课程并上传一份可解析资料，返回 ``(资料 ID, 令牌)``。"""
    _register(client, email, "TEACHER")
    teacher = _login(client, email)
    course_id = _create_course(client, teacher)
    completed = _upload(client, fake_storage, teacher, course_id)
    return completed["material"]["id"], teacher


def test_worker_persists_retrieval_chunks(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_sync_engine: Engine,
    pg_session_factory,
    make_settings,
) -> None:
    """解析成功：片段与大纲一同落库，顺序连续且定位合法。"""
    material_id, _ = _material_id_after_upload(client, fake_storage)
    ai_factory = _fake_model_client(
        lambda request: _model_json_response(VALID_OUTLINE_PAYLOAD)
    )

    assert (
        _drive_worker(pg_session_factory, fake_storage, make_settings, ai_factory)
        == 1
    )
    assert client.get(
        f"/api/v1/materials/{material_id}", headers=_auth(_login(client, "chunks@example.com"))
    ).json()["status"] == "READY"

    rows = _chunk_rows(pg_sync_engine, material_id)
    assert rows, "解析成功必须落库可检索片段"
    assert [row["order"] for row in rows] == list(range(1, len(rows) + 1))
    for row in rows:
        assert row["content"].strip()
        assert 1 <= row["location_start"] <= row["location_end"]

    # 原文必须可检索：正文句子出现在某个片段中
    joined = "\n".join(row["content"] for row in rows)
    assert "软件工程是应用系统化的方法。" in joined
    assert "需求分析是软件生命周期的起点。" in joined


def test_repeated_parse_does_not_duplicate_chunks(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_sync_engine: Engine,
    pg_session_factory,
    make_settings,
) -> None:
    """重复解析：片段全量重写，数量不翻倍（契约 5.5）。"""
    material_id, teacher = _material_id_after_upload(client, fake_storage)
    ai_factory = _fake_model_client(
        lambda request: _model_json_response(VALID_OUTLINE_PAYLOAD)
    )

    assert _drive_worker(pg_session_factory, fake_storage, make_settings, ai_factory) == 1
    first = _chunk_rows(pg_sync_engine, material_id)
    assert first

    # 队列已空：再驱动一次不会新增任何片段
    assert _drive_worker(pg_session_factory, fake_storage, make_settings, ai_factory) == 0
    assert _chunk_rows(pg_sync_engine, material_id) == first

    # 强制把任务重置为 PENDING（等价于一次重试）后再解析：仍然是同一批片段
    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET status='PENDING', run_token=NULL,"
                " lease_expires_at=NULL, started_at=NULL, finished_at=NULL,"
                " progress=0, error=NULL WHERE id = ("
                " SELECT id FROM jobs WHERE resource_id = CAST(:id AS uuid))"
            ),
            {"id": material_id},
        )
    assert _drive_worker(pg_session_factory, fake_storage, make_settings, ai_factory) == 1
    second = _chunk_rows(pg_sync_engine, material_id)
    assert len(second) == len(first)
    assert [row["content"] for row in second] == [row["content"] for row in first]


def test_failed_parse_persists_no_chunks(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_sync_engine: Engine,
    pg_session_factory,
    make_settings,
) -> None:
    """解析失败：不落库任何片段（也不产生可见的错误响应体之外的影响）。"""
    _register(client, "badfile@example.com", "TEACHER")
    teacher = _login(client, "badfile@example.com")
    course_id = _create_course(client, teacher)
    completed = _upload(
        client,
        fake_storage,
        teacher,
        course_id,
        filename="broken.docx",
        content_type=DOCX_MIME,
        content=UNPARSEABLE_DOCX,
    )
    material_id = completed["material"]["id"]

    assert (
        _drive_worker(pg_session_factory, fake_storage, make_settings, None) == 1
    )
    detail = client.get(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert detail.json()["status"] == "FAILED"
    assert _chunk_rows(pg_sync_engine, material_id) == []


def test_deleting_material_removes_chunks(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_sync_engine: Engine,
    pg_session_factory,
    make_settings,
) -> None:
    """删除资料后片段不得再被检索（契约 5.2 / 6.1）。"""
    material_id, teacher = _material_id_after_upload(client, fake_storage)
    ai_factory = _fake_model_client(
        lambda request: _model_json_response(VALID_OUTLINE_PAYLOAD)
    )
    assert _drive_worker(pg_session_factory, fake_storage, make_settings, ai_factory) == 1
    assert _chunk_rows(pg_sync_engine, material_id)

    deleted = client.delete(f"/api/v1/materials/{material_id}", headers=_auth(teacher))
    assert deleted.status_code == 204, deleted.text

    assert _chunk_rows(pg_sync_engine, material_id) == []
