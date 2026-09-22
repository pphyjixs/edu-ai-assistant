"""Agent 模块的依赖注入。

沿用 practice 模块的做法：把"认证 + 可见性 + 归档"这类前置检查放进依赖，
让端点在**请求体校验之前**就完成 401 / 404 / 403 / 409 判定——
契约要求错误优先级固定，越权或归档的请求不能先返回 422。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends

from app.core.errors import ResourceNotFoundError
from app.db.session import SessionDep
from app.modules.agent import repository as repo
from app.modules.auth.permissions import CurrentUserDep
from app.modules.courses import service as courses_service


@dataclass(frozen=True, slots=True)
class RunScope:
    """创建 Run 所需的前置事实。"""

    session_id: uuid.UUID
    course_id: uuid.UUID
    #: 当前用户是否为课程创建教师（决定作业可见性与上下文注入范围）
    is_staff: bool


async def _locked_run_scope(
    session_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> RunScope:
    """会话所有者 → 课程成员 → 课程未归档；并按统一顺序锁住课程行。"""
    chat_session = await repo.lock_owned_session(
        session, session_id=session_id, user_id=user.id
    )
    if chat_session is None:
        raise ResourceNotFoundError()

    course = await courses_service.lock_member_course(
        session, user_id=user.id, course_id=chat_session.course_id
    )
    courses_service.require_course_active(course)
    return RunScope(
        session_id=chat_session.id,
        course_id=course.id,
        is_staff=course.teacher_id == user.id,
    )


#: 已校验并锁定课程的 Run 前置上下文
RunScopeDep = Annotated[RunScope, Depends(_locked_run_scope)]

__all__ = ["RunScope", "RunScopeDep"]
