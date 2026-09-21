"""练习链路的确定性并发与竞态回归（真实 PostgreSQL）。

覆盖 ``docs/api-contract.md`` 7.11 / 10.1 与统一锁协议
（**课程 → 练习 → 任务 → 按 ID 升序的资料**）：

- 生成 / 发布 / 提交 / 重试 四个写接口 × 两个方向：
  操作先持课程锁（归档等待，操作提交后归档完成）、
  归档先持课程锁（操作等待，归档提交后操作 409 且**无副作用**）；
- Worker 最终回写与过期任务重试并发：不出现 deadlock，只形成一个串行结果，
  旧运行令牌在重试后不能写入题目或覆盖任务状态；
- 模型开始后归档且模型失败 → 最终 ``CANCELLED``；
- 模型开始后来源资料被删除 → 最终 ``FAILED`` 且不留题目；
- 并发提交：恰好一次 ``201``，其余 ``409 PRACTICE_ALREADY_ATTEMPTED``。

实现要点：**不使用固定休眠**——用跨线程事件门控决定时序，用"锁被持有时
请求在给定时限内不可能完成"的断言证明行锁确实生效。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.time import utc_now
from app.modules.jobs import service as jobs_service
from app.modules.practice import generation_ai
from app.modules.practice import repository as practice_repo
from app.modules.practice import worker as practice_worker
from app.modules.practice.models import PracticeDifficulty, PracticeQuestionType
from tests.integration.test_chat_api import _make_chat_client
from tests.integration.test_practice_api import (
    _ALLOCATION,
    ATTEMPTS_URL,
    GENERATE_URL,
    PUBLISH_URL,
    RETRY_URL,
    SET_URL,
    _answers_from_teacher_view,
    _drive_practice_worker,
    _empty_question_model_factory,
    _generate,
    _join,
    _model_json_response,
    _parse_context,
    _question_payload,
    _ready_course,
    practice_model_factory,
)
from tests.integration.test_materials_api import (
    _auth,
    _fake_model_client,
    _login,
    _register,
)
from tests.integration.test_materials_api import fake_storage as fake_storage  # noqa: F401

ARCHIVE_URL = "/api/v1/courses/{course_id}/archive"

#: 断言"请求被行锁挡住"的观察窗口：真被挡住时不可能提前完成
BLOCK_WINDOW_SECONDS = 1.0


# --------------------------------------------------------------------------- #
# 门控与锁工具
# --------------------------------------------------------------------------- #
class Gate:
    """跨线程事件门控：请求侧在持锁状态等待，测试侧负责放行。"""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    async def hold(self) -> None:
        """在请求协程中调用：通知已到达持锁点，然后等待放行。"""
        self.entered.set()
        await asyncio.to_thread(self.release.wait, 30)

    def wait_entered(self, timeout: float = 10.0) -> None:
        assert self.entered.wait(timeout), "请求未到达持锁点（门控超时）"

    def let_go(self) -> None:
        self.release.set()


class RowLock:
    """在独立连接上锁住一行（模拟并发方先拿走该行的排他锁）。

    ``table`` 只由测试常量传入，不做外部输入拼接。
    """

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


class CourseLock:
    """在独立连接上持有课程行锁（模拟"归档先拿走课程锁"）。"""

    def __init__(self, engine, course_id: str) -> None:
        self._course_id = course_id
        self._connection = engine.connect()
        self._transaction = self._connection.begin()
        self._connection.execute(
            text("SELECT id FROM courses WHERE id = CAST(:id AS uuid) FOR UPDATE"),
            {"id": course_id},
        )

    def archive_and_commit(self) -> None:
        """在持锁期间把课程置为 ARCHIVED，然后提交释放行锁。"""
        self._connection.execute(
            text(
                "UPDATE courses SET status = 'ARCHIVED'::course_status,"
                " updated_at = now() WHERE id = CAST(:id AS uuid)"
            ),
            {"id": self._course_id},
        )
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


@dataclass(slots=True)
class World:
    """一次竞态用例的世界状态（标识符集合）。"""

    course_id: str
    teacher: str
    material_id: str
    set_id: str | None = None
    job_id: str | None = None
    student: str | None = None


@dataclass(frozen=True, slots=True)
class OperationCase:
    """一个写接口的竞态用例参数。"""

    name: str
    #: 需要挂门控的模块属性（在课程行锁之后被调用）
    hook_target: object
    hook_attribute: str
    #: 成功时的期望状态码
    success_status: int
    #: 归档先持锁时，"无副作用"的断言 SQL（返回计数）
    side_effect_sql: str


def _side_effects(pg_sync_engine, sql: str, world: World) -> int:
    with pg_sync_engine.connect() as connection:
        return int(
            connection.execute(
                text(sql),
                {
                    "course_id": world.course_id,
                    "set_id": world.set_id,
                    "job_id": world.job_id,
                },
            ).scalar_one()
        )


def _course_status(engine, course_id: str) -> str:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT status FROM courses WHERE id = CAST(:id AS uuid)"),
            {"id": course_id},
        ).scalar_one()


def _question_count(engine, set_id: str | None) -> int:
    if set_id is None:
        return 0
    with engine.connect() as connection:
        return int(
            connection.execute(
                text(
                    "SELECT count(*) FROM practice_questions"
                    " WHERE practice_set_id = CAST(:id AS uuid)"
                ),
                {"id": set_id},
            ).scalar_one()
        )


def _set_status(engine, set_id: str) -> str:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT status FROM practice_sets WHERE id = CAST(:id AS uuid)"),
            {"id": set_id},
        ).scalar_one()


def _job_row(engine, job_id: str) -> dict:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT status, attempts, run_token, lease_expires_at"
                    " FROM jobs WHERE id = CAST(:id AS uuid)"
                ),
                {"id": job_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def _expire_lease(engine, job_id: str) -> None:
    """把租约推到过去（模拟崩溃遗留的 RUNNING 任务）。"""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE jobs SET lease_expires_at = now() - interval '1 hour'"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": job_id},
        )


# --------------------------------------------------------------------------- #
# 世界准备
# --------------------------------------------------------------------------- #
def _build_world(
    name: str,
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    suffix: str,
) -> World:
    """按接口准备到"刚好可以发起该操作"的状态。"""
    course_id, teacher, material_id = _ready_course(
        client,
        fake_storage,
        pg_session_factory,
        make_settings,
        email=f"race-{name}-{suffix}@example.com",
    )
    world = World(course_id=course_id, teacher=teacher, material_id=material_id)
    if name == "generate":
        return world

    generated = _generate(client, teacher, course_id, [material_id])
    assert generated.status_code == 202, generated.text
    world.set_id = generated.json()["resource_id"]
    world.job_id = generated.json()["id"]
    if name == "publish":
        # 需要 DRAFT：先让 Worker 出题成功
        assert _drive_practice_worker(
            pg_session_factory, make_settings, practice_model_factory()
        ) == 1
        return world

    if name == "retry":
        # 需要 FAILED：让模型返回空题目
        assert _drive_practice_worker(
            pg_session_factory, make_settings, _empty_question_model_factory()
        ) == 1
        return world

    # submit：出题成功后发布，并需要一个尚未提交的学生
    assert _drive_practice_worker(
        pg_session_factory, make_settings, practice_model_factory()
    ) == 1
    published = client.post(
        PUBLISH_URL.format(set_id=world.set_id), headers=_auth(teacher)
    )
    assert published.status_code == 200, published.text
    student_email = f"race-submit-student-{suffix}@example.com"
    _register(client, student_email, "STUDENT")
    student = _login(client, student_email)
    _join(client, course_id, teacher, student)
    world.student = student
    return world


def _request_kwargs(
    name: str, client: TestClient, world: World
) -> tuple[str, dict]:
    """构造写接口的请求（URL 与关键字参数）。"""
    if name == "generate":
        return GENERATE_URL.format(course_id=world.course_id), {
            "json": {
                "material_ids": [world.material_id],
                "question_count": 3,
                "question_types": ["SINGLE_CHOICE", "TRUE_FALSE", "SHORT_ANSWER"],
                "difficulty": "MEDIUM",
            },
            "headers": _auth(world.teacher),
        }
    if name == "publish":
        return PUBLISH_URL.format(set_id=world.set_id), {
            "headers": _auth(world.teacher)
        }
    if name == "retry":
        return RETRY_URL.format(job_id=world.job_id), {
            "headers": _auth(world.teacher)
        }
    # submit
    teacher_view = client.get(
        SET_URL.format(set_id=world.set_id), headers=_auth(world.teacher)
    ).json()
    return ATTEMPTS_URL.format(set_id=world.set_id), {
        "json": {"answers": _answers_from_teacher_view(teacher_view["questions"])},
        "headers": _auth(world.student),
    }


@pytest.fixture
def client(db_isolation: None, pg_app, fake_storage) -> Iterator[TestClient]:
    with _make_chat_client(
        db_isolation, pg_app, fake_storage, practice_model_factory()
    ) as test_client:
        yield test_client


OPERATION_CASES = (
    OperationCase(
        name="generate",
        hook_target=practice_repo,
        hook_attribute="list_generation_materials",
        success_status=202,
        side_effect_sql=(
            "SELECT count(*) FROM practice_sets"
            " WHERE course_id = CAST(:course_id AS uuid)"
        ),
    ),
    OperationCase(
        name="publish",
        hook_target=practice_repo,
        hook_attribute="get_set_for_update",
        success_status=200,
        side_effect_sql=(
            "SELECT count(*) FROM practice_sets"
            " WHERE id = CAST(:set_id AS uuid) AND status = 'PUBLISHED'"
        ),
    ),
    OperationCase(
        name="submit",
        hook_target=practice_repo,
        hook_attribute="list_questions",
        success_status=201,
        side_effect_sql=(
            "SELECT count(*) FROM practice_attempts"
            " WHERE practice_set_id = CAST(:set_id AS uuid)"
        ),
    ),
    OperationCase(
        name="retry",
        hook_target=jobs_service,
        hook_attribute="lock_practice_generate_job",
        success_status=202,
        side_effect_sql=(
            "SELECT count(*) FROM jobs"
            " WHERE id = CAST(:job_id AS uuid) AND status = 'PENDING'"
        ),
    ),
)


def _install_gate(monkeypatch: pytest.MonkeyPatch, case: OperationCase, gate: Gate) -> None:
    """在"课程行锁之后"的调用点挂门控，使操作在持锁状态下暂停。"""
    real = getattr(case.hook_target, case.hook_attribute)

    async def hooked(*args, **kwargs):
        await gate.hold()
        return await real(*args, **kwargs)

    monkeypatch.setattr(case.hook_target, case.hook_attribute, hooked)


# --------------------------------------------------------------------------- #
# 方向一：操作先持有课程锁 → 归档等待，操作提交后归档完成
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", OPERATION_CASES, ids=lambda case: case.name)
def test_operation_holding_course_lock_blocks_archive(
    case: OperationCase,
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    db_isolation: None,
    pg_app,
    pg_sync_engine,
) -> None:
    suffix = uuid.uuid4().hex[:8]
    world = _build_world(
        case.name, client, fake_storage, pg_session_factory, make_settings, suffix
    )
    url, kwargs = _request_kwargs(case.name, client, world)
    archive_url = ARCHIVE_URL.format(course_id=world.course_id)

    gate = Gate()
    _install_gate(monkeypatch, case, gate)

    def call_operation() -> httpx.Response:
        with _make_chat_client(
            db_isolation, pg_app, fake_storage, practice_model_factory()
        ) as threaded:
            return threaded.post(url, **kwargs)

    def call_archive() -> httpx.Response:
        with _make_chat_client(
            db_isolation, pg_app, fake_storage, practice_model_factory()
        ) as threaded:
            return threaded.post(archive_url, headers=_auth(world.teacher))

    with ThreadPoolExecutor(max_workers=2) as pool:
        operation = pool.submit(call_operation)
        gate.wait_entered()  # 操作已持有课程锁
        archive = pool.submit(call_archive)
        # 归档被课程行锁挡住，不可能提前完成
        _assert_blocked(archive)
        assert _course_status(pg_sync_engine, world.course_id) == "ACTIVE"
        gate.let_go()
        assert operation.result(timeout=30).status_code == case.success_status
        assert archive.result(timeout=30).status_code == 200

    # 操作先提交、归档随后生效：两者都按事务顺序完成
    assert _course_status(pg_sync_engine, world.course_id) == "ARCHIVED"


# --------------------------------------------------------------------------- #
# 方向二：归档先持有课程锁 → 操作等待，归档提交后操作 409 且无副作用
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case", OPERATION_CASES, ids=lambda case: case.name)
def test_archive_holding_course_lock_rejects_operation(
    case: OperationCase,
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    db_isolation: None,
    pg_app,
) -> None:
    suffix = uuid.uuid4().hex[:8]
    world = _build_world(
        case.name, client, fake_storage, pg_session_factory, make_settings, suffix
    )
    url, kwargs = _request_kwargs(case.name, client, world)
    baseline = _side_effects(pg_sync_engine, case.side_effect_sql, world)
    baseline_questions = _question_count(pg_sync_engine, world.set_id)

    def call_operation() -> httpx.Response:
        with _make_chat_client(
            db_isolation, pg_app, fake_storage, practice_model_factory()
        ) as threaded:
            return threaded.post(url, **kwargs)

    lock = CourseLock(pg_sync_engine, world.course_id)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            operation = pool.submit(call_operation)
            # 操作被课程行锁挡住
            _assert_blocked(operation)
            # 归档在持锁状态下先提交
            lock.archive_and_commit()
            response = operation.result(timeout=30)
    finally:
        lock.close()

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "COURSE_ARCHIVED"
    # 数据库无副作用：练习、发布、答题、任务状态都与之前一致
    assert _side_effects(pg_sync_engine, case.side_effect_sql, world) == baseline
    assert _question_count(pg_sync_engine, world.set_id) == baseline_questions
    assert _course_status(pg_sync_engine, world.course_id) == "ARCHIVED"


# --------------------------------------------------------------------------- #
# Worker 回写 与 过期任务重试
# --------------------------------------------------------------------------- #
def _claimed_with_validated_questions(
    teacher: str,
    material_id: str,
    pg_session_factory,
    make_settings,
    *,
    question_count: int = 3,
) -> tuple[object, object, object]:
    """领取任务并准备一份通过校验的题目快照（模拟 Worker 已拿到模型结果）。

    返回 ``(settings, claimed, validated)``。
    """
    settings = make_settings(
        ai_base_url="http://fake-model.local/v1", ai_model="fake-model"
    )

    async def claim():
        return await practice_worker.claim_next(
            pg_session_factory,
            now=utc_now(),
            lease_seconds=settings.practice_generate_lease_seconds,
        )

    claimed = asyncio.run(claim())
    assert claimed is not None
    assert claimed.course_id is not None

    async def read_context():
        async with pg_session_factory() as session:
            material_ids = await practice_repo.list_set_material_ids(
                session, practice_set_id=claimed.practice_set_id
            )
            rows = await practice_repo.list_chunks_for_materials(
                session, material_ids=material_ids
            )
        return material_ids, rows

    material_ids, rows = asyncio.run(read_context())
    contexts = practice_worker.select_context(
        rows,
        material_order=material_ids,
        max_chars=settings.practice_generate_max_chars,
    )
    assert contexts
    validated = generation_ai.generate_practice(
        contexts,
        question_count=question_count,
        question_types=[
            PracticeQuestionType.SINGLE_CHOICE,
            PracticeQuestionType.TRUE_FALSE,
            PracticeQuestionType.SHORT_ANSWER,
        ],
        difficulty=PracticeDifficulty.MEDIUM,
        base_url=settings.ai_base_url,
        api_key=settings.ai_api_key,
        model=settings.ai_model,
        timeout_seconds=settings.ai_timeout_seconds,
        client=practice_model_factory()(),
    )
    return settings, claimed, validated


def test_writeback_and_expired_retry_do_not_deadlock(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    db_isolation: None,
    pg_app,
) -> None:
    """回写与重试并发：无 deadlock，只形成一个串行结果。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"race-writeback-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    assert generated.status_code == 202
    set_id = generated.json()["resource_id"]
    job_id = generated.json()["id"]

    settings, claimed, validated = _claimed_with_validated_questions(
        teacher, material_id, pg_session_factory, make_settings
    )
    _expire_lease(pg_sync_engine, job_id)

    def write_back() -> bool:
        return asyncio.run(
            practice_worker._write_success(
                pg_session_factory,
                claimed=claimed,
                validated=validated,
                settings=settings,
                duration_ms=3,
                now=utc_now(),
            )
        )

    def retry() -> httpx.Response:
        with _make_chat_client(
            db_isolation, pg_app, fake_storage, practice_model_factory()
        ) as threaded:
            return threaded.post(
                RETRY_URL.format(job_id=job_id), headers=_auth(teacher)
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        write_future = pool.submit(write_back)
        retry_future = pool.submit(retry)
        written = write_future.result(timeout=30)
        response = retry_future.result(timeout=30)

    # 双方都正常返回（死锁会让其中一方抛错或一直阻塞）
    assert written is False, "租约已过期的执行不得回写"
    assert response.status_code == 202, response.text
    # 唯一一致的终态：任务回到 PENDING、练习在生成中、没有任何题目
    assert _job_row(pg_sync_engine, job_id)["status"] == "PENDING"
    assert _set_status(pg_sync_engine, set_id) == "GENERATING"
    assert _question_count(pg_sync_engine, set_id) == 0


def test_stale_run_token_cannot_write_after_retry(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """重试之后，旧运行令牌既不能写题目也不能覆盖任务状态。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"race-stale-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    set_id = generated.json()["resource_id"]
    job_id = generated.json()["id"]

    settings, claimed, validated = _claimed_with_validated_questions(
        teacher, material_id, pg_session_factory, make_settings
    )
    _expire_lease(pg_sync_engine, job_id)
    retried = client.post(RETRY_URL.format(job_id=job_id), headers=_auth(teacher))
    assert retried.status_code == 202, retried.text
    after_retry = _job_row(pg_sync_engine, job_id)

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
    assert _job_row(pg_sync_engine, job_id) == after_retry, "旧执行者不得覆盖任务状态"
    assert _set_status(pg_sync_engine, set_id) == "GENERATING"


# --------------------------------------------------------------------------- #
# 锁序：领取任务不得反向锁练习行；回写必须等资料锁
# --------------------------------------------------------------------------- #
def test_claim_never_waits_for_the_practice_row(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """领取任务时练习行已被写路径锁住也必须能完成（只读、不加锁）。

    若领取重新变成"任务 → 练习"的加锁顺序，这里会一直阻塞而超时失败。
    """
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"race-claim-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    set_id = generated.json()["resource_id"]

    settings = make_settings(
        ai_base_url="http://fake-model.local/v1", ai_model="fake-model"
    )
    lock = RowLock(pg_sync_engine, "practice_sets", set_id)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        claim = pool.submit(
            lambda: asyncio.run(
                practice_worker.claim_next(
                    pg_session_factory,
                    now=utc_now(),
                    lease_seconds=settings.practice_generate_lease_seconds,
                )
            )
        )
        # 练习行被排他锁住时领取仍须完成；反向加锁会一直等待到超时
        try:
            claimed = claim.result(timeout=15)
        except FuturesTimeoutError as exc:
            raise AssertionError(
                "领取任务等待了练习行锁：出现了「任务 → 练习」的反向加锁顺序"
            ) from exc
    finally:
        # 先释放行锁，让可能被阻塞的线程结束，再等线程池收尾（避免测试挂死）
        lock.close()
        pool.shutdown(wait=True)

    assert claimed is not None
    assert str(claimed.practice_set_id) == set_id


def test_writeback_waits_for_material_lock_before_publishing(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
) -> None:
    """回写必须等资料行锁：资料在回写期间被删除时不得发布题目。

    资料行被排他锁住时，回写的共享锁会等待——若缺少共享锁，回写会先落库
    题目，随后删除提交，留下"引用已删除资料"的过期结果。
    """
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"race-material-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    set_id = generated.json()["resource_id"]
    job_id = generated.json()["id"]

    settings, claimed, validated = _claimed_with_validated_questions(
        teacher, material_id, pg_session_factory, make_settings
    )

    def write_back() -> bool:
        return asyncio.run(
            practice_worker._write_success(
                pg_session_factory,
                claimed=claimed,
                validated=validated,
                settings=settings,
                duration_ms=3,
                now=utc_now(),
            )
        )

    lock = RowLock(pg_sync_engine, "materials", material_id)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            write = pool.submit(write_back)
            # 资料锁未释放前回写必须停住（证明共享锁确实生效）
            _assert_blocked(write)
            assert _question_count(pg_sync_engine, set_id) == 0
            # 删除在同一把锁内完成，然后提交
            lock.run(
                "UPDATE materials SET deleted_at = now(),"
                " status = 'FAILED'::material_status WHERE id = CAST(:id AS uuid)"
            )
            lock.run("DELETE FROM material_chunks WHERE material_id = CAST(:id AS uuid)")
            lock.commit()
            assert write.result(timeout=30) is False
    finally:
        lock.close()

    assert _question_count(pg_sync_engine, set_id) == 0
    assert _set_status(pg_sync_engine, set_id) == "FAILED"
    assert _job_row(pg_sync_engine, job_id)["status"] == "FAILED"


# --------------------------------------------------------------------------- #
# 模型调用期间的归档与资料删除
# --------------------------------------------------------------------------- #
def _model_factory_with_side_effect(side_effect) -> object:
    """先执行副作用（归档课程 / 删除资料），再返回一份合法的模型响应。"""

    def responder(request: httpx.Request) -> httpx.Response:
        side_effect()
        prompt = json.loads(request.content)["messages"][1]["content"]
        chunk_ids, quote = _parse_context(prompt)
        if not chunk_ids:  # pragma: no cover - 上下文必然非空
            return _model_json_response({"title": "空", "questions": []})
        questions = []
        for question_type, count in _ALLOCATION.findall(prompt):
            for _ in range(int(count)):
                questions.append(
                    _question_payload(question_type, chunk_ids[0], quote)
                )
        return _model_json_response({"title": "自动生成练习", "questions": questions})

    return responder


def test_archive_during_model_call_then_model_failure_cancels(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    db_isolation: None,
    pg_app,
) -> None:
    """模型开始后课程被归档、随后模型失败：最终必须是 CANCELLED（而非 FAILED）。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"race-archive-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    set_id = generated.json()["resource_id"]
    job_id = generated.json()["id"]
    archive_url = ARCHIVE_URL.format(course_id=course_id)

    def archive_then_fail() -> None:
        with _make_chat_client(
            db_isolation, pg_app, fake_storage, practice_model_factory()
        ) as threaded:
            archived = threaded.post(archive_url, headers=_auth(teacher))
            assert archived.status_code == 200, archived.text

    def responder(request: httpx.Request) -> httpx.Response:
        archive_then_fail()
        raise httpx.ConnectError("模型调用失败", request=request)

    assert (
        _drive_practice_worker(
            pg_session_factory, make_settings, _fake_model_client(responder)
        )
        == 1
    )

    assert _set_status(pg_sync_engine, set_id) == "CANCELLED"
    assert _job_row(pg_sync_engine, job_id)["status"] == "CANCELLED"
    assert _question_count(pg_sync_engine, set_id) == 0


def test_material_deleted_during_model_call_fails_without_questions(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    db_isolation: None,
    pg_app,
) -> None:
    """模型开始后来源资料被删除：最终 FAILED，且不留任何题目。"""
    suffix = uuid.uuid4().hex[:8]
    course_id, teacher, material_id = _ready_course(
        client, fake_storage, pg_session_factory, make_settings,
        email=f"race-delete-{suffix}@example.com",
    )
    generated = _generate(client, teacher, course_id, [material_id])
    set_id = generated.json()["resource_id"]
    job_id = generated.json()["id"]

    def delete_material() -> None:
        with _make_chat_client(
            db_isolation, pg_app, fake_storage, practice_model_factory()
        ) as threaded:
            deleted = threaded.delete(
                f"/api/v1/materials/{material_id}", headers=_auth(teacher)
            )
            assert deleted.status_code == 204, deleted.text

    assert (
        _drive_practice_worker(
            pg_session_factory,
            make_settings,
            _fake_model_client(_model_factory_with_side_effect(delete_material)),
        )
        == 1
    )

    assert _set_status(pg_sync_engine, set_id) == "FAILED"
    assert _job_row(pg_sync_engine, job_id)["status"] == "FAILED"
    assert _question_count(pg_sync_engine, set_id) == 0


# --------------------------------------------------------------------------- #
# 并发提交
# --------------------------------------------------------------------------- #
def test_concurrent_submit_exactly_one_succeeds(
    client: TestClient,
    fake_storage,
    pg_session_factory,
    make_settings,
    pg_sync_engine,
    db_isolation: None,
    pg_app,
) -> None:
    """并发提交：恰好一次 201，其余 409 PRACTICE_ALREADY_ATTEMPTED。"""
    suffix = uuid.uuid4().hex[:8]
    world = _build_world(
        "submit", client, fake_storage, pg_session_factory, make_settings, suffix
    )
    url, kwargs = _request_kwargs("submit", client, world)

    def submit() -> httpx.Response:
        with _make_chat_client(
            db_isolation, pg_app, fake_storage, practice_model_factory()
        ) as threaded:
            return threaded.post(url, **kwargs)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = [
            future.result(timeout=30) for future in (pool.submit(submit), pool.submit(submit))
        ]

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 409], [response.text for response in responses]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["error"]["code"] == "PRACTICE_ALREADY_ATTEMPTED"

    # 只落一条答题记录与一份明细
    attempts = _side_effects(
        pg_sync_engine,
        "SELECT count(*) FROM practice_attempts"
        " WHERE practice_set_id = CAST(:set_id AS uuid)",
        world,
    )
    assert attempts == 1
    with pg_sync_engine.connect() as connection:
        answers = connection.execute(
            text("SELECT count(*) FROM practice_attempt_answers")
        ).scalar_one()
    assert int(answers) == 3
