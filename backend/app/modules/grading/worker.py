"""报告批改 Worker（``docs/api-contract.md`` 9.11）。

独立进程：以 ``FOR UPDATE SKIP LOCKED`` 领取 ``PENDING`` 的
``SUBMISSION_GRADE`` 任务，生成运行令牌、递增 ``attempts``、设置租约并推进到
``RUNNING``；按租约 1/3 心跳续租。

关键约定（与练习生成 Worker 同一套机制）：

- **模型调用期间不持有数据库事务**：读取提交与评分规则的**标量快照**后立即结束
  只读事务，对象下载、文本提取与模型调用都在事务外完成。
- **只按提交固定的评分版本批改**：快照来自 ``submission.rubric_version_id``，
  绝不读取 Assignment 的当前版本，因此教师后续修改 Rubric 不影响历史批改。
- **旧执行者无法回写**：回写前复查运行令牌与任务状态，租约过期即放弃。
- **失败不留部分结果**：校验失败、对象失效、文本提取失败或模型失败时
  **不写任何** ``GradeReview``/``GradeItem``，只写 ``FAILED``；课程在批改期间
  归档则写 ``CANCELLED``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.time import utc_now
from app.modules.assignments import repository as assignments_repo
from app.modules.assignments.models import AssignmentRubricItem
from app.modules.courses import repository as courses_repo
from app.modules.courses.models import CourseStatus
from app.modules.grading import grading_ai, repository as repo
from app.modules.grading.grading_ai import RubricItemSnapshot
from app.modules.grading.models import (
    EVIDENCE_SOURCE_DOCX_PARAGRAPH,
    EVIDENCE_SOURCE_PDF_PAGE,
    RAW_OUTPUT_MAX_LENGTH,
    SUBMISSION_ERROR_MAX_LENGTH,
    SubmissionGradeAttemptStatus,
    SubmissionStatus,
)
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobStatusValue, JobType
from app.modules.materials import extraction
from app.storage import (
    S3Storage,
    StorageObjectNotFoundError,
    StorageUnavailableError,
    StorageVerificationError,
)

logger = logging.getLogger("app.grading.worker")

#: 单批默认领取的任务数
DEFAULT_BATCH_SIZE = 5

#: 批改 AI 客户端的工厂（测试注入假模型服务）
AiClientFactory = Callable[[], object]


@dataclass(slots=True)
class ClaimedGradeJob:
    """已领取的批改任务（只携带**标量快照**，不把 ORM 对象带出事务）。"""

    job_id: uuid.UUID
    submission_id: uuid.UUID
    course_id: uuid.UUID
    run_token: str


@dataclass(slots=True)
class SubmissionSnapshot:
    """批改所需的提交标量快照。"""

    submission_id: uuid.UUID
    course_id: uuid.UUID
    assignment_id: uuid.UUID
    object_key: str
    content_type: str
    size: int
    sha256: str


def _safe_error(exc: BaseException) -> str:
    """安全失败摘要：不包含提示词、报告原文、密钥与模型地址。"""
    if isinstance(
        exc,
        (
            grading_ai.GradeGenerationError,
            grading_ai.GradeModelNotConfiguredError,
            extraction.ExtractError,
        ),
    ):
        return str(exc)[:SUBMISSION_ERROR_MAX_LENGTH]
    return f"报告批改失败（{type(exc).__name__}）"[:SUBMISSION_ERROR_MAX_LENGTH]


async def claim_next(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    lease_seconds: int,
) -> ClaimedGradeJob | None:
    """原子领取一个 ``PENDING`` 的批改任务。

    领取事务**只写任务行**：提交用普通读校验（不加行锁），避免形成
    "任务 → 提交" 的反向锁链，与写接口的
    "课程 → Assignment → RubricVersion → Submission → Job" 顺序冲突。
    """
    async with session_factory() as session:
        result = await session.execute(
            select(Job)
            .where(
                Job.type == JobType.SUBMISSION_GRADE,
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

        job_id = job.id
        submission_id = job.resource_id
        submission = await repo.get_submission_by_id(session, submission_id)
        if submission is None or submission.status is not SubmissionStatus.GRADING:
            # 提交已不存在或不再等待批改：任务作废，避免空转
            job.status = JobStatusValue.CANCELLED
            job.finished_at = now
            await session.commit()
            return None

        course_id = submission.course_id
        run_token = secrets.token_hex(16)
        job.status = JobStatusValue.RUNNING
        job.started_at = now
        job.progress = 0
        job.attempts += 1
        job.run_token = run_token
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        claimed = ClaimedGradeJob(
            job_id=job_id,
            submission_id=submission_id,
            course_id=course_id,
            run_token=run_token,
        )
        await session.commit()
        return claimed


async def renew_lease(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: uuid.UUID,
    run_token: str,
    now: datetime,
    lease_seconds: int,
) -> bool:
    """续租：令牌不匹配或任务已非 ``RUNNING`` 时返回 ``False``（执行方应中止）。"""
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


async def load_snapshot(
    session_factory: async_sessionmaker[AsyncSession], *, submission_id: uuid.UUID
) -> tuple[SubmissionSnapshot, list[RubricItemSnapshot]] | None:
    """读取提交与**固定**评分版本的标量快照（读取后立即结束事务）。

    提交引用的评分版本是历史版本：即使任务当前版本已升级，这里也只读提交固定的
    那一版，因此批改结果与提交时的评分规则一致。
    """
    async with session_factory() as session:
        submission = await repo.get_submission_by_id(session, submission_id)
        if submission is None or submission.rubric_version_id is None:
            return None
        items = await assignments_repo.list_rubric_items(
            session, rubric_version_id=submission.rubric_version_id
        )
        snapshot = SubmissionSnapshot(
            submission_id=submission.id,
            course_id=submission.course_id,
            assignment_id=submission.assignment_id,
            object_key=submission.object_key,
            content_type=submission.content_type,
            size=submission.size,
            sha256=submission.sha256,
        )
        rubric = [_rubric_snapshot(item) for item in items]
    return snapshot, rubric


def _rubric_snapshot(item: AssignmentRubricItem) -> RubricItemSnapshot:
    return RubricItemSnapshot(
        rubric_item_id=item.id,
        order=item.order,
        title=item.title,
        description=item.description,
        max_score=Decimal(item.max_score),
    )


def source_type_for_report(content_type: str) -> str:
    """由报告 MIME 确定证据来源类型（服务端决定，模型不能自行选择）。"""
    mapping = {
        "application/pdf": EVIDENCE_SOURCE_PDF_PAGE,
        (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ): EVIDENCE_SOURCE_DOCX_PARAGRAPH,
    }
    source_type = mapping.get(content_type)
    if source_type is None:
        raise extraction.ExtractError("不支持的报告类型，无法解析")
    return source_type


def build_report_source(
    data: bytes, *, content_type: str, max_chars: int
) -> grading_ai.ReportSource:
    """把报告字节流提取为**带位置**的文本单元并检查长度上限（契约 9.11 第 3 步）。

    保留 :func:`extract_units` 返回的 ``(位置, 文本)``——证据定位必须能落到
    具体的页码/段落号，而不是只留下拼接后的纯文本。

    :raises extraction.ExtractError: 扫描版 PDF、损坏文件、无文本或不支持的类型。
    :raises extraction.ExtractLimitExceeded: 超出字符预算。
    """
    units = extraction.extract_units(data, content_type=content_type)
    total = sum(len(text) for _, text in units)
    if total > max_chars:
        raise extraction.ExtractLimitExceeded(
            f"报告文本量（{total:,} 字符）超出批改上限（{max_chars:,} 字符）"
        )
    return grading_ai.ReportSource(
        source_type=source_type_for_report(content_type), units=units
    )


def _record_attempt(
    session: AsyncSession,
    *,
    submission_id: uuid.UUID,
    status: SubmissionGradeAttemptStatus,
    settings: Settings,
    item_count: int | None,
    raw_output: str | None,
    error: str | None,
    duration_ms: int,
    now: datetime,
) -> None:
    """写一条批改尝试记录（成功与失败都留痕，内容安全）。"""
    repo.add_grade_attempt(
        session,
        attempt_id=uuid.uuid4(),
        submission_id=submission_id,
        status=status,
        model=settings.ai_model.strip() or None,
        prompt_version=grading_ai.PROMPT_VERSION,
        item_count=item_count,
        raw_output=raw_output,
        error=error[:SUBMISSION_ERROR_MAX_LENGTH] if error else None,
        duration_ms=duration_ms,
        now=now,
    )


async def _lock_write_target(
    session: AsyncSession,
    *,
    claimed: ClaimedGradeJob,
    now: datetime,
):
    """按统一锁序加锁并复查令牌：课程 → Assignment → 提交版本 → 提交 → Job。

    :returns: ``(course, submission, job)``；复查失败时返回 ``(None, None, None)``。
    """
    course = await courses_repo.get_course_for_update(session, claimed.course_id)
    # 先普通读一次拿到 assignment_id / rubric_version_id，再按文档锁序加锁：
    # 课程 → Assignment → 提交固定 RubricVersion → Submission → Job
    probe = await repo.get_submission_by_id(session, claimed.submission_id)
    if probe is None:
        await session.rollback()
        return None, None, None
    await assignments_repo.lock_assignment(session, probe.assignment_id)
    if probe.rubric_version_id is not None:
        await assignments_repo.lock_rubric_version(
            session, probe.rubric_version_id
        )
    submission = await repo.lock_submission(session, claimed.submission_id)
    if submission is None:
        await session.rollback()
        return None, None, None
    job = await jobs_service.lock_submission_grade_job(
        session, submission_id=submission.id
    )
    if (
        job is None
        or job.run_token != claimed.run_token
        or job.status is not JobStatusValue.RUNNING
    ):
        await session.rollback()
        logger.info(
            "运行令牌不匹配或任务已非 RUNNING，放弃回写（submission=%s）",
            claimed.submission_id,
        )
        return None, None, None
    if job.lease_expires_at is not None and job.lease_expires_at <= now:
        # 租约已过期：本次执行不再拥有任务，交给重试后的新执行
        await session.rollback()
        logger.info("租约已过期，放弃回写（submission=%s）", claimed.submission_id)
        return None, None, None
    return course, submission, job


async def _write_success(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    claimed: ClaimedGradeJob,
    rubric: list[RubricItemSnapshot],
    validated: grading_ai.ValidatedGrade,
    settings: Settings,
    duration_ms: int,
    now: datetime,
) -> bool:
    """成功回写：一次性创建 ``GradeReview`` 与全部 ``GradeItem``（契约 9.11 第 7 步）。"""
    async with session_factory() as session:
        course, submission, job = await _lock_write_target(
            session, claimed=claimed, now=now
        )
        if job is None or submission is None:
            return False
        if submission.status is not SubmissionStatus.GRADING:
            await session.rollback()
            logger.info("提交已不在批改中，放弃发布（submission=%s）", claimed.submission_id)
            return False

        existing = await repo.lock_grade_review_by_submission(
            session, submission.id
        )
        if existing is not None:
            # 已有批改结果：绝不覆盖（正常流程不会走到这里）
            await session.rollback()
            logger.warning(
                "提交已有批改结果，放弃发布（submission=%s）", claimed.submission_id
            )
            return False

        if course is None or course.status is not CourseStatus.ACTIVE:
            # 生成期间课程被归档：任务取消、提交失败，不留下批改草稿
            submission.status = SubmissionStatus.FAILED
            submission.error_message = "课程在批改期间被归档"
            submission.updated_at = now
            job.status = JobStatusValue.CANCELLED
            job.finished_at = now
            _record_attempt(
                session,
                submission_id=submission.id,
                status=SubmissionGradeAttemptStatus.FAILED,
                settings=settings,
                item_count=None,
                raw_output=None,
                error="课程在批改期间被归档",
                duration_ms=duration_ms,
                now=now,
            )
            await session.commit()
            logger.info("课程已归档，批改取消（submission=%s）", claimed.submission_id)
            return False

        max_scores = {item.rubric_item_id: item.max_score for item in rubric}
        total = sum((item.score for item in validated.items), Decimal("0"))

        review = repo.add_grade_review(
            session,
            review_id=uuid.uuid4(),
            submission_id=submission.id,
            ai_summary=validated.summary,
            teacher_summary=validated.summary,
            suggested_total_score=total,
            final_total_score=total,
            now=now,
        )
        # 评分项通过外键指向批改记录；无 relationship 时 ORM 不保证跨表插入顺序，
        # 因此先 flush 批改行再插入评分项（否则会撞 grade_items 的外键约束）。
        await session.flush()
        for item in validated.items:
            repo.add_grade_item(
                session,
                item_id=uuid.uuid4(),
                review_id=review.id,
                rubric_item_id=item.rubric_item_id,
                # 评分项快照：批改时的标题与满分，之后 Rubric 变更不影响历史结果
                title=next(
                    snapshot.title
                    for snapshot in rubric
                    if snapshot.rubric_item_id == item.rubric_item_id
                ),
                max_score=max_scores[item.rubric_item_id],
                order=item.order,
                ai_score=item.score,
                final_score=item.score,
                ai_comment=item.comment,
                evidence_quote=item.evidence_quote,
                evidence_source_type=item.evidence_source_type,
                evidence_location_start=item.evidence_location_start,
                evidence_location_end=item.evidence_location_end,
                error_type=item.error_type,
                improvement_suggestion=item.improvement_suggestion,
                now=now,
            )

        submission.status = SubmissionStatus.REVIEW_REQUIRED
        submission.error_message = None
        submission.updated_at = now
        job.status = JobStatusValue.SUCCEEDED
        job.progress = 100
        job.error = None
        job.finished_at = now
        _record_attempt(
            session,
            submission_id=submission.id,
            status=SubmissionGradeAttemptStatus.SUCCEEDED,
            settings=settings,
            item_count=len(validated.items),
            raw_output=_dump_raw(validated),
            error=None,
            duration_ms=duration_ms,
            now=now,
        )
        await session.commit()
        return True


async def _write_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    claimed: ClaimedGradeJob,
    message: str,
    settings: Settings,
    duration_ms: int,
    cancelled: bool,
    now: datetime,
) -> None:
    """失败回写：按统一锁序置终态，**不写任何**批改结果（契约 9.11 第 8/9 步）。"""
    async with session_factory() as session:
        course, submission, job = await _lock_write_target(
            session, claimed=claimed, now=now
        )
        if job is None or submission is None:
            return

        archived = course is None or course.status is not CourseStatus.ACTIVE
        final_cancelled = cancelled or archived

        submission.status = SubmissionStatus.FAILED
        submission.error_message = None if final_cancelled else message[
            :SUBMISSION_ERROR_MAX_LENGTH
        ]
        submission.updated_at = now
        job.status = (
            JobStatusValue.CANCELLED if final_cancelled else JobStatusValue.FAILED
        )
        job.error = None if final_cancelled else message[:SUBMISSION_ERROR_MAX_LENGTH]
        job.finished_at = now
        _record_attempt(
            session,
            submission_id=submission.id,
            status=SubmissionGradeAttemptStatus.FAILED,
            settings=settings,
            item_count=None,
            raw_output=None,
            error=None if final_cancelled else message,
            duration_ms=duration_ms,
            now=now,
        )
        await session.commit()


def _dump_raw(validated: grading_ai.ValidatedGrade) -> str | None:
    """把服务端接受的结构化输出序列化后截断保存（审计用）。"""
    if validated.raw_output is None:
        return None
    try:
        payload = json.dumps(validated.raw_output, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - 输出来自 json.loads
        return None
    return payload[:RAW_OUTPUT_MAX_LENGTH]


async def run_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    claimed: ClaimedGradeJob,
    settings: Settings,
    storage: S3Storage,
    ai_client_factory: AiClientFactory | None = None,
) -> None:
    """执行一个已领取的批改任务（读取快照 → 下载 → 提取 → 模型 → 锁内回写）。"""
    lease_seconds = settings.submission_grade_lease_seconds
    stop_event = asyncio.Event()
    aborted = {"flag": False}
    started = time.monotonic()

    async def fail(message: str, *, cancelled: bool = False) -> None:
        await _write_failure(
            session_factory,
            claimed=claimed,
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
                logger.info(
                    "续租失败，执行方中止（submission=%s）", claimed.submission_id
                )
                return

    heartbeat = asyncio.create_task(heartbeat_loop())
    try:
        # ---- 1) 只读快照（读取后立即结束事务）----
        loaded = await load_snapshot(
            session_factory, submission_id=claimed.submission_id
        )
        if loaded is None:
            await fail("提交不存在或未固定评分规则版本")
            return
        snapshot, rubric = loaded
        if aborted["flag"]:
            return

        # ---- 2) 事务外：下载对象、校验、提取文本 ----
        try:
            data = await asyncio.to_thread(
                storage.get_object_verified,
                snapshot.object_key,
                expected_size=snapshot.size,
                expected_sha256_hex=snapshot.sha256,
            )
        except StorageObjectNotFoundError:
            await fail("报告对象不存在或已被清理")
            return
        except StorageVerificationError as exc:
            await fail(f"报告对象校验失败：{exc.reason}")
            return
        except StorageUnavailableError as exc:
            await fail(f"对象存储暂时不可用（{exc.reason}）")
            return

        if aborted["flag"]:
            return

        try:
            report = await asyncio.to_thread(
                build_report_source,
                data,
                content_type=snapshot.content_type,
                max_chars=settings.submission_grade_max_chars,
            )
        except extraction.ExtractError as exc:
            await fail(str(exc))
            return

        if aborted["flag"]:
            return

        # ---- 3) 事务外调用模型 ----
        ai_client: object | None = None
        try:
            if ai_client_factory is not None:
                ai_client = ai_client_factory()
            validated = await asyncio.to_thread(
                grading_ai.grade_submission,
                rubric=rubric,
                report=report,
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

        # ---- 4) 按统一锁序加锁后一次性发布 ----
        await _write_success(
            session_factory,
            claimed=claimed,
            rubric=rubric,
            validated=validated,
            settings=settings,
            duration_ms=int((time.monotonic() - started) * 1000),
            now=utc_now(),
        )
    except grading_ai.GradeModelNotConfiguredError:
        logger.warning("报告批改未配置模型（submission=%s）", claimed.submission_id)
        await fail("模型服务未配置")
    except grading_ai.GradeGenerationError as exc:
        logger.warning("报告批改失败（submission=%s）：%s", claimed.submission_id, exc)
        await fail(str(exc))
    except Exception as exc:  # noqa: BLE001 - 单任务失败不拖垮整批
        logger.exception("报告批改异常（submission=%s）", claimed.submission_id)
        await fail(_safe_error(exc))
    finally:
        stop_event.set()
        heartbeat.cancel()


async def run_pending_batch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    storage: S3Storage,
    ai_client_factory: AiClientFactory | None = None,
    max_jobs: int = DEFAULT_BATCH_SIZE,
    stop_requested: Callable[[], bool] | None = None,
) -> int:
    """领取并执行一批批改任务，返回处理数量。"""
    processed = 0
    lease_seconds = settings.submission_grade_lease_seconds
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
                storage=storage,
                ai_client_factory=ai_client_factory,
            )
        except Exception:  # noqa: BLE001 - 兜底：尽力写失败，不中断批次
            logger.exception("批改任务处理失败（job=%s）", claimed.job_id)
        processed += 1
    return processed


__all__ = [
    "ClaimedGradeJob",
    "DEFAULT_BATCH_SIZE",
    "SubmissionSnapshot",
    "build_report_source",
    "claim_next",
    "load_snapshot",
    "renew_lease",
    "run_job",
    "run_pending_batch",
    "source_type_for_report",
]
