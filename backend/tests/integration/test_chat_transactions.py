"""问答写入事务与并发边界的回归测试（专用测试库）。

对应本轮修复的验收项：

- **模型调用期间没有活动事务**：只读检查与检索结束后即结束事务；
- **生成中归档**：归档先完成则提问返回 `409 COURSE_ARCHIVED`，不写消息；
- **创建中归档**：创建会话在课程行锁内检查归档，与并发归档结果一致；
- **引用资料并发删除**：写入事务对引用资料加共享锁，生成期间被删除的资料
  会被复查排除，按无依据回答处理。

并发场景用 service 层协程精确构造（HTTP 层并发受调度影响不稳定），
模型侧仍是本地假模型服务。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.errors import ChatConflictError, CourseArchivedError
from app.modules.auth.models import User
from app.modules.chat import service as chat_service
from app.modules.chat.router import get_ai_client_factory
from app.modules.chat.schemas import NO_EVIDENCE_ANSWER
from app.modules.courses import service as courses_service
from app.modules.materials import service as materials_service
from app.storage.deps import get_storage_dep
from tests.integration.test_chat_api import (
    MESSAGES_URL,
    SESSIONS_URL,
    _create_course_with_material,
    _make_chat_client,
    answering_responder,
    make_answering_factory,
)
from tests.integration.test_materials_api import _auth, _fake_model_client
from tests.integration.test_materials_api import fake_storage as fake_storage  # noqa: F401


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage) -> Iterator[TestClient]:
    """注入假模型的应用客户端。"""
    with _make_chat_client(
        db_isolation, pg_app, fake_storage, make_answering_factory()
    ) as test_client:
        yield test_client


def _start_session(client: TestClient, teacher: str, course_id: str) -> str:
    response = client.post(
        SESSIONS_URL.format(course_id=course_id), headers=_auth(teacher)
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _me_id(client: TestClient, teacher: str) -> uuid.UUID:
    response = client.get("/api/v1/users/me", headers=_auth(teacher))
    assert response.status_code == 200, response.text
    return uuid.UUID(response.json()["id"])


def _message_rows(pg_sync_engine, session_id: str) -> list[dict]:
    with pg_sync_engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT role, content, grounded FROM chat_messages"
                    " WHERE session_id = CAST(:id AS uuid) ORDER BY created_at"
                ),
                {"id": session_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _slow_responder(delay: float = 0.4):
    """慢速假模型：给并发操作留出确定的时间窗口。"""
    responder = answering_responder()

    def slow(request: httpx.Request) -> httpx.Response:
        time.sleep(delay)
        return responder(request)

    return slow


# --------------------------------------------------------------------------- #
# 模型调用期间没有活动事务
# --------------------------------------------------------------------------- #
def test_model_call_runs_without_active_transaction(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """构造 AI 客户端的那一刻（紧邻模型调用）会话不应处于事务中。"""
    course_id, teacher, _ = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="tx-no-transaction@example.com",
    )
    session_id = _start_session(client, teacher, course_id)
    user_id = _me_id(client, teacher)

    settings = make_settings(
        ai_base_url="http://fake-model.local/v1", ai_model="fake-model"
    )
    responder = answering_responder()
    observed: list[bool] = []

    async def run() -> None:
        async with pg_session_factory() as session:
            user = await session.get(User, user_id)
            assert user is not None

            def factory():
                # 模型调用紧跟着工厂调用；此时只读事务必须已经结束
                observed.append(session.in_transaction())
                return _fake_model_client(responder)()

            await chat_service.send_question(
                session,
                user=user,
                session_id=uuid.UUID(session_id),
                content="软件工程是应用系统化的方法",
                settings=settings,
                ai_client_factory=factory,
            )

    asyncio.run(run())

    assert observed == [False], "模型调用期间不应持有数据库事务"
    # 发送仍然成功：一问一答都已落库
    rows = _message_rows(pg_sync_engine, session_id)
    assert [row["role"] for row in rows] == ["USER", "ASSISTANT"]


# --------------------------------------------------------------------------- #
# 生成中归档：409 且不写消息
# --------------------------------------------------------------------------- #
def test_archive_during_generation_rejects_question(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """模型调用期间课程被归档 → 写入阶段读到 ARCHIVED → 409，不写消息。"""
    course_id, teacher, _ = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="tx-archive@example.com",
    )
    session_id = _start_session(client, teacher, course_id)
    user_id = _me_id(client, teacher)

    settings = make_settings(
        ai_base_url="http://fake-model.local/v1", ai_model="fake-model"
    )
    responder = _slow_responder()

    async def run() -> BaseException | None:
        async with pg_session_factory() as send_session, pg_session_factory() as ops:
            user = await send_session.get(User, user_id)
            teacher_user = await ops.get(User, user_id)
            assert user is not None and teacher_user is not None

            task = asyncio.create_task(
                chat_service.send_question(
                    send_session,
                    user=user,
                    session_id=uuid.UUID(session_id),
                    content="软件工程是应用系统化的方法",
                    settings=settings,
                    ai_client_factory=lambda: _fake_model_client(responder)(),
                )
            )
            # 等进入模型调用后再归档（模型调用期间没有活动事务，归档可立即完成）
            await asyncio.sleep(0.15)
            await courses_service.archive_course(
                ops, user=teacher_user, course_id=uuid.UUID(course_id)
            )
            try:
                await task
            except BaseException as exc:  # noqa: BLE001 - 断言异常类型
                return exc
            return None

    error = asyncio.run(run())

    assert isinstance(error, CourseArchivedError)
    assert _message_rows(pg_sync_engine, session_id) == []


# --------------------------------------------------------------------------- #
# 引用资料在生成期间被删除：按无依据回答处理
# --------------------------------------------------------------------------- #
def test_material_deleted_during_generation_degrades_to_ungrounded(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """引用资料按共享锁复查：生成期间已删除 → 丢弃引用、按无依据回答。"""
    course_id, teacher, material_id = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="tx-material-deleted@example.com",
    )
    session_id = _start_session(client, teacher, course_id)
    user_id = _me_id(client, teacher)

    settings = make_settings(
        ai_base_url="http://fake-model.local/v1", ai_model="fake-model"
    )
    responder = _slow_responder()

    async def run():
        async with pg_session_factory() as send_session, pg_session_factory() as ops:
            user = await send_session.get(User, user_id)
            teacher_user = await ops.get(User, user_id)
            assert user is not None and teacher_user is not None

            task = asyncio.create_task(
                chat_service.send_question(
                    send_session,
                    user=user,
                    session_id=uuid.UUID(session_id),
                    content="软件工程是应用系统化的方法",
                    settings=settings,
                    ai_client_factory=lambda: _fake_model_client(responder)(),
                )
            )
            await asyncio.sleep(0.15)
            # 生成期间删除资料（片段随之清空）
            await materials_service.delete_material(
                ops,
                user=teacher_user,
                material_id=uuid.UUID(material_id),
            )
            return await task

    message = asyncio.run(run())

    assert message.grounded is False
    assert message.content == NO_EVIDENCE_ANSWER
    rows = _message_rows(pg_sync_engine, session_id)
    assert [row["role"] for row in rows] == ["USER", "ASSISTANT"]
    assert rows[1]["grounded"] is False

    with pg_sync_engine.connect() as connection:
        citations = connection.execute(
            text("SELECT count(*) FROM chat_message_citations")
        ).scalar_one()
    assert citations == 0, "资料已失效时不得落库任何引用"


# --------------------------------------------------------------------------- #
# 创建会话与归档并发：按事务顺序得到一致结果
# --------------------------------------------------------------------------- #
def test_create_session_and_archive_concurrency_is_consistent(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """两者都在课程行锁内判定：创建成功则课程当时未归档，409 则无会话留下。"""
    course_id, teacher, _ = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="tx-create-archive@example.com",
    )
    user_id = _me_id(client, teacher)

    async def run():
        async with pg_session_factory() as create_db, pg_session_factory() as ops:
            user = await create_db.get(User, user_id)
            teacher_user = await ops.get(User, user_id)
            assert user is not None and teacher_user is not None

            created, archived = await asyncio.gather(
                chat_service.create_session(
                    create_db, user=user, course_id=uuid.UUID(course_id)
                ),
                courses_service.archive_course(
                    ops, user=teacher_user, course_id=uuid.UUID(course_id)
                ),
                return_exceptions=True,
            )
            return created, archived

    created, archived = asyncio.run(run())

    # 归档总是成功（或幂等返回），不会因为并发创建而失败
    assert not isinstance(archived, BaseException)

    with pg_sync_engine.connect() as connection:
        sessions = connection.execute(
            text("SELECT count(*) FROM chat_sessions")
        ).scalar_one()

    if isinstance(created, CourseArchivedError):
        # 归档先提交：不留下会话
        assert sessions == 0
    else:
        # 创建先提交：会话存在，且课程随后已归档
        assert not isinstance(created, BaseException)
        assert sessions == 1
