"""Courses 数据访问层。

只做查询与写入，不含业务判断，不提交事务——事务边界属于 ``service.py``
（见 ``docs/architecture.md`` 第 4 节）。

行级锁（``FOR UPDATE``）也在这里声明：重置邀请码、加入课程和归档课程都需要
先锁住课程行，避免并发事务读到同一份旧状态。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.courses.models import Course, CourseMember, CourseRole, CourseStatus


async def _count(session: AsyncSession, statement) -> int:
    """统计某个查询的总行数（用于分页 total）。"""
    result = await session.execute(
        select(func.count()).select_from(statement.subquery())
    )
    return int(result.scalar_one())


def add_course(
    session: AsyncSession,
    *,
    course_id: uuid.UUID,
    name: str,
    description: str,
    teacher_id: uuid.UUID,
    status: CourseStatus,
    invite_code: str,
    now: datetime,
) -> Course:
    """暂存课程；邀请码唯一约束冲突在提交时由数据库抛出。"""
    course = Course(
        id=course_id,
        name=name,
        description=description,
        teacher_id=teacher_id,
        status=status,
        invite_code=invite_code,
        created_at=now,
        updated_at=now,
    )
    session.add(course)
    return course


def add_course_member(
    session: AsyncSession,
    *,
    course_id: uuid.UUID,
    user_id: uuid.UUID,
    course_role: CourseRole,
    joined_at: datetime,
) -> CourseMember:
    """暂存成员记录；``(course_id, user_id)`` 冲突在提交时由数据库抛出。"""
    member = CourseMember(
        course_id=course_id,
        user_id=user_id,
        course_role=course_role,
        joined_at=joined_at,
    )
    session.add(member)
    return member


async def get_course_by_id(
    session: AsyncSession, course_id: uuid.UUID
) -> Course | None:
    return await session.get(Course, course_id)


async def get_course_for_update(
    session: AsyncSession, course_id: uuid.UUID
) -> Course | None:
    """锁住课程行，直到当前事务结束。

    用于「检查状态 → 写入」的临界区（重置邀请码、加入、归档），
    否则并发请求可能都读到 ACTIVE 后各自写入。
    """
    result = await session.execute(
        select(Course)
        .where(Course.id == course_id)
        .with_for_update()
        # 同一事务可能先按邀请码读过该课程；锁后必须覆盖 identity map 中的旧值。
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_course_by_invite_code(
    session: AsyncSession, invite_code: str
) -> Course | None:
    result = await session.execute(
        select(Course).where(Course.invite_code == invite_code)
    )
    return result.scalar_one_or_none()


async def invite_code_exists(session: AsyncSession, invite_code: str) -> bool:
    """判断邀请码是否已被占用，用于区分"唯一约束冲突"的原因。"""
    return await get_course_by_invite_code(session, invite_code) is not None


async def get_member(
    session: AsyncSession, *, course_id: uuid.UUID, user_id: uuid.UUID
) -> CourseMember | None:
    result = await session.execute(
        select(CourseMember).where(
            CourseMember.course_id == course_id, CourseMember.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def list_courses_for_user(
    session: AsyncSession, *, user_id: uuid.UUID, offset: int, limit: int
) -> tuple[list[Course], int]:
    """列出当前用户参与的课程（含归档），按创建时间倒序、ID 倒序。"""
    statement = (
        select(Course)
        .join(CourseMember, CourseMember.course_id == Course.id)
        .where(CourseMember.user_id == user_id)
    )
    total = await _count(session, statement)
    result = await session.execute(
        statement.order_by(Course.created_at.desc(), Course.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), total


async def list_members(
    session: AsyncSession, *, course_id: uuid.UUID, offset: int, limit: int
) -> tuple[list[CourseMember], int]:
    """列出课程成员（含创建教师），按加入时间正序、用户 ID 正序。"""
    statement = select(CourseMember).where(CourseMember.course_id == course_id)
    total = await _count(session, statement)
    result = await session.execute(
        statement.order_by(CourseMember.joined_at.asc(), CourseMember.user_id.asc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), total
