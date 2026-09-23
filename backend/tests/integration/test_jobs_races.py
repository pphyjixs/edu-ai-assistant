"""通用异步任务接口的并发与锁回归（契约 10.2，真实 PostgreSQL）。

实现要点与练习竞态一致：**不使用固定休眠**——用独立连接持有真实行锁，
断言"锁被持有时请求在观察窗口内不可能完成"；状态判定必须发生在任务行锁之后，
因此持锁期间改写状态后，重试必须按**最新**状态分流。

覆盖：

- 并发重试不会创建第二条 job，也不会发生两次有效重置；
- 持有 jobs 行锁时重试必须等待，释放后按最新状态重新分流；
- 重试清除旧运行令牌后，旧执行者的成功/失败回写都不能覆盖新执行。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from app.core.time import utc_now
from app.modules.grading import worker as grading_worker
from app.modules.practice import worker as practice_worker
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.integration.test_chat_api import _make_chat_client
from tests.integration.test_grading_api import (
    GRADE_URL,
    _grading_settings,
    _open_assignment,
    _submit_report,
)
from tests.integration.test_materials_api import (
    _auth,
)
from tests.integration.test_materials_api import (
    fake_storage as fake_storage,  # noqa: PLC0414 - pytest fixture registration
)
from tests.integration.test_practice_api import (
    RETRY_URL,
    _drive_practice_worker,
    _empty_question_model_factory,
    _generate,
    _ready_course,
    practice_model_factory,
)
from tests.integration.test_practice_races import (
    RowLock,
    _assert_blocked,
    _claimed_with_validated_questions,
    _expire_lease,
    _job_row,
    _question_count,
    _set_status,
)


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage) -> Iterator[TestClient]:
    with _make_chat_client(
        db_isolation, pg_app, fake_storage, practice_model_factory()
    ) as test_client:
        yield test_client


def _set_job_status(pg_sync_engine, job_id: str, status: str) -> None:
    with pg_sync_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET status = CAST(:status AS job_status)"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"status": status, "id": job_id},
        )


def _failed_practice_job(
    client: TestClient, fake_storage, pg_session_factory, make_settings, suffix: str
) -> tuple[str, str, str, str, str]:
    """建课程 + 生成练习 + 让 Worker 失败，返回 (教师, 课程, 练习, 资料, 任务)。"""
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"jobs-race-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    assert generated.status_code == 202, generated.text
    job_id = generated.json()["id"]
    set_id = generated.json()["resource_id"]
    assert _drive_practice_worker(
        pg_session_factory, make_settings, _empty_question_model_factory()
    ) == 1
    return teacher, course_id, set_id, material_id, job_id


def _failed_grade_job(
    client: TestClient, fake_storage, pg_session_factory, make_settings
) -> tuple[str, str, str]:
    """批改失败的任务，返回 ``(教师, 提交, 任务)``。"""
    _course, teacher, student, assignment = _open_assignment(client)
    detail = _submit_report(client, fake_storage, student, assignment["id"])
    triggered = client.post(
        GRADE_URL.format(submission_id=detail["id"]), headers=_auth(teacher)
    )
    assert triggered.status_code == 202, triggered.text
    job_id = triggered.json()["id"]

    fake_storage.as_unavailable()
    assert asyncio.run(
        grading_worker.run_pending_batch(
            pg_session_factory,
            settings=_grading_settings(make_settings),
            storage=fake_storage,
            ai_client_factory=None,
            max_jobs=1,
        )
    ) == 1
    fake_storage.as_available()
    return teacher, detail["id"], job_id


# --------------------------------------------------------------------------- #
# 并发重试：不创建第二条 job，只发生一次有效重置
# --------------------------------------------------------------------------- #
def test_concurrent_practice_retry_resets_once(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    db_isolation: None,
    pg_app,
) -> None:
    """两个并发重试：一个 202 完成重置，另一个按最新状态得到 409。"""
    suffix = uuid.uuid4().hex[:8]
    teacher, _course_id, set_id, _material_id, job_id = _failed_practice_job(
        client, fake_storage, pg_session_factory, make_settings, suffix
    )

    def retry() -> httpx.Response:
        with _make_chat_client(
            db_isolation, pg_app, fake_storage, practice_model_factory()
        ) as threaded:
            return threaded.post(
                RETRY_URL.format(job_id=job_id), headers=_auth(teacher)
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [future.result(timeout=30) for future in (pool.submit(retry), pool.submit(retry))]

    codes = sorted(response.status_code for response in responses)
    assert codes == [202, 409], [response.text for response in responses]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["error"]["code"] == "JOB_NOT_RETRYABLE"

    # 只存在一条任务，终态是"重置后等待执行"
    with pg_sync_engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM jobs"
                " WHERE type = 'PRACTICE_GENERATE'"
                " AND resource_id = CAST(:id AS uuid)"
            ),
            {"id": set_id},
        ).scalar_one()
    assert count == 1
    assert _job_row(pg_sync_engine, job_id)["status"] == "PENDING"
    assert _set_status(pg_sync_engine, set_id) == "GENERATING"
    assert _question_count(pg_sync_engine, set_id) == 0


def test_concurrent_submission_grade_retry_targets_same_job(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    db_isolation: None,
    pg_app,
) -> None:
    """批改任务的并发重试：都指向同一个 job，只留一条任务行。"""
    teacher, submission_id, job_id = _failed_grade_job(
        client, fake_storage, pg_session_factory, make_settings
    )

    def retry() -> httpx.Response:
        with _make_chat_client(
            db_isolation, pg_app, fake_storage, practice_model_factory()
        ) as threaded:
            return threaded.post(
                RETRY_URL.format(job_id=job_id), headers=_auth(teacher)
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [future.result(timeout=30) for future in (pool.submit(retry), pool.submit(retry))]

    assert all(response.status_code == 202 for response in responses), [
        response.text for response in responses
    ]
    assert {response.json()["id"] for response in responses} == {job_id}

    with pg_sync_engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM jobs"
                " WHERE type = 'SUBMISSION_GRADE'"
                " AND resource_id = CAST(:id AS uuid)"
            ),
            {"id": submission_id},
        ).scalar_one()
        submission_status = connection.execute(
            text("SELECT status FROM submissions WHERE id = CAST(:id AS uuid)"),
            {"id": submission_id},
        ).scalar_one()
    assert count == 1
    assert _job_row(pg_sync_engine, job_id)["status"] == "PENDING"
    assert submission_status == "GRADING"


# --------------------------------------------------------------------------- #
# 锁语义：重试必须等 job 行锁，且状态判定在锁之后
# --------------------------------------------------------------------------- #
def test_practice_retry_waits_for_job_row_lock(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    db_isolation: None,
    pg_app,
) -> None:
    """持有 jobs 行锁时，重试必须等待（锁在 课程 → 练习 → 任务 链的末端）。"""
    suffix = uuid.uuid4().hex[:8]
    teacher, _course_id, set_id, _material_id, job_id = _failed_practice_job(
        client, fake_storage, pg_session_factory, make_settings, suffix
    )

    lock = RowLock(pg_sync_engine, "jobs", job_id)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            lambda: client.post(
                RETRY_URL.format(job_id=job_id), headers=_auth(teacher)
            )
        )
        _assert_blocked(future, seconds=1.0)
    finally:
        lock.close()
        pool.shutdown(wait=True)

    response = future.result(timeout=30)
    assert response.status_code == 202, response.text
    assert _job_row(pg_sync_engine, job_id)["status"] == "PENDING"
    assert _set_status(pg_sync_engine, set_id) == "GENERATING"


def test_practice_retry_decides_state_after_job_lock(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """持锁期间任务被推进为 SUCCEEDED：重试必须按最新状态返回 409，而不是误重置。"""
    suffix = uuid.uuid4().hex[:8]
    teacher, _course_id, set_id, _material_id, job_id = _failed_practice_job(
        client, fake_storage, pg_session_factory, make_settings, suffix
    )

    lock = RowLock(pg_sync_engine, "jobs", job_id)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            lambda: client.post(
                RETRY_URL.format(job_id=job_id), headers=_auth(teacher)
            )
        )
        _assert_blocked(future, seconds=1.0)
        # 持锁期间推进状态，提交即释放锁并使新状态可见
        lock.run("UPDATE jobs SET status = 'SUCCEEDED'::job_status WHERE id = CAST(:id AS uuid)")
        lock.commit()
    finally:
        lock.close()
        pool.shutdown(wait=True)

    response = future.result(timeout=30)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "JOB_NOT_RETRYABLE"
    # 任务与练习状态都不得被误重置
    assert _job_row(pg_sync_engine, job_id)["status"] == "SUCCEEDED"
    assert _set_status(pg_sync_engine, set_id) == "FAILED"


def test_practice_retry_decides_state_after_job_lock_for_submission(
    client: TestClient, fake_storage, pg_session_factory, make_settings, pg_sync_engine
) -> None:
    """同样的锁后判定适用于批改任务：持锁期间置为 SUCCEEDED → 409 SUBMISSION_NOT_READY。"""
    teacher, _submission_id, job_id = _failed_grade_job(
        client, fake_storage, pg_session_factory, make_settings
    )

    lock = RowLock(pg_sync_engine, "jobs", job_id)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            lambda: client.post(
                RETRY_URL.format(job_id=job_id), headers=_auth(teacher)
            )
        )
        _assert_blocked(future, seconds=1.0)
        lock.run("UPDATE jobs SET status = 'SUCCEEDED'::job_status WHERE id = CAST(:id AS uuid)")
        lock.commit()
    finally:
        lock.close()
        pool.shutdown(wait=True)

    response = future.result(timeout=30)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "SUBMISSION_NOT_READY"
    assert _job_row(pg_sync_engine, job_id)["status"] == "SUCCEEDED"


# --------------------------------------------------------------------------- #
# 旧执行者的回写不得覆盖重试后的新执行
# --------------------------------------------------------------------------- #
def test_retry_revokes_stale_worker_writeback(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    db_isolation: None,
    pg_app,
) -> None:
    """重试清除旧运行令牌后，旧执行者的成功与失败回写都不能生效。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"jobs-stale-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    assert generated.status_code == 202, generated.text
    job_id = generated.json()["id"]
    set_id = generated.json()["resource_id"]

    # 旧执行者领取任务（持有旧 run_token）并拿到一份合法题目
    settings, claimed, validated = _claimed_with_validated_questions(
        teacher, material_id, pg_session_factory, make_settings
    )
    _expire_lease(pg_sync_engine, job_id)

    # 重试：复用原 job、清除旧令牌并回到 PENDING
    retried = client.post(RETRY_URL.format(job_id=job_id), headers=_auth(teacher))
    assert retried.status_code == 202, retried.text
    assert _job_row(pg_sync_engine, job_id)["run_token"] is None

    # 旧执行者的成功回写必须被拒绝，且不留下任何题目
    written = asyncio.run(
        practice_worker._write_success(
            pg_session_factory,
            claimed=claimed,
            validated=validated,
            settings=settings,
            duration_ms=3,
            now=utc_now(),
        )
    )
    assert written is False
    assert _question_count(pg_sync_engine, set_id) == 0
    assert _job_row(pg_sync_engine, job_id)["status"] == "PENDING"


def test_retry_revokes_stale_failure_writeback(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """失败回写同样受旧令牌约束：重试后的任务不得被旧执行者改成 FAILED。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"jobs-stale-fail-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    assert generated.status_code == 202, generated.text
    job_id = generated.json()["id"]
    set_id = generated.json()["resource_id"]

    settings, claimed, _validated = _claimed_with_validated_questions(
        teacher, material_id, pg_session_factory, make_settings
    )
    _expire_lease(pg_sync_engine, job_id)

    retried = client.post(RETRY_URL.format(job_id=job_id), headers=_auth(teacher))
    assert retried.status_code == 202, retried.text

    asyncio.run(
        practice_worker._write_failure(
            pg_session_factory,
            claimed=claimed,
            message="旧执行者的失败",
            settings=settings,
            duration_ms=3,
            cancelled=False,
            now=utc_now(),
        )
    )

    assert _job_row(pg_sync_engine, job_id)["status"] == "PENDING"
    assert _set_status(pg_sync_engine, set_id) == "GENERATING"
