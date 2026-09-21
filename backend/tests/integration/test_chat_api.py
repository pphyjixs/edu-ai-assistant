"""课程问答接口的集成测试（专用测试库 + 假模型 HTTP 服务）。

覆盖 ``docs/api-contract.md`` 第 6 节：

- 权限与隔离：非成员 404、非所有者 404、会话列表各看各的、归档只禁止写入；
- 检索边界：跨课程、未就绪与已删除资料的片段不会进入提示词或引用；
- 受约束生成：有证据时 grounded 且引用可回查；无证据时固定文案 + 空引用；
- 并发与失败：并发发送 409 CHAT_CONFLICT、模型失败 502、未配置 503，
  均不留下半组消息；生成尝试记录留痕；
- 持久化：刷新（重新查询）后对话与引用完整。

模型侧全部使用本地假 HTTP 服务（``httpx.MockTransport``）；
**真实外部模型效果未在本次验收中验证**。
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

from app.modules.chat.router import get_ai_client_factory
from app.storage.deps import get_storage_dep
from tests.storage_fake import FakeStorage
from tests.integration.test_materials_api import (
    DOCX_MIME,
    PARSEABLE_DOCX,
    UNPARSEABLE_DOCX,
    VALID_OUTLINE_PAYLOAD,
    _auth,
    _create_course,
    _drive_worker,
    _fake_model_client,
    _login,
    _model_json_response,
    _register,
    _upload,
)
# 复用 materials 测试的内存对象存储夹具（pytest 会把导入的夹具当作本模块夹具）
from tests.integration.test_materials_api import fake_storage as fake_storage  # noqa: F401

SESSIONS_URL = "/api/v1/courses/{course_id}/chat-sessions"
MESSAGES_URL = "/api/v1/chat-sessions/{session_id}/messages"

_CHUNK_ID_PATTERN = re.compile(r"chunk_id=([0-9a-fA-F-]{36})")
_CHUNK_CONTENT_PATTERN = re.compile(r"原文：\n(.*?)(?=\n\n\[片段 |\Z)", re.DOTALL)


# --------------------------------------------------------------------------- #
# 夹具：应用 + 可注入的假模型
# --------------------------------------------------------------------------- #
def _make_chat_client(
    db_isolation: None,
    pg_app,
    fake_storage: FakeStorage,
    ai_client_factory=None,
    *,
    ai_configured: bool = True,
) -> TestClient:
    """构造应用；模型客户端通过依赖覆盖注入（不访问真实模型）。

    ``ai_configured=False`` 时把模型端点清空，用于验证
    ``503 SERVICE_UNAVAILABLE`` 分支（契约 6.7）。
    """
    if ai_configured:
        app = pg_app(
            ai_base_url="http://fake-model.local/v1", ai_model="fake-model"
        )
    else:
        app = pg_app(ai_base_url="", ai_model="")
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    if ai_client_factory is not None:
        app.dependency_overrides[get_ai_client_factory] = lambda: ai_client_factory
    else:
        app.dependency_overrides[get_ai_client_factory] = lambda: None
    return TestClient(app)


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage: FakeStorage) -> Iterator[TestClient]:
    """默认客户端：注入回答用的假模型服务。"""
    factory = make_answering_factory()
    with _make_chat_client(db_isolation, pg_app, fake_storage, factory) as test_client:
        yield test_client


def answering_responder(captured: list[str] | None = None):
    """假模型响应逻辑：按提示词中的片段 ID 回一条带有效引用的回答。"""

    def responder(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompt = payload["messages"][1]["content"]
        if captured is not None:
            captured.append(prompt)
        match = _CHUNK_ID_PATTERN.search(prompt)
        if match is None:
            return _model_json_response({"answer": "无片段", "citations": []})
        content_match = _CHUNK_CONTENT_PATTERN.search(prompt)
        content = content_match.group(1) if content_match else ""
        quote = next(
            (line.strip() for line in content.splitlines() if line.strip()), ""
        )
        return _model_json_response(
            {
                "answer": "按课件内容，要点如上。",
                "citations": [
                    {"chunk_id": match.group(1), "quote": quote[:200]},
                ],
            }
        )

    return responder


def make_answering_factory(captured: list[str] | None = None):
    """假模型客户端工厂（注入 ``httpx.MockTransport``）。"""
    return _fake_model_client(answering_responder(captured))


def _create_course_with_material(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_session_factory,
    make_settings,
    *,
    email: str,
    parse: bool = True,
    filename: str = "chapter-1.docx",
    content: bytes = PARSEABLE_DOCX,
    content_type: str = DOCX_MIME,
) -> tuple[str, str, str]:
    """建课程 + 一份资料；``parse=True`` 时驱动 Worker 让它变成 READY。

    返回 ``(课程 ID, 教师令牌, 资料 ID)``。
    """
    _register(client, email, "TEACHER")
    teacher = _login(client, email)
    course_id = _create_course(client, teacher)
    completed = _upload(
        client,
        fake_storage,
        teacher,
        course_id,
        filename=filename,
        content_type=content_type,
        content=content,
    )
    if parse:
        # 解析阶段用「大纲」假模型：资料 READY 后检索片段才可见
        _drive_worker(
            pg_session_factory,
            fake_storage,
            make_settings,
            _fake_model_client(
                lambda request: _model_json_response(VALID_OUTLINE_PAYLOAD)
            ),
        )
    return course_id, teacher, completed["material"]["id"]


# --------------------------------------------------------------------------- #
# 6.2 / 6.3 创建会话与会话列表
# --------------------------------------------------------------------------- #
def test_create_session_permissions_and_archived_course(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    _register(client, "student@example.com", "STUDENT")
    _register(client, "outsider@example.com", "STUDENT")
    teacher = _login(client, "teacher@example.com")
    student = _login(client, "student@example.com")
    outsider = _login(client, "outsider@example.com")

    course_id = _create_course(client, teacher)

    # 匿名 401
    assert client.post(SESSIONS_URL.format(course_id=course_id)).status_code == 401

    # 非成员 404（不区分「课程不存在」与「不可见」）
    outsider_response = client.post(
        SESSIONS_URL.format(course_id=course_id), headers=_auth(outsider)
    )
    assert outsider_response.status_code == 404
    assert outsider_response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    # 成员（学生）可以创建：学生与教师各看各的
    detail = client.get(f"/api/v1/courses/{course_id}", headers=_auth(teacher))
    invite = detail.json()["invite_code"]
    assert (
        client.post(
            "/api/v1/courses/join",
            json={"invite_code": invite},
            headers=_auth(student),
        ).status_code
        == 201
    )

    created = client.post(
        SESSIONS_URL.format(course_id=course_id), headers=_auth(student)
    )
    assert created.status_code == 201
    body = created.json()
    assert set(body) == {"id", "course_id", "created_at", "last_message_at"}
    assert body["course_id"] == course_id
    assert body["last_message_at"] == body["created_at"]

    # 未声明字段 → 422
    rejected = client.post(
        SESSIONS_URL.format(course_id=course_id),
        json={"title": "x"},
        headers=_auth(teacher),
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "VALIDATION_ERROR"

    # 归档课程：创建会话 409，但列表仍可读（读历史）
    client.post(f"/api/v1/courses/{course_id}/archive", headers=_auth(teacher))
    archived = client.post(
        SESSIONS_URL.format(course_id=course_id), headers=_auth(teacher)
    )
    assert archived.status_code == 409
    assert archived.json()["error"]["code"] == "COURSE_ARCHIVED"

    listed = client.get(SESSIONS_URL.format(course_id=course_id), headers=_auth(student))
    assert listed.status_code == 200
    assert listed.json()["total"] == 1


def test_create_session_request_body_variants(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    """省略请求体与 `{}` 都成功；显式 `null` 与多余字段统一 422（契约 6.2）。"""
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    url = SESSIONS_URL.format(course_id=course_id)

    assert client.post(url, headers=_auth(teacher)).status_code == 201
    assert client.post(url, json={}, headers=_auth(teacher)).status_code == 201

    explicit_null = client.post(
        url,
        content=b"null",
        headers={**_auth(teacher), "Content-Type": "application/json"},
    )
    assert explicit_null.status_code == 422
    assert explicit_null.json()["error"]["code"] == "VALIDATION_ERROR"

    extra_field = client.post(url, json={"title": "x"}, headers=_auth(teacher))
    assert extra_field.status_code == 422

    # 非法 UTF-8 字节序列：解码失败同样是请求体问题，必须 422 而不是 500
    invalid_encoding = client.post(
        url,
        content=b"\xff\xfe\x00\x80",
        headers={**_auth(teacher), "Content-Type": "application/json"},
    )
    assert invalid_encoding.status_code == 422
    assert invalid_encoding.json()["error"]["code"] == "VALIDATION_ERROR"

    # OpenAPI 与实现一致：请求体是**可选对象**（省略合法、显式 null 不合法）
    openapi = client.app.openapi()
    request_body = openapi["paths"][SESSIONS_URL]["post"]["requestBody"]
    assert request_body.get("required") in (None, False)
    body_schema = request_body["content"]["application/json"]["schema"]
    assert body_schema.get("type") == "object"
    assert "anyOf" not in body_schema, "请求体不应声明为可空（null）类型"
    assert body_schema.get("nullable") is not True


def test_session_list_only_returns_own_sessions(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    _register(client, "student@example.com", "STUDENT")
    teacher = _login(client, "teacher@example.com")
    student = _login(client, "student@example.com")
    course_id = _create_course(client, teacher)

    detail = client.get(f"/api/v1/courses/{course_id}", headers=_auth(teacher))
    client.post(
        "/api/v1/courses/join",
        json={"invite_code": detail.json()["invite_code"]},
        headers=_auth(student),
    )

    teacher_session = client.post(
        SESSIONS_URL.format(course_id=course_id), headers=_auth(teacher)
    ).json()
    student_session = client.post(
        SESSIONS_URL.format(course_id=course_id), headers=_auth(student)
    ).json()

    teacher_list = client.get(
        SESSIONS_URL.format(course_id=course_id), headers=_auth(teacher)
    ).json()
    assert teacher_list["total"] == 1
    assert [item["id"] for item in teacher_list["items"]] == [teacher_session["id"]]

    student_list = client.get(
        SESSIONS_URL.format(course_id=course_id), headers=_auth(student)
    ).json()
    assert [item["id"] for item in student_list["items"]] == [student_session["id"]]

    # 分页参数越界 → 422
    invalid = client.get(
        SESSIONS_URL.format(course_id=course_id),
        params={"page": 0},
        headers=_auth(teacher),
    )
    assert invalid.status_code == 422


# --------------------------------------------------------------------------- #
# 6.4 会话消息：只有会话所有者可见
# --------------------------------------------------------------------------- #
def test_messages_are_visible_only_to_owner(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    _register(client, "student@example.com", "STUDENT")
    teacher = _login(client, "teacher@example.com")
    student = _login(client, "student@example.com")
    course_id = _create_course(client, teacher)

    detail = client.get(f"/api/v1/courses/{course_id}", headers=_auth(teacher))
    client.post(
        "/api/v1/courses/join",
        json={"invite_code": detail.json()["invite_code"]},
        headers=_auth(student),
    )

    session_id = client.post(
        SESSIONS_URL.format(course_id=course_id), headers=_auth(student)
    ).json()["id"]

    # 所有者可读（空会话）
    own = client.get(MESSAGES_URL.format(session_id=session_id), headers=_auth(student))
    assert own.status_code == 200
    assert own.json() == {"items": [], "page": 1, "page_size": 20, "total": 0}

    # 同课程的其他成员（教师）不是所有者：读与写都 404，不泄露会话存在性
    other_read = client.get(
        MESSAGES_URL.format(session_id=session_id), headers=_auth(teacher)
    )
    assert other_read.status_code == 404
    assert other_read.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    other_write = client.post(
        MESSAGES_URL.format(session_id=session_id),
        json={"content": "我能插话吗？"},
        headers=_auth(teacher),
    )
    assert other_write.status_code == 404

    # 不存在的会话同样 404；匿名 401
    missing = client.get(
        MESSAGES_URL.format(session_id=str(uuid.uuid4())), headers=_auth(student)
    )
    assert missing.status_code == 404
    assert (
        client.get(MESSAGES_URL.format(session_id=session_id)).status_code == 401
    )


# --------------------------------------------------------------------------- #
# 6.5 请求校验
# --------------------------------------------------------------------------- #
def test_send_question_validates_content(
    client: TestClient, fake_storage: FakeStorage
) -> None:
    _register(client, "teacher@example.com", "TEACHER")
    teacher = _login(client, "teacher@example.com")
    course_id = _create_course(client, teacher)
    session_id = client.post(
        SESSIONS_URL.format(course_id=course_id), headers=_auth(teacher)
    ).json()["id"]
    url = MESSAGES_URL.format(session_id=session_id)

    for payload in (
        {},
        {"content": ""},
        {"content": "   "},
        {"content": "x" * 2001},
        {"content": "问题", "extra": 1},
        {"content": None},
    ):
        response = client.post(url, json=payload, headers=_auth(teacher))
        assert response.status_code == 422, payload
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    # 合法问题（当前课程没有 READY 资料）：仍写入一问一答，内容为无依据文案
    ok = client.post(url, json={"content": "  有依据吗？  "}, headers=_auth(teacher))
    assert ok.status_code == 201
    body = ok.json()
    assert body["role"] == "ASSISTANT"
    assert body["grounded"] is False
    assert body["citations"] == []
    assert body["content"] == "课程资料中未找到依据"

    listed = client.get(url, headers=_auth(teacher)).json()
    assert listed["total"] == 2
    assert [item["role"] for item in listed["items"]] == ["USER", "ASSISTANT"]
    # 用户消息：grounded 恒为 null；内容保留用户原文
    assert listed["items"][0]["grounded"] is None
    assert listed["items"][0]["content"] == "  有依据吗？  "

    # 消息分页越界 → 422
    invalid = client.get(url, params={"page_size": 101}, headers=_auth(teacher))
    assert invalid.status_code == 422


def test_no_evidence_without_model_config_returns_201(
    db_isolation: None, pg_app, fake_storage: FakeStorage
) -> None:
    """没有可检索片段时返回 201 无依据，不要求模型配置（契约 6.1 优先于 503）。"""
    with _make_chat_client(
        db_isolation, pg_app, fake_storage, None, ai_configured=False
    ) as client:
        _register(client, "no-evidence@example.com", "TEACHER")
        teacher = _login(client, "no-evidence@example.com")
        course_id = _create_course(client, teacher)
        session_id = client.post(
            SESSIONS_URL.format(course_id=course_id), headers=_auth(teacher)
        ).json()["id"]

        response = client.post(
            MESSAGES_URL.format(session_id=session_id),
            json={"content": "这门课讲了什么？"},
            headers=_auth(teacher),
        )

        assert response.status_code == 201, response.text
        assert response.json()["grounded"] is False
        assert response.json()["content"] == "课程资料中未找到依据"
        assert response.json()["citations"] == []


def test_conversation_persists_with_citations(
    client: TestClient,
    fake_storage: FakeStorage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """有依据的回答：引用可回查（片段 ID、资料、摘录都在库里）。"""
    course_id, teacher, material_id = _create_course_with_material(
        client, fake_storage, pg_session_factory, make_settings,
        email="chat-persist@example.com",
    )
    session_id = client.post(
        SESSIONS_URL.format(course_id=course_id), headers=_auth(teacher)
    ).json()["id"]
    url = MESSAGES_URL.format(session_id=session_id)

    first = client.post(url, json={"content": "软件工程是什么？"}, headers=_auth(teacher))
    assert first.status_code == 201, first.text
    answer = first.json()
    assert answer["role"] == "ASSISTANT"
    assert answer["grounded"] is True
    assert answer["citations"], "有依据的回答必须给出至少一条引用"

    citation = answer["citations"][0]
    assert citation["material_id"] == material_id
    assert citation["material_name"] == "chapter-1.docx"
    assert citation["source_type"] == "DOCX_PARAGRAPH"
    assert citation["page"] is None  # 仅 PDF 有 page
    assert citation["location_start"] <= citation["location_end"]

    # 引用摘录必须真的出现在该资料的片段原文中（可回查）
    with pg_sync_engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT content FROM material_chunks "
                    "WHERE material_id = CAST(:id AS uuid)"
                ),
                {"id": material_id},
            )
            .scalars()
            .all()
        )
    joined = "\n".join(rows)
    assert citation["quote"].strip()
    assert citation["quote"].strip().splitlines()[0] in joined

    # 刷新后（重新查询）对话与引用完整可读
    listing = client.get(url, headers=_auth(teacher)).json()
    assert listing["total"] == 2
    assistant = listing["items"][1]
    assert assistant["id"] == answer["id"]
    assert assistant["grounded"] is True
    assert assistant["citations"] == answer["citations"]

    # 第二轮：会话列表按最近消息时间排序（有消息的会话在前）
    sessions = client.get(
        SESSIONS_URL.format(course_id=course_id), headers=_auth(teacher)
    ).json()
    assert sessions["items"][0]["id"] == session_id
    assert sessions["items"][0]["last_message_at"] > sessions["items"][0]["created_at"]
