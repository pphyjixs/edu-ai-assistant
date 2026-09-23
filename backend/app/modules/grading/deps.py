"""Grading 写接口的前置守卫：两阶段处理中的"资源检查 + 加锁"阶段（契约 9.1）。

契约 9.1 固定了写接口的检查顺序：

    认证 → 资源可见性 → 角色 → 课程归档 → 业务状态 → 请求结构 → 字段与分数语义 → 写入

FastAPI 先解析依赖、后校验请求体，因此把**资源检查、状态检查与行锁**放在依赖里，
请求体校验（Pydantic / 手工解析）自然排在其后；守卫拿到的行锁与端点写入处于同一个
请求会话（同一事务），写入仍在锁内完成。

请求体改用原始 ``Request`` 手工解析（``app.core.request_body``），因此请求模型由路由的
``openapi_extra`` 显式声明、并由 :func:`app.core.openapi.install_explicit_schemas`
补进导出文档。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends

from app.db.session import SessionDep
from app.modules.auth.permissions import CurrentUserDep
from app.modules.grading import service
from app.modules.grading.service import (
    CreatorReviewGuard,
    CreatorSubmissionGuard,
    StudentAssignmentGuard,
    StudentUploadGuard,
)


async def _student_assignment(
    assignment_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> StudentAssignmentGuard:
    """初始化上传前置：锁课程与任务 → 仅学生 → 未归档 → 仍允许提交。"""
    return await service.lock_student_assignment(
        session, user=user, assignment_id=assignment_id
    )


async def _student_upload(
    assignment_id: uuid.UUID,
    upload_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> StudentUploadGuard:
    """完成提交前置：锁课程、任务、提交版本与上传会话（会话必须属于本人）。"""
    return await service.lock_student_upload(
        session, user=user, assignment_id=assignment_id, upload_id=upload_id
    )


async def _creator_submission(
    submission_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> CreatorSubmissionGuard:
    """触发批改前置：锁课程、任务、提交固定版本与提交 → 必须为课程创建教师。"""
    return await service.lock_creator_submission(
        session, user=user, submission_id=submission_id
    )


async def _creator_review(
    review_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> CreatorReviewGuard:
    """复核/发布前置：在触发批改的锁序后追加批改行锁。"""
    return await service.lock_creator_review(
        session, user=user, review_id=review_id
    )


#: 已锁定的学生上传目标（初始化上传）
StudentAssignmentDep = Annotated[
    StudentAssignmentGuard, Depends(_student_assignment)
]
#: 已锁定的完成提交目标
StudentUploadDep = Annotated[StudentUploadGuard, Depends(_student_upload)]
#: 已锁定的教师提交目标（触发批改）
CreatorSubmissionDep = Annotated[
    CreatorSubmissionGuard, Depends(_creator_submission)
]
#: 已锁定的教师复核目标（复核 / 发布）
CreatorReviewDep = Annotated[CreatorReviewGuard, Depends(_creator_review)]

__all__ = [
    "CreatorReviewDep",
    "CreatorSubmissionDep",
    "StudentAssignmentDep",
    "StudentUploadDep",
]
