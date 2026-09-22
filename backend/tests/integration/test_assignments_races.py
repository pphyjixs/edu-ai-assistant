"""实验任务的确定性并发回归（真实 PostgreSQL，``docs/api-contract.md`` 第 8 节）。

覆盖：

- 并发评分规则修改 → 版本号连续且唯一；
- 修改与发布、修改与关闭并发 → 只产生符合串行顺序的状态；
- 创建、修改、发布、关闭分别与课程归档并发（两个方向）；
  操作先持课程锁（归档等待，操作提交后归档完成）、归档先持课程锁
  （操作 ``409 COURSE_ARCHIVED`` 且无副作用）。

实现要点：**不使用固定休眠**——用跨线程事件门控决定时序，用"锁被持有时请求
在给定时限内不可能完成"的断言证明行锁确实生效。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.modules.courses import service as courses_service
from tests.integration.test_assignments_api import (
    CLOSE_URL,
    CREATE_URL,
    DETAIL_URL,
    PUBLISH_URL,
    _course_with_members,
    _create,
    _payload,
)
from tests.integration.test_materials_api import _auth

ARCHIVE_URL = "/api/v1/courses/{course_id}/archive"

#: 断言"请求被行锁挡住"的观察窗口：真被挡住时不可能提前完成
BLOCK_WINDOW_SECONDS = 1.0


@pytest.fixture
def client(db_isolation: None, pg_app) -> Iterator[TestClient]:
    with TestClient(pg_app()) as test_client:
        yield test_client


class Gate:
    """跨线程事件门控：请求侧在持锁状态等待，测试侧负责放行。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    async def hold(self) -> None:
        self.entered.set()
        # 在独立线程上等待放行，避免阻塞事件循环（否则归档请求无法被处理）
        await asyncio.to_thread(self.release.wait, 30)

    def wait_entered(self, timeout: float = 10.0) -> None:
        assert self.entered.wait(timeout), "请求未到达持锁点（门控超时）"

    def let_go(self) -> None:
        self.release.set()


class RowLock:
    """在独立连接上持有某一行的排他锁（模拟并发方先拿走行锁）。"""

    def __init__(self, engine, table: str, row_id: str) -> None:
        self._row_id = row_id
        self._connection = engine.connect()
        self._transaction = self._connection.begin()
        self._connection.execute(
            text(f"SELECT id FROM {table} WHERE id = CAST(:id AS uuid) FOR UPDATE"),
            {"id": row_id},
        )

    def run(self, sql: str) -> None:
        self._connection.execute(text(sql), {"id": self._row_id})

    def commit(self) -> None:
        self._transaction.commit()

    def close(self) -> None:
        try:
            if self._transaction.is_active:
                self._transaction.rollback()
        finally:
            self._connection.close()


def _assert_blocked(future: Future, *, seconds: float = BLOCK_WINDOW_SECONDS) -> None:
    """在观察窗口内断言请求仍未完成（即确实被行锁挡住）。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if future.done():
            raise AssertionError(f"请求未被行锁挡住，提前完成：{future.result()}")
        time.sleep(0.01)


def _install_course_lock_gate(monkeypatch: pytest.MonkeyPatch, gate: Gate) -> None:
    """在"课程行锁已取得"之后挂门控。

    ``is_course_member`` 在 ``_lock_visible_course`` 中于课程行锁之后调用，
    因此在这里等待时，请求已经持有课程行锁。
    """
    real = courses_service.is_course_member

    async def hooked(*args, **kwargs):
        await gate.hold()
        return await real(*args, **kwargs)

    monkeypatch.setattr(courses_service, "is_course_member", hooked)


def _course_state(pg_sync_engine, course_id: str) -> str:
    with pg_sync_engine.connect() as connection:
        return connection.execute(
            text("SELECT status FROM courses WHERE id = CAST(:id AS uuid)"),
            {"id": course_id},
        ).scalar_one()


def _assignment_row(pg_sync_engine, assignment_id: str) -> dict:
    with pg_sync_engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT title, status, published_at, closed_at,"
                    " current_rubric_version_id FROM assignments"
                    " WHERE id = CAST(:id AS uuid)"
                ),
                {"id": assignment_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def _version_numbers(pg_sync_engine, assignment_id: str) -> list[int]:
    with pg_sync_engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT version FROM assignment_rubric_versions"
                    " WHERE assignment_id = CAST(:id AS uuid) ORDER BY version"
                ),
                {"id": assignment_id},
            )
            .scalars()
            .all()
        )


def _current_rubric_version_id(pg_sync_engine, assignment_id: str) -> str:
    with pg_sync_engine.connect() as connection:
        return str(
            connection.execute(
                text(
                    "SELECT current_rubric_version_id FROM assignments"
                    " WHERE id = CAST(:id AS uuid)"
                ),
                {"id": assignment_id},
            ).scalar_one()
        )


def _assignment_count(pg_sync_engine, course_id: str) -> int:
    with pg_sync_engine.connect() as connection:
        return int(
            connection.execute(
                text(
                    "SELECT count(*) FROM assignments WHERE course_id = CAST(:id AS uuid)"
                ),
                {"id": course_id},
            ).scalar_one()
        )


def _concurrent(fn, count: int = 2) -> list[httpx.Response]:
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(fn) for _ in range(count)]
        return [future.result(timeout=30) for future in futures]


# --------------------------------------------------------------------------- #
# 并发评分规则修改 → 版本号连续且唯一
# --------------------------------------------------------------------------- #
def test_concurrent_rubric_updates_produce_contiguous_versions(
    client: TestClient,
    pg_sync_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个并发的评分规则修改串行执行，版本号连续且唯一。"""
    course_id, teacher, _student = _course_with_members(client, "race-version")
    created = _create(client, course_id, teacher).json()
    assignment_id = created["id"]
    url = DETAIL_URL.format(assignment_id=assignment_id)

    gate = Gate()
    _install_course_lock_gate(monkeypatch, gate)

    def patch_rubric(title: str, score: float):
        def call() -> httpx.Response:
            return client.patch(
                url,
                json={
                    "rubric_items": [
                        {"title": title, "description": "", "max_score": score, "order": 1}
                    ]
                },
                headers=_auth(teacher),
            )

        return call

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(patch_rubric("改动一", 100))
        gate.wait_entered()  # 第一个请求已持有课程行锁
        second = pool.submit(patch_rubric("改动二", 100))
        _assert_blocked(second)  # 第二个请求在课程行锁上等待
        gate.let_go()
        responses = [first.result(timeout=30), second.result(timeout=30)]

    assert [response.status_code for response in responses] == [200, 200]
    assert _version_numbers(pg_sync_engine, assignment_id) == [1, 2, 3]
    final = client.get(url, headers=_auth(teacher)).json()
    assert final["rubric_version"] == 3
    # 每次修改都生成了全新的评分项 ID
    assert len({item["id"] for item in final["rubric_items"]}) == 1


# --------------------------------------------------------------------------- #
# 修改与发布 / 修改与关闭并发 → 只产生符合串行顺序的状态
# --------------------------------------------------------------------------- #
def test_update_and_publish_concurrency_serializes(
    client: TestClient,
    pg_sync_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """修改与发布并发：结果只能是"先改后发"或"先发后改"，且都合法。"""
    course_id, teacher, _student = _course_with_members(client, "race-publish")
    created = _create(client, course_id, teacher).json()
    assignment_id = created["id"]

    gate = Gate()
    _install_course_lock_gate(monkeypatch, gate)

    def do_patch() -> httpx.Response:
        return client.patch(
            DETAIL_URL.format(assignment_id=assignment_id),
            json={"rubric_items": [{"title": "新规则", "max_score": 100, "order": 1}]},
            headers=_auth(teacher),
        )

    def do_publish() -> httpx.Response:
        return client.post(
            PUBLISH_URL.format(assignment_id=assignment_id), headers=_auth(teacher)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        patch_future = pool.submit(do_patch)
        gate.wait_entered()
        publish_future = pool.submit(do_publish)
        _assert_blocked(publish_future)
        gate.let_go()
        patch_response = patch_future.result(timeout=30)
        publish_response = publish_future.result(timeout=30)

    assert patch_response.status_code == 200
    assert publish_response.status_code == 200
    # 串行结果：修改先生效（版本 2），发布随后把状态推进到 PUBLISHED
    assert _version_numbers(pg_sync_engine, assignment_id) == [1, 2]
    row = _assignment_row(pg_sync_engine, assignment_id)
    assert row["status"] == "PUBLISHED"
    assert row["published_at"] is not None


def test_update_and_close_concurrency_serializes(
    client: TestClient,
    pg_sync_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """修改与关闭并发：关闭后到达的修改必须被拒，不能出现"已关闭又被改"。"""
    course_id, teacher, _student = _course_with_members(client, "race-close")
    created = _create(client, course_id, teacher).json()
    assignment_id = created["id"]
    publish_url = PUBLISH_URL.format(assignment_id=assignment_id)
    assert client.post(publish_url, headers=_auth(teacher)).status_code == 200

    gate = Gate()
    _install_course_lock_gate(monkeypatch, gate)

    def do_close() -> httpx.Response:
        return client.post(
            CLOSE_URL.format(assignment_id=assignment_id), headers=_auth(teacher)
        )

    def do_patch() -> httpx.Response:
        return client.patch(
            DETAIL_URL.format(assignment_id=assignment_id),
            json={"title": "关闭后的修改"},
            headers=_auth(teacher),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        close_future = pool.submit(do_close)
        gate.wait_entered()
        patch_future = pool.submit(do_patch)
        _assert_blocked(patch_future)
        gate.let_go()
        close_response = close_future.result(timeout=30)
        patch_response = patch_future.result(timeout=30)

    assert close_response.status_code == 200
    row = _assignment_row(pg_sync_engine, assignment_id)
    assert row["status"] == "CLOSED"
    assert row["closed_at"] is not None

    if patch_response.status_code == 200:
        # 修改先于关闭生效：标题被改，但状态最终仍是 CLOSED
        assert row["title"] == "关闭后的修改"
        assert _version_numbers(pg_sync_engine, assignment_id) == [1]
    else:
        # 关闭先生效：修改被状态检查拒绝，标题保持原样
        assert patch_response.status_code == 409
        assert patch_response.json()["error"]["code"] == "ASSIGNMENT_NOT_OPEN"
        assert row["title"] == created["title"]


# --------------------------------------------------------------------------- #
# 当前 RubricVersion 行锁（契约 8.1：课程 → 任务 → 当前评分版本）
# --------------------------------------------------------------------------- #
#: 需要拿课程、任务与当前评分版本三把行锁的写操作（创建时还没有版本行）
VERSION_LOCKED_OPERATIONS = ("patch", "publish", "close")


@pytest.mark.parametrize("name", VERSION_LOCKED_OPERATIONS)
def test_write_operation_waits_for_rubric_version_lock(
    name: str,
    client: TestClient,
    pg_sync_engine,
) -> None:
    """直接持有当前评分版本行锁：写请求必须等待，放锁后才完成。

    这条用例是锁协议本身的回归：删掉 ``lock_rubric_version``（或把它退回普通
    ``session.get()``）后，请求会在观察窗口内直接完成，断言随即失败。
    """
    course_id, teacher, _student = _course_with_members(client, f"race-vlock-{name}")
    assignment_id = _prepare(name, client, teacher, course_id)
    assert assignment_id is not None
    baseline = _snapshot(pg_sync_engine, course_id, assignment_id)

    lock = RowLock(
        pg_sync_engine,
        "assignment_rubric_versions",
        _current_rubric_version_id(pg_sync_engine, assignment_id),
    )
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        operation = pool.submit(_call, name, client, teacher, course_id, assignment_id)
        _assert_blocked(operation)  # 请求在版本行锁上等待
        # 等待期间没有任何已提交的副作用
        assert _snapshot(pg_sync_engine, course_id, assignment_id) == baseline
        lock.commit()  # 放锁，请求得以继续
        response = operation.result(timeout=30)
    finally:
        lock.close()
        pool.shutdown(wait=True)

    assert response.status_code == _success_status(name), response.text
    _assert_applied(name, pg_sync_engine, course_id, assignment_id, baseline)
    # 版本行锁不新增版本：三把行锁都不改动评分规则
    assert _version_numbers(pg_sync_engine, assignment_id) == [1]


# --------------------------------------------------------------------------- #
# 写操作 vs 课程归档（两个方向）
# --------------------------------------------------------------------------- #
OPERATIONS = ("create", "patch", "publish", "close")

_PATCH_TITLE = "并发修改"


def _prepare(
    name: str, client: TestClient, teacher: str, course_id: str
) -> str | None:
    """准备到"刚好可以发起该操作"的状态，返回 assignment_id（创建操作为 None）。"""
    if name == "create":
        return None
    created = _create(client, course_id, teacher).json()
    assignment_id = created["id"]
    if name == "close":
        published = client.post(
            PUBLISH_URL.format(assignment_id=assignment_id), headers=_auth(teacher)
        )
        assert published.status_code == 200, published.text
    return assignment_id


def _call(
    name: str,
    client: TestClient,
    teacher: str,
    course_id: str,
    assignment_id: str | None,
) -> httpx.Response:
    if name == "create":
        return client.post(
            CREATE_URL.format(course_id=course_id),
            json=_payload(),
            headers=_auth(teacher),
        )
    if name == "patch":
        return client.patch(
            DETAIL_URL.format(assignment_id=assignment_id),
            json={"title": _PATCH_TITLE},
            headers=_auth(teacher),
        )
    if name == "publish":
        return client.post(
            PUBLISH_URL.format(assignment_id=assignment_id), headers=_auth(teacher)
        )
    return client.post(
        CLOSE_URL.format(assignment_id=assignment_id), headers=_auth(teacher)
    )


def _success_status(name: str) -> int:
    return 201 if name == "create" else 200


def _snapshot(pg_sync_engine, course_id: str, assignment_id: str | None) -> dict:
    snapshot: dict = {"count": _assignment_count(pg_sync_engine, course_id)}
    if assignment_id is not None:
        snapshot.update(_assignment_row(pg_sync_engine, assignment_id))
    return snapshot


def _assert_applied(
    name: str, pg_sync_engine, course_id: str, assignment_id: str | None, baseline: dict
) -> None:
    """操作确实生效。"""
    if name == "create":
        assert _assignment_count(pg_sync_engine, course_id) == baseline["count"] + 1
        return
    row = _assignment_row(pg_sync_engine, assignment_id)
    if name == "patch":
        assert row["title"] == _PATCH_TITLE
    elif name == "publish":
        assert row["status"] == "PUBLISHED" and row["published_at"] is not None
    else:
        assert row["status"] == "CLOSED" and row["closed_at"] is not None


def _assert_not_applied(
    name: str, pg_sync_engine, course_id: str, assignment_id: str | None, baseline: dict
) -> None:
    """操作没有留下任何副作用。"""
    if name == "create":
        assert _assignment_count(pg_sync_engine, course_id) == baseline["count"]
        return
    row = _assignment_row(pg_sync_engine, assignment_id)
    assert row["title"] == baseline["title"]
    assert row["status"] == baseline["status"]
    assert row["published_at"] == baseline["published_at"]
    assert row["closed_at"] == baseline["closed_at"]


@pytest.mark.parametrize("name", OPERATIONS)
def test_operation_holding_course_lock_blocks_archive(
    name: str,
    client: TestClient,
    pg_sync_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """操作先持课程锁：归档等待，操作提交后归档完成。"""
    course_id, teacher, _student = _course_with_members(client, f"race-a-{name}")
    assignment_id = _prepare(name, client, teacher, course_id)
    baseline = _snapshot(pg_sync_engine, course_id, assignment_id)

    gate = Gate()
    _install_course_lock_gate(monkeypatch, gate)

    with ThreadPoolExecutor(max_workers=2) as pool:
        operation = pool.submit(_call, name, client, teacher, course_id, assignment_id)
        gate.wait_entered()  # 操作已持有课程行锁
        archive = pool.submit(
            lambda: client.post(
                ARCHIVE_URL.format(course_id=course_id), headers=_auth(teacher)
            )
        )
        _assert_blocked(archive)  # 归档在课程行锁上等待
        assert _course_state(pg_sync_engine, course_id) == "ACTIVE"
        gate.let_go()
        operation_response = operation.result(timeout=30)
        archive_response = archive.result(timeout=30)

    assert operation_response.status_code == _success_status(name), operation_response.text
    assert archive_response.status_code == 200, archive_response.text
    assert _course_state(pg_sync_engine, course_id) == "ARCHIVED"
    _assert_applied(name, pg_sync_engine, course_id, assignment_id, baseline)


@pytest.mark.parametrize("name", OPERATIONS)
def test_archive_holding_course_lock_rejects_operation(
    name: str,
    client: TestClient,
    pg_sync_engine,
) -> None:
    """归档先持课程锁：操作等待，归档提交后操作 409 且无副作用。"""
    course_id, teacher, _student = _course_with_members(client, f"race-b-{name}")
    assignment_id = _prepare(name, client, teacher, course_id)
    baseline = _snapshot(pg_sync_engine, course_id, assignment_id)

    lock = RowLock(pg_sync_engine, "courses", course_id)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        operation = pool.submit(_call, name, client, teacher, course_id, assignment_id)
        _assert_blocked(operation)  # 操作在课程行锁上等待
        # 归档在持锁状态下提交（等价于归档接口先完成）
        lock.run(
            "UPDATE courses SET status = 'ARCHIVED'::course_status,"
            " updated_at = now() WHERE id = CAST(:id AS uuid)"
        )
        lock.commit()
        response = operation.result(timeout=30)
    finally:
        lock.close()
        pool.shutdown(wait=True)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "COURSE_ARCHIVED"
    assert _course_state(pg_sync_engine, course_id) == "ARCHIVED"
    _assert_not_applied(name, pg_sync_engine, course_id, assignment_id, baseline)
