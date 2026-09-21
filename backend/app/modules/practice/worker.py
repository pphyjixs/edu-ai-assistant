"""练习生成 Worker（``docs/api-contract.md`` 7.10）。

独立进程：以 ``FOR UPDATE SKIP LOCKED`` 领取 ``PENDING`` 的
``PRACTICE_GENERATE`` 任务，生成运行令牌、递增 ``attempts``、设置租约并
推进到 ``RUNNING``；按租约 1/3 心跳续租。

关键约定（与资料解析 Worker 同一套机制）：

- **模型调用期间不持有数据库事务**：读取片段上下文后立即结束只读事务，
  模型调用在线程池中执行；发布时才开启短事务并按固定顺序加锁
  （课程 → 练习 → 任务 → 按 ID 排序的来源资料）。
- **旧执行者无法回写**：回写前复查运行令牌与任务状态，不匹配即放弃。
- **失败不留部分结果**：校验失败、资料失效或模型失败时清空题目并写
  ``FAILED``；课程在生成期间归档则写 ``CANCELLED``。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.time import utc_now
from app.modules.courses import repository as courses_repo
from app.modules.courses.models import Course, CourseStatus
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobStatusValue, JobType
from app.modules.materials import repository as materials_repo
from app.modules.practice import generation_ai, repository as repo
from app.modules.practice.models import (
    PracticeDifficulty,
    PracticeGenerationStatus,
    PracticeQuestionType,
    PracticeSet,
    PracticeStatus,
)

logger = logging.getLogger("app.practice.worker")

#: 单批默认领取的任务数
DEFAULT_BATCH_SIZE = 5

#: 生成 AI 客户端的工厂（测试注入假模型服务）
AiClientFactory = Callable[[], object]

_ERROR_MAX_LENGTH = 500


@dataclass(slots=True)
class ClaimedPracticeJob:
    """已领取的练习生成任务（含生成参数与运行令牌）。"""

    job_id: uuid.UUID
    practice_set: PracticeSet
    run_token: str


def _safe_error(exc: BaseException) -> str:
    """安全失败摘要：不包含提示词、课件原文、密钥与模型地址。"""
    if isinstance(exc, (generation_ai.PracticeGenerationError,
                        generation_ai.PracticeModelNotConfiguredError)):
        return str(exc)[:_ERROR_MAX_LENGTH]
    return f"练习生成失败（{type(exc).__name__}）"[:_ERROR_MAX_LENGTH]


async def claim_next(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    lease_seconds: int,
) -> ClaimedPracticeJob | None:
    """原子领取一个 ``PENDING`` 的练习生成任务。"""
    async with session_factory() as session:
        result = await session.execute(
            select(Job)
            .where(
                Job.type == JobType.PRACTICE_GENERATE,
                Job.status == JobStatusValue.PENDING,
            )
            .order_by(Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = result.scalar_one_or_none()
        if job is None:
            await session.rollback()
            return None

        practice_set = await repo.get_set_for_update(session, job.resource_id)
        if practice_set is None or practice_set.status is not PracticeStatus.GENERATING:
            # 练习已不存在或不再等待生成：任务作废，避免空转
            job.status = JobStatusValue.CANCELLED
            job.finished_at = now
            await session.commit()
            return None

        run_token = secrets.token_hex(16)
        job.status = JobStatusValue.RUNNING
        job.started_at = now
        job.progress = 0
        job.attempts += 1
        job.run_token = run_token
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        practice_set.updated_at = now
        await session.commit()

        return ClaimedPracticeJob(
            job_id=job.id, practice_set=practice_set, run_token=run_token
        )


async def renew_lease(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: uuid.UUID,
    run_token: str,
    now: datetime,
    lease_seconds: int,
) -> bool:
    """续租：令牌不匹配或任务已非 ``RUNNING`` 时返回 ``False``（执行方应中止）。"""
    from sqlalchemy import update

    async with session_factory() as session:
        result = await session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.run_token == run_token,
                Job.status == JobStatusValue.RUNNING,
            )
            .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
        )
        await session.commit()
        return result.rowcount > 0


def select_context(
    rows: list,
    *,
    material_order: list[uuid.UUID],
    max_chars: int,
) -> list[generation_ai.ContextChunk]:
    """按资料顺序轮流选取片段，保证每份资料至少一个片段且总量不超上限。"""
    grouped: dict[uuid.UUID, list] = {}
    for row in rows:
        grouped.setdefault(row.material_id, []).append(row)
    for chunks in grouped.values():
        chunks.sort(key=lambda item: item.order)

    selected: list[generation_ai.ContextChunk] = []
    total = 0
    index = 0
    while True:
        added = False
        for material_id in material_order:
            chunks = grouped.get(material_id, [])
            if index >= len(chunks):
                continue
            row = chunks[index]
            content = row.content
            if selected and total + len(content) > max_chars:
                return selected
            selected.append(
                generation_ai.ContextChunk(
                    chunk_id=row.chunk_id,
                    material_id=row.material_id,
                    material_name=row.filename,
                    content=content,
                    location_start=row.location_start,
                    location_end=row.location_end,
                )
            )
            total += len(content)
            added = True
        if not added:
            break
        index += 1
    return selected


def _record_attempt(
    session: AsyncSession,
    *,
    practice_set: PracticeSet,
    status: PracticeGenerationStatus,
    settings: Settings,
    question_count: int | None,
    duration_ms: int,
    error: str | None,
    now: datetime,
) -> None:
    """写一条生成尝试记录（成功与失败都留痕，内容安全）。"""
    repo.add_generation_attempt(
        session,
        attempt_id=uuid.uuid4(),
        practice_set_id=practice_set.id,
        teacher_id=practice_set.teacher_id,
        status=status,
        model=settings.ai_model.strip() or None,
        prompt_version=generation_ai.PROMPT_VERSION,
        requested_count=practice_set.requested_question_count,
        question_count=question_count,
        duration_ms=duration_ms,
        error=error[:_ERROR_MAX_LENGTH] if error else None,
        now=now,
    )


async def _write_success(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    practice_set_id: uuid.UUID,
    run_token: str,
    validated: generation_ai.ValidatedPractice,
    settings: Settings,
    duration_ms: int,
    now: datetime,
) -> bool:
    """成功回写：按固定顺序加锁复查后**一次性**写入全部题目。

    返回 ``False`` 表示复查未通过（旧执行者、练习已非生成中、课程已归档、
    资料失效），此时不落库任何题目。
    """
    async with session_factory() as session:
        practice_set = await repo.get_set_by_id(session, practice_set_id)
        if practice_set is None:
            return False
        course_id = practice_set.course_id

        # 固定顺序：课程 → 练习 → 任务 → 来源资料（按 ID 升序）
        course = await courses_repo.get_course_for_update(session, course_id)
        locked_set = await repo.get_set_for_update(session, practice_set_id)
        job = await jobs_service.lock_practice_generate_job(
            session, practice_set_id=practice_set_id
        )
        if (
            locked_set is None
            or job is None
            or job.run_token != run_token
            or job.status is not JobStatusValue.RUNNING
        ):
            await session.rollback()
            logger.info("运行令牌不匹配或任务已非 RUNNING，放弃发布（set=%s）", practice_set_id)
            return False

        if course is None or course.status is not CourseStatus.ACTIVE:
            locked_set.status = PracticeStatus.CANCELLED
            locked_set.updated_at = now
            job.status = JobStatusValue.CANCELLED
            job.finished_at = now
            _record_attempt(
                session,
                practice_set=locked_set,
                status=PracticeGenerationStatus.FAILED,
                settings=settings,
                question_count=None,
                duration_ms=duration_ms,
                error="课程在生成期间被归档",
                now=now,
            )
            await session.commit()
            logger.info("课程已归档，练习生成取消（set=%s）", practice_set_id)
            return False

        material_ids = await repo.list_set_material_ids(
            session, practice_set_id=practice_set_id
        )
        live = await materials_repo.lock_live_materials(
            session, material_ids=material_ids
        )
        if set(material_ids) - live:
            locked_set.status = PracticeStatus.FAILED
            locked_set.updated_at = now
            job.status = JobStatusValue.FAILED
            job.error = "所选资料在生成期间被删除或不再可用"
            job.finished_at = now
            _record_attempt(
                session,
                practice_set=locked_set,
                status=PracticeGenerationStatus.FAILED,
                settings=settings,
                question_count=None,
                duration_ms=duration_ms,
                error=job.error,
                now=now,
            )
            await session.commit()
            logger.info("来源资料已失效，练习生成失败（set=%s）", practice_set_id)
            return False

        await repo.delete_questions(session, practice_set_id=practice_set_id)
        for question in validated.questions:
            repo.add_question(
                session,
                question_id=uuid.uuid4(),
                practice_set_id=practice_set_id,
                order=question.order,
                question_type=question.type,
                prompt=question.prompt,
                options=question.options,
                correct_answer=question.correct_answer,
                explanation=question.explanation,
                knowledge_point=question.knowledge_point,
                grading_points=question.grading_points,
                source_material_id=question.source_material_id,
                source_material_name=question.source_material_name,
                source_location_start=question.source_location_start,
                source_location_end=question.source_location_end,
                source_quote=question.source_quote,
                now=now,
            )

        locked_set.title = validated.title
        locked_set.question_count = len(validated.questions)
        locked_set.status = PracticeStatus.DRAFT
        locked_set.updated_at = now
        job.status = JobStatusValue.SUCCEEDED
        job.progress = 100
        job.error = None
        job.finished_at = now
        _record_attempt(
            session,
            practice_set=locked_set,
            status=PracticeGenerationStatus.SUCCEEDED,
            settings=settings,
            question_count=len(validated.questions),
            duration_ms=duration_ms,
            error=None,
            now=now,
        )
        await session.commit()
        return True


async def _write_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    practice_set_id: uuid.UUID,
    run_token: str,
    message: str,
    settings: Settings,
    duration_ms: int,
    cancelled: bool,
    now: datetime,
) -> None:
    """失败回写：清空题目并置 ``FAILED``（或课程归档导致的 ``CANCELLED``）。"""
    async with session_factory() as session:
        locked_set = await repo.get_set_for_update(session, practice_set_id)
        job = await jobs_service.lock_practice_generate_job(
            session, practice_set_id=practice_set_id
        )
        if (
            locked_set is None
            or job is None
            or job.run_token != run_token
            or job.status is not JobStatusValue.RUNNING
        ):
            await session.rollback()
            return

        await repo.delete_questions(session, practice_set_id=practice_set_id)
        locked_set.status = (
            PracticeStatus.CANCELLED if cancelled else PracticeStatus.FAILED
        )
        locked_set.updated_at = now
        job.status = (
            JobStatusValue.CANCELLED if cancelled else JobStatusValue.FAILED
        )
        job.error = None if cancelled else message[:_ERROR_MAX_LENGTH]
        job.finished_at = now
        _record_attempt(
            session,
            practice_set=locked_set,
            status=PracticeGenerationStatus.FAILED,
            settings=settings,
            question_count=None,
            duration_ms=duration_ms,
            error=None if cancelled else message,
            now=now,
        )
        await session.commit()


async def run_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    claimed: ClaimedPracticeJob,
    settings: Settings,
    ai_client_factory: AiClientFactory | None = None,
) -> None:
    """执行一个已领取的练习生成任务（读取 → 模型 → 固定顺序发布）。"""
    practice_set_id = claimed.practice_set.id
    requested = claimed.practice_set.requested_question_count
    question_types = [
        PracticeQuestionType(value) for value in claimed.practice_set.question_types
    ]
    difficulty = claimed.practice_set.difficulty
    lease_seconds = settings.practice_generate_lease_seconds

    stop_event = asyncio.Event()
    aborted = {"flag": False}
    started = time.monotonic()

    async def fail(message: str, *, cancelled: bool = False) -> None:
        await _write_failure(
            session_factory,
            practice_set_id=practice_set_id,
            run_token=claimed.run_token,
            message=message,
            settings=settings,
            duration_ms=int((time.monotonic() - started) * 1000),
            cancelled=cancelled,
            now=utc_now(),
        )

    async def heartbeat_loop() -> None:
        interval = max(lease_seconds / 3, 0.05)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            ok = await renew_lease(
                session_factory,
                job_id=claimed.job_id,
                run_token=claimed.run_token,
                now=utc_now(),
                lease_seconds=lease_seconds,
            )
            if not ok:
                aborted["flag"] = True
                logger.info("续租失败，执行方中止（set=%s）", practice_set_id)
                return

    heartbeat = asyncio.create_task(heartbeat_loop())
    try:
        # ---- 1) 只读上下文（读取后立即结束事务）----
        async with session_factory() as session:
            material_ids = await repo.list_set_material_ids(
                session, practice_set_id=practice_set_id
            )
            rows = await repo.list_chunks_for_materials(
                session, material_ids=material_ids
            )
        if aborted["flag"]:
            return

        contexts = select_context(
            rows,
            material_order=material_ids,
            max_chars=settings.practice_generate_max_chars,
        )
        if not contexts:
            await fail("所选资料没有可用的检索片段")
            return

        # ---- 2) 事务外调用模型 ----
        ai_client: object | None = None
        try:
            if ai_client_factory is not None:
                ai_client = ai_client_factory()
            validated = await asyncio.to_thread(
                generation_ai.generate_practice,
                contexts,
                question_count=requested,
                question_types=question_types,
                difficulty=difficulty,
                base_url=settings.ai_base_url,
                api_key=settings.ai_api_key,
                model=settings.ai_model,
                timeout_seconds=settings.ai_timeout_seconds,
                client=ai_client,
            )
        finally:
            close = getattr(ai_client, "close", None)
            if close is not None:
                close()

        if aborted["flag"]:
            return

        # ---- 3) 固定顺序加锁后一次性发布 ----
        await _write_success(
            session_factory,
            practice_set_id=practice_set_id,
            run_token=claimed.run_token,
            validated=validated,
            settings=settings,
            duration_ms=int((time.monotonic() - started) * 1000),
            now=utc_now(),
        )
    except generation_ai.PracticeModelNotConfiguredError:
        logger.warning("练习生成未配置模型（set=%s）", practice_set_id)
        await fail("模型服务未配置")
    except generation_ai.PracticeGenerationError as exc:
        logger.warning("练习生成失败（set=%s）：%s", practice_set_id, exc)
        await fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - 单任务失败不拖垮整批
        logger.exception("练习生成异常（set=%s）", practice_set_id)
        await fail(_safe_error(exc))
    finally:
        stop_event.set()
        heartbeat.cancel()


async def run_pending_batch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    ai_client_factory: AiClientFactory | None = None,
    max_jobs: int = DEFAULT_BATCH_SIZE,
    stop_requested: Callable[[], bool] | None = None,
) -> int:
    """领取并执行一批练习生成任务，返回处理数量。"""
    processed = 0
    lease_seconds = settings.practice_generate_lease_seconds
    while processed < max_jobs:
        if stop_requested is not None and stop_requested():
            break
        claimed = await claim_next(
            session_factory, now=utc_now(), lease_seconds=lease_seconds
        )
        if claimed is None:
            break
        try:
            await run_job(
                session_factory,
                claimed=claimed,
                settings=settings,
                ai_client_factory=ai_client_factory,
            )
        except Exception:  # noqa: BLE001 - 兜底：尽力写失败，不中断批次
            logger.exception("练习生成任务处理失败（job=%s）", claimed.job_id)
        processed += 1
    return processed
