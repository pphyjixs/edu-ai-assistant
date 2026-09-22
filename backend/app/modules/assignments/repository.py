"""Assignments 数据访问层（``docs/api-contract.md`` 第 8 节）。

只做查询与写入，不含业务判断、不提交事务——事务边界在 ``service.py``。

评分规则版本是**只追加**的：这里没有更新或删除版本的函数，历史版本只由
:func:`list_rubric_versions` 读出（供测试与后续 Grading 模块使用）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.assignments.models import (
    STUDENT_VISIBLE_STATUSES,
    Assignment,
    AssignmentRubricItem,
    AssignmentRubricVersion,
    AssignmentStatus,
)


def add_assignment(
    session: AsyncSession,
    *,
    assignment_id: uuid.UUID,
    course_id: uuid.UUID,
    created_by: uuid.UUID,
    title: str,
    description: str,
    due_at: datetime | None,
    allow_late_submission: bool,
    status: AssignmentStatus,
    now: datetime,
) -> Assignment:
    """创建任务（``DRAFT``）；与评分版本、评分项在同一事务写入。"""
    assignment = Assignment(
        id=assignment_id,
        course_id=course_id,
        created_by=created_by,
        title=title,
        description=description,
        due_at=due_at,
        allow_late_submission=allow_late_submission,
        status=status,
        created_at=now,
        updated_at=now,
    )
    session.add(assignment)
    return assignment


async def get_assignment_by_id(
    session: AsyncSession, assignment_id: uuid.UUID
) -> Assignment | None:
    """按 ID 读任务（不加锁）。"""
    return await session.get(Assignment, assignment_id)


async def lock_assignment(
    session: AsyncSession, assignment_id: uuid.UUID
) -> Assignment | None:
    """锁住任务行（写路径的临界区）。

    锁顺序固定为 **课程 → 任务 → 当前评分版本**，因此调用方必须已经拿到
    课程行锁。
    """
    result = await session.execute(
        select(Assignment)
        .where(Assignment.id == assignment_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def list_assignments(
    session: AsyncSession,
    *,
    course_id: uuid.UUID,
    include_drafts: bool,
    offset: int,
    limit: int,
) -> tuple[list[Assignment], int]:
    """课程任务列表：``created_at DESC, id DESC``；学生视角在 SQL 层排除草稿。"""
    conditions = [Assignment.course_id == course_id]
    if not include_drafts:
        conditions.append(Assignment.status.in_(STUDENT_VISIBLE_STATUSES))

    total = await session.scalar(
        select(func.count()).select_from(Assignment).where(*conditions)
    )
    result = await session.execute(
        select(Assignment)
        .where(*conditions)
        .order_by(Assignment.created_at.desc(), Assignment.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), int(total or 0)


async def rubric_overview(
    session: AsyncSession, *, assignment_ids: list[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, Decimal]]:
    """批量取"当前评分版本号与总分"，避免列表接口逐条查询。"""
    if not assignment_ids:
        return {}
    result = await session.execute(
        select(
            AssignmentRubricVersion.assignment_id,
            AssignmentRubricVersion.version,
            AssignmentRubricVersion.total_score,
        )
        .join(
            Assignment,
            Assignment.current_rubric_version_id == AssignmentRubricVersion.id,
        )
        .where(Assignment.id.in_(assignment_ids))
    )
    return {row[0]: (int(row[1]), row[2]) for row in result.all()}


def add_rubric_version(
    session: AsyncSession,
    *,
    version_id: uuid.UUID,
    assignment_id: uuid.UUID,
    version: int,
    total_score: Decimal,
    created_by: uuid.UUID,
    now: datetime,
) -> AssignmentRubricVersion:
    """追加一个评分规则版本（只追加，不更新）。"""
    rubric_version = AssignmentRubricVersion(
        id=version_id,
        assignment_id=assignment_id,
        version=version,
        total_score=total_score,
        created_by=created_by,
        created_at=now,
    )
    session.add(rubric_version)
    return rubric_version


def add_rubric_item(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    rubric_version_id: uuid.UUID,
    title: str,
    description: str,
    max_score: Decimal,
    order: int,
    now: datetime,
) -> AssignmentRubricItem:
    """追加一个评分项（每个版本都是新记录、新 ID）。"""
    item = AssignmentRubricItem(
        id=item_id,
        rubric_version_id=rubric_version_id,
        title=title,
        description=description,
        max_score=max_score,
        order=order,
        created_at=now,
    )
    session.add(item)
    return item


async def get_rubric_version_by_id(
    session: AsyncSession, version_id: uuid.UUID
) -> AssignmentRubricVersion | None:
    """按 ID 读评分版本（不加锁，读接口使用）。"""
    return await session.get(AssignmentRubricVersion, version_id)


async def lock_rubric_version(
    session: AsyncSession, version_id: uuid.UUID
) -> AssignmentRubricVersion | None:
    """锁住评分版本行。

    锁顺序固定为 **课程 → 任务 → 当前评分版本**（契约 8.1），因此调用方必须
    已经拿到课程与任务行锁。写事务（修改、发布、关闭）都必须走到这一步：
    版本只追加、任务行锁已串行化同一任务的写入，缺了这把锁就不再符合文档化的
    锁协议，也挡不住把版本行当作并发临界区的写入方。
    """
    result = await session.execute(
        select(AssignmentRubricVersion)
        .where(AssignmentRubricVersion.id == version_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def list_rubric_items(
    session: AsyncSession, *, rubric_version_id: uuid.UUID
) -> list[AssignmentRubricItem]:
    """按 ``order`` 升序取某版本的评分项。"""
    result = await session.execute(
        select(AssignmentRubricItem)
        .where(AssignmentRubricItem.rubric_version_id == rubric_version_id)
        .order_by(AssignmentRubricItem.order)
    )
    return list(result.scalars().all())


async def list_rubric_versions(
    session: AsyncSession, *, assignment_id: uuid.UUID
) -> list[AssignmentRubricVersion]:
    """按版本号升序取全部历史版本（供测试与后续 Grading 使用）。"""
    result = await session.execute(
        select(AssignmentRubricVersion)
        .where(AssignmentRubricVersion.assignment_id == assignment_id)
        .order_by(AssignmentRubricVersion.version)
    )
    return list(result.scalars().all())


async def max_rubric_version(
    session: AsyncSession, *, assignment_id: uuid.UUID
) -> int:
    """当前最大版本号；无版本时返回 0。"""
    value = await session.scalar(
        select(func.max(AssignmentRubricVersion.version)).where(
            AssignmentRubricVersion.assignment_id == assignment_id
        )
    )
    return int(value or 0)


__all__ = [
    "add_assignment",
    "add_rubric_item",
    "add_rubric_version",
    "get_assignment_by_id",
    "get_rubric_version_by_id",
    "list_assignments",
    "list_rubric_items",
    "list_rubric_versions",
    "lock_assignment",
    "lock_rubric_version",
    "max_rubric_version",
    "rubric_overview",
]
