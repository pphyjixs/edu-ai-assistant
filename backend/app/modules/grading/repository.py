"""Grading 数据访问层（``docs/api-contract.md`` 第 9 节）。

只做查询与写入，不含业务判断、不提交事务——事务边界在 ``service.py``。

行级锁（``FOR UPDATE``）在这里声明；调用方必须按契约 9.1 的固定顺序加锁：
**课程 → Assignment → 提交固定的 RubricVersion → Submission →（UploadSession / Job / GradeReview）**。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.assignments.models import AssignmentRubricVersion
from app.modules.grading.models import (
    GradeItem,
    GradeReview,
    Submission,
    SubmissionGradeAttempt,
    SubmissionGradeAttemptStatus,
    SubmissionStatus,
    SubmissionUploadSession,
)

#: 每轮过期上传清理最多处理的会话数
CLEANUP_BATCH_LIMIT = 200


# --------------------------------------------------------------------------- #
# submissions
# --------------------------------------------------------------------------- #
def add_submission(
    session: AsyncSession,
    *,
    submission_id: uuid.UUID,
    assignment_id: uuid.UUID,
    course_id: uuid.UUID,
    student_id: uuid.UUID,
    status: SubmissionStatus,
    filename: str,
    content_type: str,
    size: int,
    sha256: str,
    object_key: str,
    now: datetime,
) -> Submission:
    """创建提交记录（初始为 ``UPLOADING``）。"""
    submission = Submission(
        id=submission_id,
        assignment_id=assignment_id,
        course_id=course_id,
        student_id=student_id,
        status=status,
        filename=filename,
        content_type=content_type,
        size=size,
        sha256=sha256,
        object_key=object_key,
        created_at=now,
        updated_at=now,
    )
    session.add(submission)
    return submission


async def get_submission_by_id(
    session: AsyncSession, submission_id: uuid.UUID
) -> Submission | None:
    """按 ID 读提交（不加锁）。"""
    return await session.get(Submission, submission_id)


async def get_submission_for_student(
    session: AsyncSession, *, assignment_id: uuid.UUID, student_id: uuid.UUID
) -> Submission | None:
    """按 ``(assignment_id, student_id)`` 读提交（不加锁）。"""
    result = await session.execute(
        select(Submission).where(
            Submission.assignment_id == assignment_id,
            Submission.student_id == student_id,
        )
    )
    return result.scalar_one_or_none()


async def lock_submission(
    session: AsyncSession, submission_id: uuid.UUID
) -> Submission | None:
    """锁住提交行。

    调用方必须已经拿到课程、任务与提交固定评分版本的行锁（契约 9.1）。
    """
    result = await session.execute(
        select(Submission)
        .where(Submission.id == submission_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def lock_submission_for_student(
    session: AsyncSession, *, assignment_id: uuid.UUID, student_id: uuid.UUID
) -> Submission | None:
    """锁住 ``(assignment_id, student_id)`` 的提交行（初始化上传的临界区）。"""
    result = await session.execute(
        select(Submission)
        .where(
            Submission.assignment_id == assignment_id,
            Submission.student_id == student_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def list_assignment_submissions(
    session: AsyncSession,
    *,
    assignment_id: uuid.UUID,
    offset: int,
    limit: int,
) -> tuple[list[Submission], int]:
    """教师视角：某任务下全部**正式提交**，按 ``submitted_at DESC, id DESC``。

    ``UPLOADING``（``submitted_at IS NULL``）不进入教师列表（契约 9.4）。
    """
    conditions = (
        Submission.assignment_id == assignment_id,
        Submission.submitted_at.is_not(None),
    )
    total = await session.scalar(
        select(func.count()).select_from(Submission).where(*conditions)
    )
    result = await session.execute(
        select(Submission)
        .where(*conditions)
        .order_by(Submission.submitted_at.desc(), Submission.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), int(total or 0)


async def list_student_submissions(
    session: AsyncSession, *, assignment_id: uuid.UUID, student_id: uuid.UUID
) -> tuple[list[Submission], int]:
    """学生视角：只返回**本人的正式提交**（``submitted_at`` 非空，0–1 条）。

    仅初始化了上传、尚未完成提交的 ``UPLOADING`` 记录不进入列表（契约 9.4）：
    学生在正式完成前列表为空，完成后返回一条。
    """
    result = await session.execute(
        select(Submission).where(
            Submission.assignment_id == assignment_id,
            Submission.student_id == student_id,
            Submission.submitted_at.is_not(None),
        )
    )
    items = list(result.scalars().all())
    return items, len(items)


async def rubric_version_numbers(
    session: AsyncSession, *, version_ids: list[uuid.UUID]
) -> dict[uuid.UUID, int]:
    """批量取评分版本号，避免列表接口逐条查询。"""
    ids = [version_id for version_id in version_ids if version_id is not None]
    if not ids:
        return {}
    result = await session.execute(
        select(AssignmentRubricVersion.id, AssignmentRubricVersion.version).where(
            AssignmentRubricVersion.id.in_(ids)
        )
    )
    return {row[0]: int(row[1]) for row in result.all()}


# --------------------------------------------------------------------------- #
# submission_upload_sessions
# --------------------------------------------------------------------------- #
def add_upload_session(
    session: AsyncSession,
    *,
    upload_id: uuid.UUID,
    submission_id: uuid.UUID,
    course_id: uuid.UUID,
    assignment_id: uuid.UUID,
    student_id: uuid.UUID,
    object_key: str,
    filename: str,
    content_type: str,
    size: int,
    sha256: str,
    upload_url_expires_at: datetime,
    confirm_deadline_at: datetime,
    now: datetime,
) -> SubmissionUploadSession:
    """创建一次上传尝试。"""
    upload = SubmissionUploadSession(
        id=upload_id,
        submission_id=submission_id,
        course_id=course_id,
        assignment_id=assignment_id,
        student_id=student_id,
        object_key=object_key,
        filename=filename,
        content_type=content_type,
        size=size,
        sha256=sha256,
        upload_url_expires_at=upload_url_expires_at,
        confirm_deadline_at=confirm_deadline_at,
        created_at=now,
        updated_at=now,
    )
    session.add(upload)
    return upload


async def get_upload_session(
    session: AsyncSession, upload_id: uuid.UUID
) -> SubmissionUploadSession | None:
    return await session.get(SubmissionUploadSession, upload_id)


async def lock_upload_session(
    session: AsyncSession, upload_id: uuid.UUID
) -> SubmissionUploadSession | None:
    """锁住上传会话行（完成上传的临界区）。"""
    result = await session.execute(
        select(SubmissionUploadSession)
        .where(SubmissionUploadSession.id == upload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def list_active_upload_sessions(
    session: AsyncSession, submission_id: uuid.UUID
) -> list[SubmissionUploadSession]:
    """列出某提交下**仍有效**（未完成、未过期、未被替代）的上传会话。

    重新初始化上传时用它们把旧会话标记为"被替代"（契约 9.2）。
    """
    result = await session.execute(
        select(SubmissionUploadSession).where(
            SubmissionUploadSession.submission_id == submission_id,
            SubmissionUploadSession.completed_at.is_(None),
            SubmissionUploadSession.expired_at.is_(None),
            SubmissionUploadSession.superseded_at.is_(None),
        )
    )
    return list(result.scalars().all())


async def list_cleanable_upload_sessions(
    session: AsyncSession,
    *,
    now: datetime,
    url_expired_before: datetime,
    limit: int,
) -> list[SubmissionUploadSession]:
    """可清理的上传会话（契约 9.3 的清理协议）。

    必须同时满足：

    - ``completed_at IS NULL``（已完成会话及其报告对象**永不清理**）；
    - ``expired_at IS NULL``（未清理过，保证命令幂等）；
    - 已超过确认窗口**或**已被替代；
    - ``upload_url_expires_at <= url_expired_before``：预签名 PUT 已过期
      并经过删除缓冲期（``SUBMISSION_UPLOAD_DELETE_BUFFER_SECONDS``），
      此前浏览器仍可能拿着旧地址直传，立即删除会留下重建窗口。

    用 ``FOR UPDATE SKIP LOCKED`` 锁定，与并发的完成请求互斥：完成请求先拿到锁
    并写入 ``completed_at`` 时，本查询读不到该行，因此**不会误删已确认的报告对象**。
    """
    result = await session.execute(
        select(SubmissionUploadSession)
        .where(
            SubmissionUploadSession.completed_at.is_(None),
            SubmissionUploadSession.expired_at.is_(None),
            SubmissionUploadSession.upload_url_expires_at <= url_expired_before,
            (
                SubmissionUploadSession.superseded_at.is_not(None)
            )
            | (SubmissionUploadSession.confirm_deadline_at <= now),
        )
        .order_by(
            SubmissionUploadSession.confirm_deadline_at, SubmissionUploadSession.id
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(result.scalars().all())


def save_completion_snapshot(
    session: AsyncSession, upload: SubmissionUploadSession, *, snapshot: dict
) -> None:
    """写入完成响应快照（重复完成返回首次结果）。"""
    upload.completion_snapshot = snapshot
    session.add(upload)


# --------------------------------------------------------------------------- #
# grade_reviews / grade_items
# --------------------------------------------------------------------------- #
def add_grade_review(
    session: AsyncSession,
    *,
    review_id: uuid.UUID,
    submission_id: uuid.UUID,
    ai_summary: str,
    teacher_summary: str,
    suggested_total_score: Decimal,
    final_total_score: Decimal,
    now: datetime,
) -> GradeReview:
    """创建批改记录（AI 建议自动复制为初始终稿，``reviewed_at`` 仍为空）。"""
    review = GradeReview(
        id=review_id,
        submission_id=submission_id,
        ai_summary=ai_summary,
        teacher_summary=teacher_summary,
        suggested_total_score=suggested_total_score,
        final_total_score=final_total_score,
        created_at=now,
        updated_at=now,
    )
    session.add(review)
    return review


def add_grade_item(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    review_id: uuid.UUID,
    rubric_item_id: uuid.UUID,
    title: str,
    max_score: Decimal,
    order: int,
    ai_score: Decimal,
    final_score: Decimal,
    ai_comment: str,
    evidence_quote: str,
    evidence_source_type: str,
    evidence_location_start: int,
    evidence_location_end: int,
    error_type: str,
    improvement_suggestion: str,
    now: datetime,
) -> GradeItem:
    """创建评分项结果（``final_score`` 初始等于 ``ai_score``）。"""
    item = GradeItem(
        id=item_id,
        review_id=review_id,
        rubric_item_id=rubric_item_id,
        title=title,
        max_score=max_score,
        order=order,
        ai_score=ai_score,
        final_score=final_score,
        ai_comment=ai_comment,
        evidence_quote=evidence_quote,
        evidence_source_type=evidence_source_type,
        evidence_location_start=evidence_location_start,
        evidence_location_end=evidence_location_end,
        error_type=error_type,
        improvement_suggestion=improvement_suggestion,
        created_at=now,
        updated_at=now,
    )
    session.add(item)
    return item


async def get_grade_review_by_submission(
    session: AsyncSession, submission_id: uuid.UUID
) -> GradeReview | None:
    result = await session.execute(
        select(GradeReview).where(GradeReview.submission_id == submission_id)
    )
    return result.scalar_one_or_none()


async def get_grade_review_by_id(
    session: AsyncSession, review_id: uuid.UUID
) -> GradeReview | None:
    return await session.get(GradeReview, review_id)


async def lock_grade_review(
    session: AsyncSession, review_id: uuid.UUID
) -> GradeReview | None:
    """锁住批改行（复核与发布的临界区）。

    锁顺序的最后一步：调用方必须已经拿到课程、任务、提交版本与提交行锁。
    """
    result = await session.execute(
        select(GradeReview)
        .where(GradeReview.id == review_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def lock_grade_review_by_submission(
    session: AsyncSession, submission_id: uuid.UUID
) -> GradeReview | None:
    """按提交锁住批改行（Worker 回写与按提交访问的复核路径）。"""
    result = await session.execute(
        select(GradeReview)
        .where(GradeReview.submission_id == submission_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def list_grade_items(
    session: AsyncSession, *, review_id: uuid.UUID
) -> list[GradeItem]:
    """按 ``order`` 升序取批改的评分项。"""
    result = await session.execute(
        select(GradeItem)
        .where(GradeItem.review_id == review_id)
        .order_by(GradeItem.order)
    )
    return list(result.scalars().all())


# --------------------------------------------------------------------------- #
# submission_grade_attempts
# --------------------------------------------------------------------------- #
def add_grade_attempt(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    submission_id: uuid.UUID,
    status: SubmissionGradeAttemptStatus,
    model: str | None,
    prompt_version: str | None,
    item_count: int | None,
    raw_output: str | None,
    error: str | None,
    duration_ms: int,
    now: datetime,
) -> SubmissionGradeAttempt:
    """写一条批改尝试记录（成功与失败都留痕，内容安全）。"""
    attempt = SubmissionGradeAttempt(
        id=attempt_id,
        submission_id=submission_id,
        status=status,
        model=model,
        prompt_version=prompt_version,
        item_count=item_count,
        raw_output=raw_output,
        error=error,
        duration_ms=duration_ms,
        created_at=now,
    )
    session.add(attempt)
    return attempt


__all__ = [
    "CLEANUP_BATCH_LIMIT",
    "add_grade_attempt",
    "add_grade_item",
    "add_grade_review",
    "add_submission",
    "add_upload_session",
    "get_grade_review_by_id",
    "get_grade_review_by_submission",
    "get_submission_by_id",
    "get_submission_for_student",
    "get_upload_session",
    "list_active_upload_sessions",
    "list_assignment_submissions",
    "list_cleanable_upload_sessions",
    "list_grade_items",
    "list_student_submissions",
    "lock_grade_review",
    "lock_grade_review_by_submission",
    "lock_submission",
    "lock_submission_for_student",
    "lock_upload_session",
    "rubric_version_numbers",
    "save_completion_snapshot",
]
