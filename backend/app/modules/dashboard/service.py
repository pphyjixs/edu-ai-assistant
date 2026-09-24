"""Dashboard 业务层（``docs/api-contract.md`` 第 11 节）。

只做三件事：统一取一次 ``now``、调用固定数量的只读聚合查询、把投影行组装成
响应模型。不持有任何业务状态、不提交事务、不加锁、不返回完整业务对象。

教师 / 学生作用域由 :data:`TeacherDep` / :data:`StudentDep` 在路由层保证
（角色不符直接 403 ROLE_FORBIDDEN），这里只接收已通过角色校验的用户。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.modules.auth.models import User
from app.modules.dashboard import repository as repo
from app.modules.dashboard.schemas import (
    RECENT_ITEM_LIMIT,
    StudentDashboard,
    StudentMaterialStatus,
    StudentPendingAssignment,
    StudentRecentFeedback,
    TeacherDashboard,
    TeacherFailedMaterial,
    TeacherRecentSubmission,
)
from app.modules.materials.models import MaterialStatus


def is_open_for_submission(
    *, due_at: datetime | None, allow_late_submission: bool, now: datetime
) -> bool:
    """是否仍可提交（契约 11.2 的待完成任务口径）。

    无截止 / 未到截止 / 允许补交均视为可提交；``now == due_at`` 视为已经截止。
    该口径与 :func:`app.modules.assignments.service.can_submit` 的截止判断一致，
    Dashboard 已在 SQL 层按 ``PUBLISHED`` 过滤，这里只复用截止时间语义。
    """
    if due_at is None:
        return True
    if now < due_at:
        return True
    return bool(allow_late_submission)


async def get_teacher_dashboard(session: AsyncSession, *, user: User) -> TeacherDashboard:
    """教师工作台：仅统计该教师创建且仍活动的课程（契约 11.1）。"""
    teacher_id = user.id

    active_course_count = await repo.count_active_teacher_courses(
        session, teacher_id=teacher_id
    )
    pending_grading_count = await repo.count_pending_grading(
        session, teacher_id=teacher_id
    )
    failed_material_count = await repo.count_failed_materials(
        session, teacher_id=teacher_id
    )

    recent_submissions = await repo.list_recent_teacher_submissions(
        session, teacher_id=teacher_id, limit=RECENT_ITEM_LIMIT
    )
    failed_materials = await repo.list_failed_materials(
        session, teacher_id=teacher_id, limit=RECENT_ITEM_LIMIT
    )

    return TeacherDashboard(
        active_course_count=active_course_count,
        pending_grading_count=pending_grading_count,
        failed_material_count=failed_material_count,
        recent_submissions=[
            TeacherRecentSubmission(
                submission_id=submission_id,
                assignment_id=assignment_id,
                course_id=course_id,
                student_id=student_id,
                student_name=display_name,
                status=status,
                is_late=is_late,
                submitted_at=submitted_at,
            )
            for (
                submission_id,
                assignment_id,
                course_id,
                student_id,
                display_name,
                status,
                is_late,
                submitted_at,
            ) in recent_submissions
        ],
        failed_materials=[
            TeacherFailedMaterial(
                material_id=material_id,
                course_id=course_id,
                filename=filename,
                error_message=error_message,
                updated_at=updated_at,
            )
            for (material_id, course_id, filename, error_message, updated_at) in failed_materials
        ],
    )


async def get_student_dashboard(session: AsyncSession, *, user: User) -> StudentDashboard:
    """学生工作台：仅统计本人加入且仍活动的课程（契约 11.2）。"""
    student_id = user.id
    now = utc_now()

    active_course_count = await repo.count_active_student_courses(
        session, student_id=student_id
    )
    pending_assignment_count = await repo.count_pending_assignments(
        session, student_id=student_id, now=now
    )
    material_counts = await repo.count_student_material_statuses(
        session, student_id=student_id
    )

    pending_assignments = await repo.list_pending_assignments(
        session, student_id=student_id, now=now, limit=RECENT_ITEM_LIMIT
    )
    recent_feedback = await repo.list_student_recent_feedback(
        session, student_id=student_id, limit=RECENT_ITEM_LIMIT
    )
    material_statuses = await repo.list_student_material_statuses(
        session, student_id=student_id, limit=RECENT_ITEM_LIMIT
    )

    return StudentDashboard(
        active_course_count=active_course_count,
        pending_assignment_count=pending_assignment_count,
        processing_material_count=material_counts.get(MaterialStatus.PROCESSING, 0),
        failed_material_count=material_counts.get(MaterialStatus.FAILED, 0),
        pending_assignments=[
            StudentPendingAssignment(
                assignment_id=assignment_id,
                course_id=course_id,
                title=title,
                due_at=due_at,
                allow_late_submission=allow_late_submission,
                published_at=published_at,
            )
            for (
                assignment_id,
                course_id,
                title,
                due_at,
                allow_late_submission,
                published_at,
            ) in pending_assignments
        ],
        recent_feedback=[
            StudentRecentFeedback(
                review_id=review_id,
                submission_id=submission_id,
                assignment_id=assignment_id,
                course_id=course_id,
                final_total_score=float(final_total_score),
                total_score=float(total_score),
                published_at=published_at,
            )
            for (
                review_id,
                submission_id,
                assignment_id,
                course_id,
                final_total_score,
                total_score,
                published_at,
            ) in recent_feedback
        ],
        material_statuses=[
            StudentMaterialStatus(
                material_id=material_id,
                course_id=course_id,
                filename=filename,
                status=status,
                error_message=error_message,
                updated_at=updated_at,
            )
            for (material_id, course_id, filename, status, error_message, updated_at) in material_statuses
        ],
    )


__all__ = ["get_student_dashboard", "get_teacher_dashboard", "is_open_for_submission"]
