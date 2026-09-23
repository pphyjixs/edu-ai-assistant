"""Assignments 写接口的前置守卫：两阶段处理中的"资源检查 + 加锁"阶段。

契约 8.1 固定了写接口的检查顺序：

    认证 → 课程/任务存在及成员可见性 → 角色与课程创建者 → 课程归档
    → 任务状态 → 请求体结构与字段 → RUBRIC_SCORE_MISMATCH → 数据写入

FastAPI 先解析依赖、后校验请求体，因此把**资源检查、状态检查与行锁**放在依赖里，
请求体校验（Pydantic）自然排在其后；守卫拿到的行锁与端点写入处于同一个请求会话
（同一事务），写入仍在锁内完成。

请求体改用原始 ``Request`` 手工解析（`app.core.request_body`），
因此请求模型由路由的 ``openapi_extra`` 显式声明、并由
:func:`app.core.openapi.install_explicit_schemas` 补进导出文档。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends

from app.db.session import SessionDep
from app.modules.assignments import service
from app.modules.assignments.models import Assignment
from app.modules.auth.permissions import CurrentUserDep
from app.modules.courses.models import Course


async def _locked_creator_course(
    course_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> Course:
    """创建接口前置：锁课程 → 成员（404）→ 创建教师（403）→ 未归档（409）。"""
    return await service.lock_creator_course(session, user=user, course_id=course_id)


async def _locked_editable_assignment(
    assignment_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> Assignment:
    """修改接口前置：锁课程与任务 → 必须可修改（`DRAFT` / `PUBLISHED`）。"""
    return await service.lock_writable_assignment(
        session, user=user, assignment_id=assignment_id
    )


async def _locked_publishable_assignment(
    assignment_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> Assignment:
    """发布接口前置：锁课程与任务 → 必须可发布（`DRAFT` / `PUBLISHED`）。"""
    return await service.lock_writable_assignment(
        session, user=user, assignment_id=assignment_id, publishable=True
    )


async def _locked_closable_assignment(
    assignment_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> Assignment:
    """关闭接口前置：锁课程与任务 → 必须可关闭（`PUBLISHED` / `CLOSED`）。"""
    return await service.lock_writable_assignment(
        session, user=user, assignment_id=assignment_id, closable=True
    )


async def _locked_reopenable_assignment(
    assignment_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> Assignment:
    """重新开启接口前置：锁课程与任务 → 必须可重新开启（`CLOSED` / `PUBLISHED`）。"""
    return await service.lock_writable_assignment(
        session, user=user, assignment_id=assignment_id, reopenable=True
    )


async def _locked_attachment_assignment(
    assignment_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> Assignment:
    """附件写路径前置：锁课程与任务 → 创建教师 → 课程未归档 → 任务未归档。"""
    return await service.lock_attachment_writable_assignment(
        session, user=user, assignment_id=assignment_id
    )


async def _visible_assignment(
    assignment_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> Assignment:
    """附件读路径前置：任务对当前用户可见（学生读草稿按不存在处理）。

    直接复用任务详情的可见性规则，避免附件列表与任务详情给出不一致的结论。
    """
    detail = await service.get_assignment_detail(
        session, user=user, assignment_id=assignment_id
    )
    return detail.assignment


#: 已锁定的课程（创建接口）
CreatorCourseDep = Annotated[Course, Depends(_locked_creator_course)]
#: 已锁定且可修改的任务
EditableAssignmentDep = Annotated[Assignment, Depends(_locked_editable_assignment)]
#: 已锁定且可发布的任务
PublishableAssignmentDep = Annotated[
    Assignment, Depends(_locked_publishable_assignment)
]
#: 已锁定且可关闭的任务
ClosableAssignmentDep = Annotated[Assignment, Depends(_locked_closable_assignment)]
#: 已锁定且可重新开启的任务
ReopenableAssignmentDep = Annotated[
    Assignment, Depends(_locked_reopenable_assignment)
]
#: 已锁定且可改附件的任务（创建教师）
AttachmentAssignmentDep = Annotated[
    Assignment, Depends(_locked_attachment_assignment)
]
#: 对当前用户可见的任务（附件读路径）
VisibleAssignmentDep = Annotated[Assignment, Depends(_visible_assignment)]

__all__ = [
    "AttachmentAssignmentDep",
    "ClosableAssignmentDep",
    "CreatorCourseDep",
    "EditableAssignmentDep",
    "PublishableAssignmentDep",
    "ReopenableAssignmentDep",
    "VisibleAssignmentDep",
]
