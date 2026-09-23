"""提交与批改的确定性并发回归（真实 PostgreSQL，``docs/api-contract.md`` 第 9 节）。

覆盖：

- 并发初始化上传、并发完成提交 → 只产生一份提交（``(assignment, student)`` 唯一）；
- 完成提交与任务关闭、Rubric 修改、课程归档：统一锁顺序下按串行结果生效；
- 并发触发批改 → 只产生一个 ``SUBMISSION_GRADE`` 任务；
- 过期上传清理与完成请求并发 → **不会**删除已确认的报告对象（``SKIP LOCKED``）。

实现要点：**不使用固定休眠**——用独立连接持有真实行锁，断言"锁被持有时请求在给定时限内
不可能完成"，因此这些用例可以捕获"临时删除锁"的变异回归。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.modules.grading import service as grading_service
from app.modules.grading import worker
from app.storage.deps import get_storage_dep
from tests.integration.test_grading_api import (
    COMPLETE_URL,
    DETAIL_URL,
    GRADE_URL,
    REVIEW_PATCH_URL,
    REVIEW_PUBLISH_URL,
    UPLOADS_URL,
    _auth,
    _graded_review,
    _grading_settings,
    _init_upload,
    _open_assignment,
    _simulate_put,
    _submit_report,
    fake_model_response,
    PDF_MIME,
    REPORT_SHA256,
    REPORT_SIZE,
)
from tests.storage_fake import FakeStorage

#: 断言"请求被行锁挡住"的观察窗口：真被挡住时不可能提前完成
BLOCK_WINDOW_SECONDS = 1.0


def _cleanup_settings(make_settings) -> object:
    """清理用例需要的配置：只关心删除缓冲期。"""
    return make_settings(submission_upload_delete_buffer_seconds=3600)


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage: FakeStorage) -> Iterator[TestClient]:
    app = pg_app()
    app.dependency_overrides[get_storage_dep] = lambda: fake_storage
    with TestClient(app) as test_client:
        yield test_client


class RowLock:
    """在独立连接上持有某一行的排他锁（模拟并发方先拿走行锁）。"""

    def __init__(self, engine: Engine, table: str, row_id: str) -> None:
        self._connection = engine.connect()
        self._transaction = self._connection.begin()
        self._connection.execute(
            text(f"SELECT id FROM {table} WHERE id = CAST(:id AS uuid) FOR UPDATE"),
            {"id": str(row_id)},
        )

    def release(self) -> None:
        self._transaction.rollback()
        self._connection.close()


def _assert_blocked(future, *, message: str) -> None:
    """在给定时限内请求必须仍未完成（否则说明没有取到应有的行锁）。"""
    time.sleep(BLOCK_WINDOW_SECONDS)
    assert not future.done(), message


# --------------------------------------------------------------------------- #
# 并发初始化 / 完成：只能产生一份提交
# --------------------------------------------------------------------------- #
def test_concurrent_init_upload_reuses_single_submission(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """并发初始化只产生一份提交：复用同一 ``submission_id``，各自获得新的 upload。"""
    _course, _teacher, student, assignment = _open_assignment(client)

    async def run() -> list:
        async with _async_client(client.app) as async_client:
            payload = {
                "filename": "report.pdf",
                "content_type": PDF_MIME,
                "size": REPORT_SIZE,
                "sha256": REPORT_SHA256,
            }
            return await asyncio.gather(
                *[
                    async_client.post(
                        UPLOADS_URL.format(assignment_id=assignment["id"]),
                        json=payload,
                        headers=_auth(student),
                    )
                    for _ in range(2)
                ]
            )

    responses = asyncio.run(run())

    assert [response.status_code for response in responses] == [201, 201]
    submission_ids = {response.json()["submission_id"] for response in responses}
    upload_ids = {response.json()["upload_id"] for response in responses}
    assert len(submission_ids) == 1
    assert len(upload_ids) == 2

    with pg_sync_engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM submissions")
        ).scalar_one()
    assert count == 1


def test_concurrent_complete_produces_single_submission_row(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """同一 upload 并发完成：两次都成功且返回同一份提交，库里只有一行。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])
    _simulate_put(client, fake_storage, init)

    async def run() -> list:
        async with _async_client(client.app) as async_client:
            return await asyncio.gather(
                *[
                    async_client.post(
                        COMPLETE_URL.format(
                            assignment_id=assignment["id"],
                            upload_id=init["upload_id"],
                        ),
                        headers=_auth(student),
                    )
                    for _ in range(2)
                ]
            )

    responses = asyncio.run(run())

    assert all(response.status_code == 201 for response in responses), [
        response.text for response in responses
    ]
    assert len({response.json()["id"] for response in responses}) == 1

    with pg_sync_engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM submissions")
        ).scalar_one() == 1


# --------------------------------------------------------------------------- #
# 统一锁顺序：请求确实要等到上一把行锁
# --------------------------------------------------------------------------- #
def test_complete_waits_for_course_row_lock(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """完成提交必须先拿到课程行锁（锁顺序的第一步）。"""
    course_id, _teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])
    _simulate_put(client, fake_storage, init)

    lock = RowLock(pg_sync_engine, "courses", course_id)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                lambda: client.post(
                    COMPLETE_URL.format(
                        assignment_id=assignment["id"], upload_id=init["upload_id"]
                    ),
                    headers=_auth(student),
                )
            )
            _assert_blocked(future, message="完成提交必须先拿课程行锁")
            lock.release()
            assert future.result(timeout=15).status_code == 201
    finally:
        lock.release()


def test_close_waits_for_assignment_row_lock(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """关闭任务同样在任务行锁上排队：与完成提交共处同一串行化临界区。"""
    _course, teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])
    _simulate_put(client, fake_storage, init)

    lock = RowLock(pg_sync_engine, "assignments", assignment["id"])
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                lambda: client.post(
                    f"/api/v1/assignments/{assignment['id']}/close",
                    headers=_auth(teacher),
                )
            )
            _assert_blocked(future, message="关闭任务必须先拿任务行锁")
            lock.release()
            assert future.result(timeout=15).status_code == 200
    finally:
        lock.release()

    # 关闭先完成：随后的完成提交被状态检查拒绝
    late = client.post(
        COMPLETE_URL.format(assignment_id=assignment["id"], upload_id=init["upload_id"]),
        headers=_auth(student),
    )
    assert late.status_code == 409, late.text
    assert late.json()["error"]["code"] == "ASSIGNMENT_NOT_OPEN"


def test_rubric_update_waits_for_assignments_current_version_lock(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """修改 Rubric 要拿"当前评分版本"行锁：与完成提交共用同一把锁。"""
    _course, teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])
    _simulate_put(client, fake_storage, init)
    completed = client.post(
        COMPLETE_URL.format(assignment_id=assignment["id"], upload_id=init["upload_id"]),
        headers=_auth(student),
    )
    assert completed.status_code == 201, completed.text

    with pg_sync_engine.connect() as connection:
        version_id = connection.execute(
            text(
                "SELECT current_rubric_version_id FROM assignments"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": assignment["id"]},
        ).scalar_one()

    lock = RowLock(pg_sync_engine, "assignment_rubric_versions", version_id)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                lambda: client.patch(
                    f"/api/v1/assignments/{assignment['id']}",
                    json={"total_score": 100},
                    headers=_auth(teacher),
                )
            )
            _assert_blocked(future, message="修改任务必须先拿当前评分版本行锁")
            lock.release()
            assert future.result(timeout=15).status_code == 200
    finally:
        lock.release()


def test_archive_waits_for_course_row_lock(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """课程归档与提交写入共用课程行锁：两个方向都串行化。"""
    course_id, teacher, _student, assignment = _open_assignment(client)

    lock = RowLock(pg_sync_engine, "courses", course_id)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                lambda: client.post(
                    f"/api/v1/courses/{course_id}/archive", headers=_auth(teacher)
                )
            )
            _assert_blocked(future, message="归档课程必须先拿课程行锁")
            lock.release()
            assert future.result(timeout=15).status_code == 200
    finally:
        lock.release()

    # 归档已生效：后续写入统一 409
    blocked = client.post(
        f"/api/v1/assignments/{assignment['id']}/publish", headers=_auth(teacher)
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"]["code"] == "COURSE_ARCHIVED"


# --------------------------------------------------------------------------- #
# 触发批改：只产生一个任务
# --------------------------------------------------------------------------- #
def test_concurrent_grade_trigger_creates_single_job(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    _course, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])

    async def run() -> list:
        async with _async_client(client.app) as async_client:
            return await asyncio.gather(
                *[
                    async_client.post(
                        GRADE_URL.format(submission_id=detail["id"]),
                        headers=_auth(teacher),
                    )
                    for _ in range(3)
                ]
            )

    responses = asyncio.run(run())

    assert all(response.status_code == 202 for response in responses), [
        response.text for response in responses
    ]
    assert len({response.json()["id"] for response in responses}) == 1
    with pg_sync_engine.connect() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM jobs WHERE type = 'SUBMISSION_GRADE'")
        ).scalar_one() == 1


def test_grade_trigger_waits_for_job_row_lock(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """触发批改必须等任务行锁：并发的第二次触发不会重置执行中的任务。"""
    _course, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])
    job = client.post(
        GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher)
    ).json()

    lock = RowLock(pg_sync_engine, "jobs", job["id"])
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                lambda: client.post(
                    GRADE_URL.format(submission_id=detail["id"]),
                    headers=_auth(teacher),
                )
            )
            _assert_blocked(future, message="触发批改必须先拿任务行锁")
            lock.release()
            assert future.result(timeout=15).status_code == 202
    finally:
        lock.release()


def test_retry_waits_for_submission_row_lock(
    client, fake_storage, pg_sync_engine: Engine, pg_session_factory, make_settings
) -> None:
    """重试与旧执行者回写互斥：任务行锁被 Worker 持有期间重试必须等待。"""
    _course, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])
    client.post(GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher))

    # 让 Worker 失败，得到可重试的任务
    fake_storage.as_unavailable()
    assert asyncio.run(
        worker.run_pending_batch(
            pg_session_factory,
            settings=_grading_settings(make_settings),
            storage=fake_storage,
            ai_client_factory=lambda: __import__("httpx").Client(
                transport=fake_model_response(), timeout=10.0
            ),
            max_jobs=1,
        )
    ) == 1
    fake_storage.as_available()

    lock = RowLock(pg_sync_engine, "submissions", detail["id"])
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                lambda: client.post(
                    GRADE_URL.format(submission_id=detail["id"]),
                    headers=_auth(teacher),
                )
            )
            _assert_blocked(future, message="重试必须先拿提交行锁")
            lock.release()
            assert future.result(timeout=15).status_code == 202
    finally:
        lock.release()


def test_trigger_decides_after_submission_lock(
    client, fake_storage, pg_sync_engine: Engine, pg_session_factory, make_settings
) -> None:
    """触发必须**拿到提交行锁之后**才读取状态做分流（契约 9.6）。

    持锁期间把提交推进到 ``REVIEW_REQUIRED``（模拟 Worker 回写完成），触发在
    拿到锁后必须看到最新状态并返回 ``409 SUBMISSION_NOT_READY``，且不重置任务。
    若实现"先读状态后加锁"，触发会按过期的 ``FAILED`` 状态重置任务并返回 202。
    """
    _course, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])
    client.post(GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher))

    # Worker 失败 → FAILED（可重试）
    fake_storage.as_unavailable()
    assert asyncio.run(
        worker.run_pending_batch(
            pg_session_factory,
            settings=_grading_settings(make_settings),
            storage=fake_storage,
            ai_client_factory=lambda: __import__("httpx").Client(
                transport=fake_model_response(), timeout=10.0
            ),
            max_jobs=1,
        )
    ) == 1
    fake_storage.as_available()

    holder = pg_sync_engine.connect()
    holder_tx = holder.begin()
    holder.execute(
        text("SELECT id FROM submissions WHERE id = CAST(:id AS uuid) FOR UPDATE"),
        {"id": detail["id"]},
    )
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            lambda: client.post(
                GRADE_URL.format(submission_id=detail["id"]),
                headers=_auth(teacher),
            )
        )
        _assert_blocked(future, message="触发必须先拿提交行锁")

        # 持锁推进状态（同事务，天然拥有行锁），提交即释放锁并使新状态可见
        holder.execute(
            text(
                "UPDATE submissions SET status = 'REVIEW_REQUIRED',"
                " error_message = NULL, updated_at = now()"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": detail["id"]},
        )
        holder_tx.commit()
    finally:
        pool.shutdown(wait=True)
        holder.close()

    response = future.result(timeout=15)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SUBMISSION_NOT_READY"

    with pg_sync_engine.connect() as connection:
        job_status = connection.execute(
            text(
                "SELECT status FROM jobs"
                " WHERE type = 'SUBMISSION_GRADE'"
                " AND resource_id = CAST(:id AS uuid)"
            ),
            {"id": detail["id"]},
        ).scalar_one()
    # 任务不得被过期状态的触发重置
    assert job_status == "FAILED"


# --------------------------------------------------------------------------- #
# 清理与完成并发：绝不删除已确认对象
# --------------------------------------------------------------------------- #
def test_cleanup_skips_sessions_locked_by_complete(
    client, fake_storage, pg_sync_engine: Engine, pg_session_factory, make_settings
) -> None:
    """清理用 ``SKIP LOCKED``：会话行被完成请求持有时直接跳过，不误删对象。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])
    object_key = _simulate_put(client, fake_storage, init)

    lock = RowLock(pg_sync_engine, "submission_upload_sessions", init["upload_id"])
    try:

        async def run_cleanup() -> int:
            async with pg_session_factory() as session:
                return await grading_service.cleanup_expired_submission_uploads(
                    session,
                    storage=fake_storage,
                    settings=_cleanup_settings(make_settings),
                )

        cleaned = asyncio.run(run_cleanup())
        assert cleaned == 0
        assert object_key in fake_storage.objects
    finally:
        lock.release()

    with pg_sync_engine.connect() as connection:
        expired = connection.execute(
            text(
                "SELECT expired_at FROM submission_upload_sessions"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": init["upload_id"]},
        ).scalar_one()
    assert expired is None


def test_complete_locks_upload_session_row(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """完成请求必须取得上传会话行锁：清理因此能用 ``SKIP LOCKED`` 安全跳过。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])
    object_key = _simulate_put(client, fake_storage, init)

    lock = RowLock(pg_sync_engine, "submission_upload_sessions", init["upload_id"])
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            lambda: client.post(
                COMPLETE_URL.format(
                    assignment_id=assignment["id"], upload_id=init["upload_id"]
                ),
                headers=_auth(student),
            )
        )
        _assert_blocked(future, message="完成请求必须先取得上传会话行锁")
    finally:
        lock.release()
        pool.shutdown(wait=True)

    assert future.result(timeout=15).status_code == 201
    assert object_key in fake_storage.objects


def test_complete_rechecks_session_state_after_lock(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """完成请求必须在**拿到会话行锁之后**复核会话状态（契约 9.3）。

    持锁期间会话被标记"被替代"（模拟新的初始化上传），完成请求在拿到锁后
    必须返回 ``422 UPLOAD_SUPERSEDED`` 且不写下提交；若实现"先读状态后加锁"，
    会按过期状态完成提交并返回 ``201``。
    """
    _course, _teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])
    object_key = _simulate_put(client, fake_storage, init)

    holder = pg_sync_engine.connect()
    holder_tx = holder.begin()
    holder.execute(
        text(
            "SELECT id FROM submission_upload_sessions"
            " WHERE id = CAST(:id AS uuid) FOR UPDATE"
        ),
        {"id": init["upload_id"]},
    )
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            lambda: client.post(
                COMPLETE_URL.format(
                    assignment_id=assignment["id"], upload_id=init["upload_id"]
                ),
                headers=_auth(student),
            )
        )
        _assert_blocked(future, message="完成请求必须先取得上传会话行锁")

        # 持锁标记"被替代"（同事务天然拥有行锁），提交即释放锁并可见
        holder.execute(
            text(
                "UPDATE submission_upload_sessions"
                " SET superseded_at = now(), updated_at = now()"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": init["upload_id"]},
        )
        holder_tx.commit()
    finally:
        pool.shutdown(wait=True)
        holder.close()

    response = future.result(timeout=15)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "UPLOAD_INVALID"
    assert error["details"]["reason"] == "UPLOAD_SUPERSEDED"

    # 提交不得被完成
    with pg_sync_engine.connect() as connection:
        submitted_at = connection.execute(
            text(
                "SELECT submitted_at FROM submissions"
                " WHERE object_key = :key"
            ),
            {"key": object_key},
        ).scalar_one()
    assert submitted_at is None


def test_cleanup_removes_superseded_upload_objects(
    client, fake_storage, pg_sync_engine: Engine, pg_session_factory, make_settings
) -> None:
    """被新一次初始化替代的旧会话：对象被清理且会话标记过期（幂等）。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    first = _init_upload(client, student, assignment["id"])
    first_key = _simulate_put(client, fake_storage, first)
    second = _init_upload(client, student, assignment["id"])
    second_key = _simulate_put(client, fake_storage, second)

    # 预签名 PUT 早已过期（并超过删除缓冲期），可以安全删除
    _backdate_put_expiry(pg_sync_engine, [first["upload_id"], second["upload_id"]])

    async def run() -> int:
        async with pg_session_factory() as session:
            return await grading_service.cleanup_expired_submission_uploads(
                session,
                storage=fake_storage,
                settings=_cleanup_settings(make_settings),
            )

    assert asyncio.run(run()) == 1
    assert first_key not in fake_storage.objects
    assert second_key in fake_storage.objects  # 当前有效会话不受影响

    # 幂等：重复执行不再处理任何会话
    assert asyncio.run(run()) == 0


def test_cleanup_skips_when_put_url_not_past_buffer(
    client, fake_storage, pg_sync_engine: Engine, pg_session_factory, make_settings
) -> None:
    """PUT 地址未过期（或未经过删除缓冲期）时，清理不能领取任何会话。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    first = _init_upload(client, student, assignment["id"])
    first_key = _simulate_put(client, fake_storage, first)
    second = _init_upload(client, student, assignment["id"])
    second_key = _simulate_put(client, fake_storage, second)

    # 已被替代，但 PUT 地址仍在缓冲期内：不删除、不标记过期
    async def run() -> int:
        async with pg_session_factory() as session:
            return await grading_service.cleanup_expired_submission_uploads(
                session,
                storage=fake_storage,
                settings=_cleanup_settings(make_settings),
            )

    assert asyncio.run(run()) == 0
    assert first_key in fake_storage.objects
    assert second_key in fake_storage.objects


def _backdate_put_expiry(engine: Engine, upload_ids: list[str]) -> None:
    """把指定上传会话的 PUT 到期时间拨回 2 小时前（越过删除缓冲期）。"""
    with engine.begin() as connection:
        for upload_id in upload_ids:
            connection.execute(
                text(
                    "UPDATE submission_upload_sessions"
                    " SET upload_url_expires_at = now() - interval '2 hours'"
                    " WHERE id = CAST(:id AS uuid)"
                ),
                {"id": upload_id},
            )


def test_cleanup_rechecks_object_after_delete(
    client, fake_storage, pg_sync_engine: Engine, pg_session_factory, make_settings
) -> None:
    """清理删除对象后必须复查：发现晚到 PUT 则本轮不标记过期，下一轮继续。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    first = _init_upload(client, student, assignment["id"])
    first_key = _simulate_put(client, fake_storage, first)
    _init_upload(client, student, assignment["id"])
    _backdate_put_expiry(pg_sync_engine, [first["upload_id"]])

    # 模拟晚到 PUT：删除后对象立即被重建
    fake_storage.recreate_after_delete = True

    async def run() -> int:
        async with pg_session_factory() as session:
            return await grading_service.cleanup_expired_submission_uploads(
                session,
                storage=fake_storage,
                settings=_cleanup_settings(make_settings),
            )

    assert asyncio.run(run()) == 0
    with pg_sync_engine.connect() as connection:
        expired = connection.execute(
            text(
                "SELECT expired_at FROM submission_upload_sessions"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": first["upload_id"]},
        ).scalar_one()
    assert expired is None
    assert first_key in fake_storage.objects

    # 不再重建对象：下一轮清理完成标记（幂等收敛）
    fake_storage.recreate_after_delete = False
    assert asyncio.run(run()) == 1
    with pg_sync_engine.connect() as connection:
        expired = connection.execute(
            text(
                "SELECT expired_at FROM submission_upload_sessions"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": first["upload_id"]},
        ).scalar_one()
    assert expired is not None


def test_complete_locks_rubric_version_before_submission(
    client, fake_storage, pg_sync_engine: Engine
) -> None:
    """锁顺序：完成请求必须先拿 RubricVersion，才能去拿 Submission。

    先锁住 Submission 行，完成请求只能阻塞在它前面；此时它**必然已经持有**
    RubricVersion 行锁（正确顺序），因此另一条连接以 500ms 锁超时去锁
    RubricVersion 必然失败。若实现把 Submission 锁放在 RubricVersion 之前，
    请求会阻塞在 Submission 上且未持有版本锁，这里的加锁就会成功——测试失败。
    """
    _course, _teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])
    _simulate_put(client, fake_storage, init)

    with pg_sync_engine.connect() as connection:
        version_id = str(
            connection.execute(
                text(
                    "SELECT current_rubric_version_id FROM assignments"
                    " WHERE id = CAST(:id AS uuid)"
                ),
                {"id": assignment["id"]},
            ).scalar_one()
        )
        submission_id = str(
            connection.execute(
                text("SELECT id FROM submissions WHERE object_key = :key"),
                {"key": _session_object_key(pg_sync_engine, init["upload_id"])},
            ).scalar_one()
        )

    submission_lock = RowLock(pg_sync_engine, "submissions", submission_id)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            lambda: client.post(
                COMPLETE_URL.format(
                    assignment_id=assignment["id"], upload_id=init["upload_id"]
                ),
                headers=_auth(student),
            )
        )
        _assert_blocked(future, message="完成提交应阻塞在 Submission 行锁上")

        # 用短锁超时探测版本行锁：500ms 内拿不到 → 请求已持有（正确顺序）。
        order_ok = _probe_lock_held(
            pg_sync_engine, "assignment_rubric_versions", version_id
        )
    finally:
        # 关键顺序：**先释放 Submission 行锁，再 join 请求线程**。
        # 请求一直阻塞在该行锁上；若先 shutdown(wait=True)（with 块退出的
        # 行为）后释放锁，主线程等请求、请求等主线程持有的锁，永久死锁。
        submission_lock.release()
        pool.shutdown(wait=True)

    assert future.result(timeout=15).status_code == 201
    assert order_ok, (
        "完成请求在等待 Submission 锁时尚未持有 RubricVersion 锁："
        "锁顺序与契约 9.1 不一致"
    )


def _probe_lock_held(engine: Engine, table: str, row_id: str) -> bool:
    """以 500ms 锁超时尝试 ``FOR UPDATE`` 某行。

    返回 ``True`` 表示加锁超时（该行已被别的连接持有）；``False`` 表示
    成功加锁（该行此刻无人持有）。探针连接无论结果都回滚并关闭。
    """
    probe = engine.connect()
    try:
        probe.execute(text("SET lock_timeout = '500ms'"))
        probe.execute(
            text(
                f"SELECT id FROM {table}"
                " WHERE id = CAST(:id AS uuid) FOR UPDATE"
            ),
            {"id": row_id},
        )
    except Exception:
        # 锁超时：目标行已被完成请求持有
        return True
    finally:
        probe.rollback()
        probe.close()
    return False


def _session_object_key(engine: Engine, upload_id: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text(
                    "SELECT object_key FROM submission_upload_sessions"
                    " WHERE id = CAST(:id AS uuid)"
                ),
                {"id": upload_id},
            ).scalar_one()
        )


def test_cleanup_never_removes_completed_object(
    client,
    fake_storage,
    pg_sync_engine: Engine,
    pg_session_factory,
    make_settings,
) -> None:
    """已完成的会话及其报告对象绝不被清理。"""
    _course, _teacher, student, assignment = _open_assignment(client)
    init = _init_upload(client, student, assignment["id"])
    object_key = _simulate_put(client, fake_storage, init)
    completed = client.post(
        COMPLETE_URL.format(assignment_id=assignment["id"], upload_id=init["upload_id"]),
        headers=_auth(student),
    )
    assert completed.status_code == 201, completed.text

    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE submission_upload_sessions"
                " SET confirm_deadline_at = now() - interval '1 hour'"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": init["upload_id"]},
        )

    async def run() -> int:
        async with pg_session_factory() as session:
            return await grading_service.cleanup_expired_submission_uploads(
                session,
                storage=fake_storage,
                settings=_cleanup_settings(make_settings),
            )

    assert asyncio.run(run()) == 0
    assert object_key in fake_storage.objects
    assert client.get(
        DETAIL_URL.format(submission_id=completed.json()["id"]), headers=_auth(student)
    ).status_code == 200


# --------------------------------------------------------------------------- #
# 复核修改与发布：串行化在 GradeReview 行锁上（契约 9.1 / 9.8 / 9.9）
# --------------------------------------------------------------------------- #
def test_review_patch_waits_for_review_row_lock(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine: Engine
) -> None:
    """教师复核 PATCH 必须先取得 GradeReview 行锁才能写入。"""
    _assignment, teacher, _student, _submission_id, review = _graded_review(
        client, fake_storage, pg_session_factory, make_settings
    )
    payload = {
        "summary": "整体反馈",
        "items": [
            {"rubric_item_id": item["rubric_item_id"], "final_score": item["max_score"]}
            for item in review["items"]
        ],
    }

    lock = RowLock(pg_sync_engine, "grade_reviews", review["id"])
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            lambda: client.patch(
                REVIEW_PATCH_URL.format(review_id=review["id"]),
                json=payload,
                headers=_auth(teacher),
            )
        )
        _assert_blocked(future, message="复核修改应阻塞在 GradeReview 行锁上")
    finally:
        lock.release()
        pool.shutdown(wait=True)

    patched = future.result(timeout=15)
    assert patched.status_code == 200, patched.text
    assert patched.json()["reviewed_at"] is not None


def test_publish_waits_for_review_row_lock(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine: Engine
) -> None:
    """发布也必须串行化在 GradeReview 行锁上（与 PATCH 相同锁序）。"""
    _assignment, teacher, _student, _submission_id, review = _graded_review(
        client, fake_storage, pg_session_factory, make_settings
    )

    lock = RowLock(pg_sync_engine, "grade_reviews", review["id"])
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            lambda: client.post(
                REVIEW_PUBLISH_URL.format(review_id=review["id"]),
                headers=_auth(teacher),
            )
        )
        _assert_blocked(future, message="发布应阻塞在 GradeReview 行锁上")
    finally:
        lock.release()
        pool.shutdown(wait=True)

    # 锁释放后发布才能执行；此时尚未教师复核 → 按契约 9.9 返回 409
    published = future.result(timeout=15)
    assert published.status_code == 409, published.text
    assert published.json()["error"]["code"] == "GRADE_NOT_REVIEWED"


def test_review_patch_rechecks_published_after_lock(
    client, fake_storage, pg_session_factory, make_settings, pg_sync_engine: Engine
) -> None:
    """复核 PATCH 必须在**拿到批改行锁之后**复核"是否已发布"（契约 9.8）。

    持锁期间成绩被并发发布，PATCH 在拿到锁后必须返回
    ``409 GRADE_ALREADY_PUBLISHED``；若实现"先读状态后加锁"，会把已发布的
    复核结果改写并返回 ``200``。
    """
    _assignment, teacher, _student, _submission_id, review = _graded_review(
        client, fake_storage, pg_session_factory, make_settings
    )
    payload = {
        "summary": "整体反馈",
        "items": [
            {"rubric_item_id": item["rubric_item_id"], "final_score": item["max_score"]}
            for item in review["items"]
        ],
    }

    holder = pg_sync_engine.connect()
    holder_tx = holder.begin()
    holder.execute(
        text("SELECT id FROM grade_reviews WHERE id = CAST(:id AS uuid) FOR UPDATE"),
        {"id": review["id"]},
    )
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            lambda: client.patch(
                REVIEW_PATCH_URL.format(review_id=review["id"]),
                json=payload,
                headers=_auth(teacher),
            )
        )
        _assert_blocked(future, message="复核修改必须先取得 GradeReview 行锁")

        # 持锁期间并发发布（同事务天然拥有行锁），提交即释放锁并可见
        holder.execute(
            text(
                "UPDATE grade_reviews SET published_at = now()"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": review["id"]},
        )
        holder_tx.commit()
    finally:
        pool.shutdown(wait=True)
        holder.close()

    response = future.result(timeout=15)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "GRADE_ALREADY_PUBLISHED"


# --------------------------------------------------------------------------- #
# 辅助：异步客户端（真并发请求）
# --------------------------------------------------------------------------- #
def _async_client(app) -> httpx.AsyncClient:
    """基于 ASGITransport 的异步客户端，用于 ``asyncio.gather`` 真并发。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        timeout=30.0,
    )
