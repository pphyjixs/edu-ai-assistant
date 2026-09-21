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
import threading
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


def _course_status(pg_sync_engine, course_id: str) -> str:
    with pg_sync_engine.connect() as connection:
        return connection.execute(
            text("SELECT status FROM courses WHERE id = CAST(:id AS uuid)"),
            {"id": course_id},
        ).scalar_one()


def _citation_count(pg_sync_engine) -> int:
    with pg_sync_engine.connect() as connection:
        return connection.execute(
            text("SELECT count(*) FROM chat_message_citations")
        ).scalar_one()


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


def _gated_responder(started: threading.Event, release: threading.Event):
    """门控假模型：模型**真正开始**时通知测试，等测试放行后才返回。

    用它替代 ``sleep``：测试能在"模型已开始"这一确定时刻执行并发操作，
    并在操作提交后才让模型返回，不依赖任何时长假设。
    """
    inner = answering_responder()

    def responder(request: httpx.Request) -> httpx.Response:
        started.set()
        assert release.wait(timeout=10), "模型未被放行"
        return inner(request)

    return responder


class _CourseLockGate:
    """把课程行锁的获取过程暴露给测试（持有 → 通知 → 等放行）。

    ``hold_first=True`` 时第一次调用（先到的那个请求）在**持锁后**等待放行；
    后续调用记录"已到达加锁点"并正常阻塞，直到第一个事务提交。
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.modules.courses import repository as courses_repo

        self.first_acquired = asyncio.Event()
        self.first_release = asyncio.Event()
        self.second_attempt = asyncio.Event()
        self.second_acquired = asyncio.Event()
        self.calls = 0
        original = courses_repo.get_course_for_update

        async def traced(session, course_id):
            self.calls += 1
            if self.calls == 1:
                course = await original(session, course_id)
                self.first_acquired.set()
                await self.first_release.wait()
                return course
            self.second_attempt.set()
            course = await original(session, course_id)
            # 只有**真正拿到**课程行锁才会置位：被第一个事务挡住时不会到达这里
            self.second_acquired.set()
            return course

        monkeypatch.setattr(courses_repo, "get_course_for_update", traced)


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
    """模型已开始（问答不持事务）时归档 → 写入阶段读到 ARCHIVED → 409，不写消息。

    归档在模型返回**之前**完成，因此这里同时验证了"模型调用期间不持有事务"：
    若问答仍持有课程行锁或任何事务，归档会被阻塞而超时。
    """
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
    model_started = threading.Event()
    model_release = threading.Event()
    responder = _gated_responder(model_started, model_release)

    async def run() -> tuple[BaseException | None, bool]:
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
            # 等到模型真正开始（只读事务已结束），而不是靠固定等待时长
            started = await asyncio.to_thread(model_started.wait, 10)
            assert started, "模型未在超时内开始"
            assert send_session.in_transaction() is False

            # 归档必须能立即完成：问答此刻不持有任何锁
            archive_task = asyncio.create_task(
                courses_service.archive_course(
                    ops, user=teacher_user, course_id=uuid.UUID(course_id)
                )
            )
            await asyncio.wait_for(archive_task, timeout=5)
            archived_before_release = archive_task.done()

            model_release.set()
            try:
                await asyncio.wait_for(task, timeout=10)
            except BaseException as exc:  # noqa: BLE001 - 断言异常类型
                return exc, archived_before_release
            return None, archived_before_release

    error, archived_before_release = asyncio.run(run())

    assert archived_before_release, "归档不应被模型调用阻塞"
    assert isinstance(error, CourseArchivedError)
    assert _message_rows(pg_sync_engine, session_id) == []
    assert _course_status(pg_sync_engine, course_id) == "ARCHIVED"


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
    model_started = threading.Event()
    model_release = threading.Event()
    responder = _gated_responder(model_started, model_release)

    async def run() -> bool:
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
            started = await asyncio.to_thread(model_started.wait, 10)
            assert started, "模型未在超时内开始"

            # 生成期间删除资料（片段随之清空）：问答不持锁，删除立即完成
            delete_task = asyncio.create_task(
                materials_service.delete_material(
                    ops, user=teacher_user, material_id=uuid.UUID(material_id)
                )
            )
            await asyncio.wait_for(delete_task, timeout=5)
            deleted_before_release = delete_task.done()

            model_release.set()
            await asyncio.wait_for(task, timeout=10)
            return deleted_before_release

    deleted_before_release = asyncio.run(run())

    message_rows = _message_rows(pg_sync_engine, session_id)
    assert deleted_before_release, "模型调用期间问答不持锁，删除不应被阻塞"
    assert message_rows[1]["grounded"] is False
    assert message_rows[1]["content"] == NO_EVIDENCE_ANSWER
    assert _citation_count(pg_sync_engine) == 0, "资料已失效时不得落库任何引用"


def test_delete_waits_for_question_shared_lock(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """问答写入事务持有资料共享锁时，删除必须等到该事务结束。

    同步点放在「共享锁已加、事务尚未提交」处（``retrieval.match_section``
    在共享锁之后调用）：删除到达加锁点后必须**仍未完成**，放行问答提交后
    才完成；若共享锁被移除，删除会立即成功、本用例失败。
    """
    from app.modules.chat import retrieval as chat_retrieval
    from app.modules.materials import repository as materials_repo

    course_id, teacher, material_id = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="tx-shared-lock@example.com",
    )
    session_id = _start_session(client, teacher, course_id)
    user_id = _me_id(client, teacher)

    lock_taken = asyncio.Event()
    release_write = asyncio.Event()
    delete_attempted = asyncio.Event()
    delete_lock_acquired = asyncio.Event()

    original_match = chat_retrieval.match_section
    original_delete_lock = materials_repo.get_visible_material_for_update

    async def traced_match_section(*args, **kwargs):
        lock_taken.set()
        await release_write.wait()
        return await original_match(*args, **kwargs)

    async def traced_delete_lock(session, material_id):
        delete_attempted.set()
        material = await original_delete_lock(session, material_id)
        # 只有**真正拿到**资料行锁才会置位：被共享锁挡住时不会到达这里
        delete_lock_acquired.set()
        return material

    monkeypatch.setattr(chat_retrieval, "match_section", traced_match_section)
    monkeypatch.setattr(
        materials_repo, "get_visible_material_for_update", traced_delete_lock
    )

    settings = make_settings(
        ai_base_url="http://fake-model.local/v1", ai_model="fake-model"
    )
    responder = answering_responder()

    async def run() -> tuple[object, bool]:
        async with pg_session_factory() as send_session, pg_session_factory() as ops:
            user = await send_session.get(User, user_id)
            teacher_user = await ops.get(User, user_id)
            assert user is not None and teacher_user is not None

            question = asyncio.create_task(
                chat_service.send_question(
                    send_session,
                    user=user,
                    session_id=uuid.UUID(session_id),
                    content="软件工程是应用系统化的方法",
                    settings=settings,
                    ai_client_factory=lambda: _fake_model_client(responder)(),
                )
            )
            # 共享锁已加（写入事务进行中）
            await asyncio.wait_for(lock_taken.wait(), timeout=10)

            delete_task = asyncio.create_task(
                materials_service.delete_material(
                    ops, user=teacher_user, material_id=uuid.UUID(material_id)
                )
            )
            await asyncio.wait_for(delete_attempted.wait(), timeout=10)
            # 删除已尝试加锁：若共享锁生效，它拿不到锁（期望超时）；
            # 若锁被移除，删除会在毫秒内拿到锁，下面的断言随即失败。
            blocked = False
            try:
                await asyncio.wait_for(delete_lock_acquired.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                blocked = True

            release_write.set()
            message = await asyncio.wait_for(question, timeout=10)
            await asyncio.wait_for(delete_task, timeout=10)
            return message, blocked

    message, blocked = asyncio.run(run())

    assert blocked, "删除必须等待问答事务释放资料共享锁"
    # 问答先提交：引用快照落库，资料随后被删除（引用表不设外键，历史可回读）
    assert message.grounded is True
    assert _citation_count(pg_sync_engine) == 1
    assert _course_status(pg_sync_engine, course_id) == "ACTIVE"


# --------------------------------------------------------------------------- #
# 课程行锁的两个方向：创建先持锁 / 归档先持锁
# --------------------------------------------------------------------------- #
def _session_count(pg_sync_engine) -> int:
    with pg_sync_engine.connect() as connection:
        return connection.execute(
            text("SELECT count(*) FROM chat_sessions")
        ).scalar_one()


def test_create_session_holds_course_lock_before_archive(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """创建会话先持课程行锁：并发归档必须等待，创建提交后归档才生效。"""
    course_id, teacher, _ = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="tx-lock-create@example.com",
    )
    user_id = _me_id(client, teacher)
    gate = _CourseLockGate(monkeypatch)

    async def run() -> bool:
        async with pg_session_factory() as create_db, pg_session_factory() as ops:
            user = await create_db.get(User, user_id)
            teacher_user = await ops.get(User, user_id)
            assert user is not None and teacher_user is not None

            create_task = asyncio.create_task(
                chat_service.create_session(
                    create_db, user=user, course_id=uuid.UUID(course_id)
                )
            )
            # 创建已持有课程行锁（此时尚未提交）
            await asyncio.wait_for(gate.first_acquired.wait(), timeout=10)

            archive_task = asyncio.create_task(
                courses_service.archive_course(
                    ops, user=teacher_user, course_id=uuid.UUID(course_id)
                )
            )
            await asyncio.wait_for(gate.second_attempt.wait(), timeout=10)
            # 归档已尝试加锁：共享（此处为排他）行锁生效时它拿不到锁（期望超时）
            blocked = False
            try:
                await asyncio.wait_for(gate.second_acquired.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                blocked = True

            gate.first_release.set()
            await asyncio.wait_for(create_task, timeout=10)
            await asyncio.wait_for(archive_task, timeout=10)
            return blocked

    assert asyncio.run(run()) is True, "归档必须等待创建会话持有课程行锁"
    # 创建先提交：会话存在，归档随后生效
    assert _session_count(pg_sync_engine) == 1
    assert _course_status(pg_sync_engine, course_id) == "ARCHIVED"


def test_archive_holds_course_lock_before_create(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """归档先持课程行锁：并发创建必须等待，之后返回 409 且不留下会话。"""
    course_id, teacher, _ = _create_course_with_material(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email="tx-lock-archive@example.com",
    )
    user_id = _me_id(client, teacher)
    gate = _CourseLockGate(monkeypatch)

    async def run() -> tuple[bool, BaseException | None]:
        async with pg_session_factory() as create_db, pg_session_factory() as ops:
            user = await create_db.get(User, user_id)
            teacher_user = await ops.get(User, user_id)
            assert user is not None and teacher_user is not None

            archive_task = asyncio.create_task(
                courses_service.archive_course(
                    ops, user=teacher_user, course_id=uuid.UUID(course_id)
                )
            )
            # 归档已持有课程行锁（此时尚未提交）
            await asyncio.wait_for(gate.first_acquired.wait(), timeout=10)

            create_task = asyncio.create_task(
                chat_service.create_session(
                    create_db, user=user, course_id=uuid.UUID(course_id)
                )
            )
            await asyncio.wait_for(gate.second_attempt.wait(), timeout=10)
            # 创建已尝试加锁：归档持有行锁时它拿不到锁（期望超时）
            blocked = False
            try:
                await asyncio.wait_for(gate.second_acquired.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                blocked = True

            gate.first_release.set()
            await asyncio.wait_for(archive_task, timeout=10)

            error: BaseException | None = None
            try:
                await asyncio.wait_for(create_task, timeout=10)
            except BaseException as exc:  # noqa: BLE001 - 断言异常类型
                error = exc
            return blocked, error

    blocked, error = asyncio.run(run())

    assert blocked is True, "创建会话必须等待归档持有课程行锁"
    assert isinstance(error, CourseArchivedError)
    assert _session_count(pg_sync_engine) == 0
    assert _course_status(pg_sync_engine, course_id) == "ARCHIVED"


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
