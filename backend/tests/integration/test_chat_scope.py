"""问答的检索边界、归档竞态、并发冲突与模型失败（专用测试库）。

补充 ``test_chat_api.py``：

- 提示词只含本课程 **READY** 资料的片段（跨课程、未就绪、已删除都不进入）；
- 归档课程：读历史仍 200、发送问题 409；
- 并发发送：一个 201、一个 409 ``CHAT_CONFLICT``，不留半组消息；
- 模型失败（超时/5xx/无效输出）→ 502，未配置 → 503，均不写消息，
  只留一条安全的生成尝试记录。

模型侧全部使用本地假 HTTP 服务；**真实外部模型效果未在本次验收中验证**。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.modules.chat import answer_ai
from app.modules.chat.router import get_ai_client_factory
from app.storage.deps import get_storage_dep
from tests.integration.test_chat_api import (
    MESSAGES_URL,
    SESSIONS_URL,
    _create_course_with_material,
    _make_chat_client,
    answering_responder,
    make_answering_factory,
)
from tests.integration.test_materials_api import (
    _archive_course,
    _auth,
    _fake_model_client,
    _login,
    _model_json_response,
    _upload,
    build_docx,
)
from tests.integration.test_materials_api import fake_storage as fake_storage  # noqa: F401


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage) -> Iterator[TestClient]:
    """注入假模型的应用客户端（本文件多数用例手动构造，需要时用这个）。"""
    with _make_chat_client(
        db_isolation, pg_app, fake_storage, make_answering_factory()
    ) as test_client:
        yield test_client


def _start_session(client: TestClient, teacher: str, course_id: str) -> str:
    """建一个会话并返回会话 ID。"""
    response = client.post(
        SESSIONS_URL.format(course_id=course_id), headers=_auth(teacher)
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _attempt_rows(pg_sync_engine, session_id: str) -> list[dict]:
    """读取某会话的生成尝试记录（内部可观测性，契约 6.1）。"""
    with pg_sync_engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT status, model, prompt_version, retrieved_count,"
                    " grounded, duration_ms, error FROM chat_generation_attempts"
                    " WHERE session_id = CAST(:id AS uuid)"
                ),
                {"id": session_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _message_total(client: TestClient, teacher: str, session_id: str) -> int:
    response = client.get(
        MESSAGES_URL.format(session_id=session_id), headers=_auth(teacher)
    )
    assert response.status_code == 200, response.text
    return response.json()["total"]


# --------------------------------------------------------------------------- #
# 检索边界：跨课程、未就绪、已删除都不进入提示词
# --------------------------------------------------------------------------- #
def test_prompt_only_contains_this_course_ready_materials(
    db_isolation: None,
    pg_app,
    fake_storage,
    pg_session_factory,
    make_settings,
) -> None:
    captured: list[str] = []
    factory = make_answering_factory(captured)
    with _make_chat_client(db_isolation, pg_app, fake_storage, factory) as client:
        course_a, teacher_a, _ = _create_course_with_material(
            client,
            fake_storage,
            pg_session_factory,
            make_settings,
            email="scope-a@example.com",
        )
        # 同一课程的第二份资料：只上传不解析 → PROCESSING，没有片段
        _upload(
            client,
            fake_storage,
            teacher_a,
            course_a,
            filename="pending.docx",
            content=build_docx([("未解析资料的标识词甲甲乙乙", None)]),
        )
        # 另一门课程的 READY 资料：不得进入本课程的提示词
        _create_course_with_material(
            client,
            fake_storage,
            pg_session_factory,
            make_settings,
            email="scope-b@example.com",
            filename="other-course.docx",
            content=build_docx([("跨课程资料的标识词丙丙丁丁", None)]),
        )

        session_id = _start_session(client, teacher_a, course_a)
        response = client.post(
            MESSAGES_URL.format(session_id=session_id),
            json={"content": "软件工程是应用系统化的方法"},
            headers=_auth(teacher_a),
        )
        assert response.status_code == 201, response.text

    assert captured, "有片段时必须调用模型"
    prompt = captured[-1]
    assert "chapter-1.docx" in prompt  # 本课程的 READY 资料
    assert "other-course.docx" not in prompt
    assert "跨课程资料的标识词丙丙丁丁" not in prompt
    assert "pending.docx" not in prompt
    assert "未解析资料的标识词甲甲乙乙" not in prompt


def test_deleted_material_is_no_longer_retrieved(
    db_isolation: None,
    pg_app,
    fake_storage,
    pg_session_factory,
    make_settings,
) -> None:
    """删除资料会清空片段：之后的提问检索不到，回答转为无依据。"""
    captured: list[str] = []
    factory = make_answering_factory(captured)
    with _make_chat_client(db_isolation, pg_app, fake_storage, factory) as client:
        course_id, teacher, material_id = _create_course_with_material(
            client,
            fake_storage,
            pg_session_factory,
            make_settings,
            email="scope-delete@example.com",
        )
        session_id = _start_session(client, teacher, course_id)

        # 删除前：有依据
        first = client.post(
            MESSAGES_URL.format(session_id=session_id),
            json={"content": "软件工程是应用系统化的方法"},
            headers=_auth(teacher),
        )
        assert first.json()["grounded"] is True
        captured.clear()

        # 删除资料（片段随之清空）
        deleted = client.delete(
            f"/api/v1/materials/{material_id}", headers=_auth(teacher)
        )
        assert deleted.status_code == 204

        second = client.post(
            MESSAGES_URL.format(session_id=session_id),
            json={"content": "软件工程是应用系统化的方法"},
            headers=_auth(teacher),
        )
        assert second.status_code == 201
        assert second.json()["grounded"] is False
        assert second.json()["citations"] == []
        assert second.json()["content"] == "课程资料中未找到依据"
    # 检索为空时不调用模型：删除后的那次提问没有新的提示词
    assert captured == []


# --------------------------------------------------------------------------- #
# 归档课程：读历史仍可读，发送问题 409
# --------------------------------------------------------------------------- #
def test_archived_course_allows_history_but_rejects_question(
    db_isolation: None,
    pg_app,
    fake_storage,
    pg_session_factory,
    make_settings,
) -> None:
    factory = make_answering_factory()
    with _make_chat_client(db_isolation, pg_app, fake_storage, factory) as client:
        course_id, teacher, _ = _create_course_with_material(
            client,
            fake_storage,
            pg_session_factory,
            make_settings,
            email="scope-archive@example.com",
        )
        session_id = _start_session(client, teacher, course_id)
        url = MESSAGES_URL.format(session_id=session_id)
        assert (
            client.post(url, json={"content": "第一问"}, headers=_auth(teacher)).status_code
            == 201
        )

        _archive_course(client, teacher, course_id)

        # 读历史仍 200（归档只禁止写入）
        history = client.get(url, headers=_auth(teacher))
        assert history.status_code == 200
        assert history.json()["total"] == 2
        sessions = client.get(
            SESSIONS_URL.format(course_id=course_id), headers=_auth(teacher)
        )
        assert sessions.status_code == 200

        # 发送问题 409，且不新增消息
        blocked = client.post(url, json={"content": "第二问"}, headers=_auth(teacher))
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "COURSE_ARCHIVED"
        assert _message_total(client, teacher, session_id) == 2


# --------------------------------------------------------------------------- #
# 并发发送：一个成功、一个 409 CHAT_CONFLICT，不留半组消息
# --------------------------------------------------------------------------- #
def test_concurrent_sends_produce_one_conflict(
    db_isolation: None,
    pg_app,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    captured: list[str] = []
    responder = answering_responder(captured)

    def slow_responder(request: httpx.Request) -> httpx.Response:
        """放慢模型响应，保证两个请求都在读到同一会话版本后进入生成阶段。"""
        time.sleep(0.4)
        return responder(request)

    factory = _fake_model_client(slow_responder)
    app = pg_app(ai_base_url="http://fake-model.local/v1", ai_model="fake-model")
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    app.dependency_overrides[get_ai_client_factory] = lambda: factory

    with TestClient(app) as setup_client:
        course_id, teacher, _ = _create_course_with_material(
            setup_client,
            fake_storage,
            pg_session_factory,
            make_settings,
            email="scope-concurrent@example.com",
        )
        session_id = _start_session(setup_client, teacher, course_id)
        url = MESSAGES_URL.format(session_id=session_id)
        headers = _auth(teacher)

    def send(_: int) -> int:
        with TestClient(app) as worker_client:
            response = worker_client.post(
                url, json={"content": "并发提问"}, headers=headers
            )
            return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(send, range(2)))

    # 两个请求读到同一会话版本时恰好一个冲突；请求被串行化时两个都成功。
    # 不允许出现 5xx，也不允许出现"冲突却写了消息"这类不一致。
    assert statuses in ([201, 409], [201, 201]), statuses
    success = statuses.count(201)

    with TestClient(app) as verify_client:
        # 每个成功请求写入一问一答：不出现半组消息
        assert _message_total(verify_client, teacher, session_id) == 2 * success
        # 生成尝试记录与成功次数一致（冲突发生在写入前，不产生尝试记录）
        attempts = _attempt_rows(pg_sync_engine, session_id)
        assert len(attempts) == success
        assert {row["status"] for row in attempts} == {"SUCCEEDED"}


def test_service_level_concurrent_send_conflicts(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """确定性地验证乐观锁：两个协程同时发送，恰好一个 409 CHAT_CONFLICT。"""
    import asyncio

    from app.core.errors import ChatConflictError
    from app.modules.auth.models import User
    from app.modules.chat import service as chat_service

    course_id, teacher_token, _ = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="scope-service-concurrent@example.com",
    )
    session_id = _start_session(client, teacher_token, course_id)
    me = client.get("/api/v1/users/me", headers=_auth(teacher_token)).json()

    settings = make_settings(
        ai_base_url="http://fake-model.local/v1", ai_model="fake-model"
    )
    responder = answering_responder()

    async def send_once() -> str:
        async with pg_session_factory() as session:
            user = await session.get(User, uuid.UUID(me["id"]))
            assert user is not None
            try:
                await chat_service.send_question(
                    session,
                    user=user,
                    session_id=uuid.UUID(session_id),
                    content="软件工程是应用系统化的方法",
                    settings=settings,
                    ai_client_factory=lambda: _fake_model_client(responder)(),
                )
                return "ok"
            except ChatConflictError:
                return "conflict"

    async def run() -> list[str]:
        return sorted(await asyncio.gather(send_once(), send_once()))

    assert asyncio.run(run()) == ["conflict", "ok"]
    # 只落库一组一问一答，且留一条成功尝试
    assert _message_total(client, teacher_token, session_id) == 2
    attempts = _attempt_rows(pg_sync_engine, session_id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "SUCCEEDED"


# --------------------------------------------------------------------------- #
# 模型失败：502 / 503，都不写消息、只留安全尝试记录
# --------------------------------------------------------------------------- #
def _failure_case(
    db_isolation: None,
    pg_app,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    *,
    responder=None,
    ai_configured: bool = True,
    expected_status: int,
    expected_code: str,
) -> None:
    factory = (
        _fake_model_client(responder) if responder is not None else None
    )
    with _make_chat_client(
        db_isolation, pg_app, fake_storage, factory, ai_configured=ai_configured
    ) as client:
        course_id, teacher, _ = _create_course_with_material(
            client,
            fake_storage,
            pg_session_factory,
            make_settings,
            email=f"failure-{expected_code.lower()}@example.com",
        )
        session_id = _start_session(client, teacher, course_id)
        response = client.post(
            MESSAGES_URL.format(session_id=session_id),
            json={"content": "软件工程是应用系统化的方法"},
            headers=_auth(teacher),
        )

    assert response.status_code == expected_status, response.text
    assert response.json()["error"]["code"] == expected_code
    # 不新增任何消息（会话历史保持原样）
    with _make_chat_client(db_isolation, pg_app, fake_storage, None) as client:
        assert _message_total(client, teacher, session_id) == 0

    attempts = _attempt_rows(pg_sync_engine, session_id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "FAILED"
    assert attempts[0]["prompt_version"] == answer_ai.PROMPT_VERSION
    # 失败摘要安全：不含提示词、课件原文或模型地址
    error = attempts[0]["error"] or ""
    assert "软件工程是应用系统化的方法" not in error
    assert "fake-model.local" not in error


def test_model_timeout_returns_502(
    db_isolation: None, pg_app, fake_storage, pg_session_factory, make_settings,
    pg_sync_engine,
) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    _failure_case(
        db_isolation, pg_app, fake_storage, pg_session_factory, make_settings,
        pg_sync_engine,
        responder=timeout, expected_status=502, expected_code="AI_JOB_FAILED",
    )


def test_model_server_error_returns_502(
    db_isolation: None, pg_app, fake_storage, pg_session_factory, make_settings,
    pg_sync_engine,
) -> None:
    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _failure_case(
        db_isolation, pg_app, fake_storage, pg_session_factory, make_settings,
        pg_sync_engine,
        responder=server_error, expected_status=502, expected_code="AI_JOB_FAILED",
    )


def test_invalid_model_output_returns_502(
    db_isolation: None, pg_app, fake_storage, pg_session_factory, make_settings,
    pg_sync_engine,
) -> None:
    def invalid(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "不是 JSON"}}]}
        )

    _failure_case(
        db_isolation, pg_app, fake_storage, pg_session_factory, make_settings,
        pg_sync_engine,
        responder=invalid, expected_status=502, expected_code="AI_JOB_FAILED",
    )


def test_missing_model_configuration_returns_503(
    db_isolation: None, pg_app, fake_storage, pg_session_factory, make_settings,
    pg_sync_engine,
) -> None:
    _failure_case(
        db_isolation, pg_app, fake_storage, pg_session_factory, make_settings,
        pg_sync_engine,
        responder=None, ai_configured=False,
        expected_status=503, expected_code="SERVICE_UNAVAILABLE",
    )
