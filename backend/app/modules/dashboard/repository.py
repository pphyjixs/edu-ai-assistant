"""Dashboard 数据访问层（``docs/api-contract.md`` 第 11 节）。

Dashboard 是跨模块只读聚合：这里用显式 SQL 投影（``select(col1, col2, ...)``）
直接读取课程、任务、提交、批改与资料等表，**不返回完整 ORM 对象**、不做懒加载、
不逐课程查询、不加 ``FOR UPDATE``、不提交事务。相邻查询允许看到刚写入数据的
短暂差异（不提供强一致快照），因此所有查询都是快照式普通读。

状态机不属于本模块：这里只按状态取值过滤，绝不修改任何行。写入、加锁与事务
边界仍由课程 / 任务 / 提交 / 资料等模块负责。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.assignments.models import (
    Assignment,
    AssignmentRubricVersion,
    AssignmentStatus,
)
from app.modules.auth.models import User
from app.modules.courses.models import Course, CourseMember, CourseStatus
from app.modules.grading.models import GradeReview, Submission, SubmissionStatus
from app.modules.materials.models import Material, MaterialStatus

#: 教师待批改：正式提交中尚未发布成绩的状态集合（契约 11.1）
PENDING_GRADING_STATUSES = (
    SubmissionStatus.SUBMITTED,
    SubmissionStatus.GRADING,
    SubmissionStatus.REVIEW_REQUIRED,
    SubmissionStatus.FAILED,
)

#: 学生资料处理状态：只统计处理中或失败（契约 11.2）
STUDENT_MATERIAL_STATUSES = (
    MaterialStatus.PROCESSING,
    MaterialStatus.FAILED,
)


def _student_active_course_ids(student_id: uuid.UUID):
    """学生本人加入且仍活动的课程 ID 子查询（教师/学生共用作用域）。"""
    return (
        select(CourseMember.course_id)
        .join(Course, Course.id == CourseMember.course_id)
        .where(
            CourseMember.user_id == student_id,
            Course.status == CourseStatus.ACTIVE,
        )
    )


def _pending_assignment_conditions(student_id: uuid.UUID, now: datetime):
    """学生待完成任务的过滤条件（列表与计数共用，避免口径漂移）。

    契约 11.2：任务 ``PUBLISHED``，无截止 / 未到截止 / 允许补交，
    且不存在本人 ``submitted_at`` 非空的正式提交；归档课程的任务由
    ``_student_active_course_ids`` 排除，``DRAFT``/``CLOSED``/``ARCHIVED``
    由 ``status == PUBLISHED`` 排除。
    """
    return (
        Assignment.course_id.in_(_student_active_course_ids(student_id)),
        Assignment.status == AssignmentStatus.PUBLISHED,
        or_(
            Assignment.due_at.is_(None),
            Assignment.due_at > now,
            Assignment.allow_late_submission.is_(True),
        ),
        ~exists(
            select(Submission.id).where(
                Submission.assignment_id == Assignment.id,
                Submission.student_id == student_id,
                Submission.submitted_at.is_not(None),
            )
        ),
    )


# --------------------------------------------------------------------------- #
# 教师
# --------------------------------------------------------------------------- #
async def count_active_teacher_courses(
    session: AsyncSession, *, teacher_id: uuid.UUID
) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(Course)
        .where(Course.teacher_id == teacher_id, Course.status == CourseStatus.ACTIVE)
    )
    return int(total or 0)


async def count_pending_grading(
    session: AsyncSession, *, teacher_id: uuid.UUID
) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(Submission)
        .join(Course, Course.id == Submission.course_id)
        .where(
            Course.teacher_id == teacher_id,
            Course.status == CourseStatus.ACTIVE,
            Submission.status.in_(PENDING_GRADING_STATUSES),
        )
    )
    return int(total or 0)


async def count_failed_materials(
    session: AsyncSession, *, teacher_id: uuid.UUID
) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(Material)
        .join(Course, Course.id == Material.course_id)
        .where(
            Course.teacher_id == teacher_id,
            Course.status == CourseStatus.ACTIVE,
            Material.status == MaterialStatus.FAILED,
            Material.deleted_at.is_(None),
        )
    )
    return int(total or 0)


async def list_recent_teacher_submissions(
    session: AsyncSession, *, teacher_id: uuid.UUID, limit: int
) -> list:
    """教师最近正式提交：按 ``submitted_at DESC, id DESC``。"""
    rows = await session.execute(
        select(
            Submission.id,
            Submission.assignment_id,
            Submission.course_id,
            Submission.student_id,
            User.display_name,
            Submission.status,
            Submission.is_late,
            Submission.submitted_at,
        )
        .select_from(Submission)
        .join(Course, Course.id == Submission.course_id)
        .join(User, User.id == Submission.student_id)
        .where(
            Course.teacher_id == teacher_id,
            Course.status == CourseStatus.ACTIVE,
            Submission.submitted_at.is_not(None),
        )
        .order_by(Submission.submitted_at.desc(), Submission.id.desc())
        .limit(limit)
    )
    return rows.all()


async def list_failed_materials(
    session: AsyncSession, *, teacher_id: uuid.UUID, limit: int
) -> list:
    """教师最近失败资料：按 ``updated_at DESC, id DESC``。"""
    rows = await session.execute(
        select(
            Material.id,
            Material.course_id,
            Material.filename,
            Material.error_message,
            Material.updated_at,
        )
        .select_from(Material)
        .join(Course, Course.id == Material.course_id)
        .where(
            Course.teacher_id == teacher_id,
            Course.status == CourseStatus.ACTIVE,
            Material.status == MaterialStatus.FAILED,
            Material.deleted_at.is_(None),
        )
        .order_by(Material.updated_at.desc(), Material.id.desc())
        .limit(limit)
    )
    return rows.all()


# --------------------------------------------------------------------------- #
# 学生
# --------------------------------------------------------------------------- #
async def count_active_student_courses(
    session: AsyncSession, *, student_id: uuid.UUID
) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(CourseMember)
        .join(Course, Course.id == CourseMember.course_id)
        .where(
            CourseMember.user_id == student_id,
            Course.status == CourseStatus.ACTIVE,
        )
    )
    return int(total or 0)


async def count_pending_assignments(
    session: AsyncSession, *, student_id: uuid.UUID, now: datetime
) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(Assignment)
        .where(*_pending_assignment_conditions(student_id, now))
    )
    return int(total or 0)


async def list_pending_assignments(
    session: AsyncSession, *, student_id: uuid.UUID, now: datetime, limit: int
) -> list:
    """学生待完成任务：按 ``due_at ASC NULLS LAST, published_at DESC, id DESC``。"""
    rows = await session.execute(
        select(
            Assignment.id,
            Assignment.course_id,
            Assignment.title,
            Assignment.due_at,
            Assignment.allow_late_submission,
            Assignment.published_at,
        )
        .select_from(Assignment)
        .where(*_pending_assignment_conditions(student_id, now))
        .order_by(
            Assignment.due_at.asc().nulls_last(),
            Assignment.published_at.desc(),
            Assignment.id.desc(),
        )
        .limit(limit)
    )
    return rows.all()


async def count_student_material_statuses(
    session: AsyncSession, *, student_id: uuid.UUID
) -> dict[MaterialStatus, int]:
    """学生可见资料的处理状态计数（仅 PROCESSING / FAILED，未删除）。"""
    rows = await session.execute(
        select(Material.status, func.count())
        .select_from(Material)
        .where(
            Material.course_id.in_(_student_active_course_ids(student_id)),
            Material.status.in_(STUDENT_MATERIAL_STATUSES),
            Material.deleted_at.is_(None),
        )
        .group_by(Material.status)
    )
    return {status: int(count) for status, count in rows.all()}


async def list_student_material_statuses(
    session: AsyncSession, *, student_id: uuid.UUID, limit: int
) -> list:
    """学生最近处理中或失败资料：按 ``updated_at DESC, id DESC``。"""
    rows = await session.execute(
        select(
            Material.id,
            Material.course_id,
            Material.filename,
            Material.status,
            Material.error_message,
            Material.updated_at,
        )
        .select_from(Material)
        .where(
            Material.course_id.in_(_student_active_course_ids(student_id)),
            Material.status.in_(STUDENT_MATERIAL_STATUSES),
            Material.deleted_at.is_(None),
        )
        .order_by(Material.updated_at.desc(), Material.id.desc())
        .limit(limit)
    )
    return rows.all()


async def list_student_recent_feedback(
    session: AsyncSession, *, student_id: uuid.UUID, limit: int
) -> list:
    """学生最近已发布反馈：按 ``published_at DESC, id DESC``。"""
    rows = await session.execute(
        select(
            GradeReview.id,
            GradeReview.submission_id,
            Submission.assignment_id,
            Submission.course_id,
            GradeReview.final_total_score,
            AssignmentRubricVersion.total_score,
            GradeReview.published_at,
        )
        .select_from(GradeReview)
        .join(Submission, Submission.id == GradeReview.submission_id)
        .join(
            AssignmentRubricVersion,
            AssignmentRubricVersion.id == Submission.rubric_version_id,
        )
        .where(
            GradeReview.published_at.is_not(None),
            Submission.student_id == student_id,
            Submission.course_id.in_(_student_active_course_ids(student_id)),
        )
        .order_by(GradeReview.published_at.desc(), GradeReview.id.desc())
        .limit(limit)
    )
    return rows.all()


__all__ = [
    "PENDING_GRADING_STATUSES",
    "STUDENT_MATERIAL_STATUSES",
    "count_active_student_courses",
    "count_active_teacher_courses",
    "count_failed_materials",
    "count_pending_assignments",
    "count_pending_grading",
    "count_student_material_statuses",
    "list_failed_materials",
    "list_pending_assignments",
    "list_recent_teacher_submissions",
    "list_student_material_statuses",
    "list_student_recent_feedback",
]
