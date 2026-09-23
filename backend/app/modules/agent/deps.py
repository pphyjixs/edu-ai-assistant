"""Agent 模块的依赖注入。

沿用 practice 模块的做法：把"认证 + 可见性 + 归档"这类前置检查放进依赖，
让端点在**请求体校验之前**就完成 401 / 404 / 403 / 409 判定——
契约要求错误优先级固定，越权或归档的请求不能先返回 422。

**锁顺序**（评审文档「一、#10」）：所有写路径统一为

```text
Course → ChatSession → AgentRun → Job
```

:func:`_locked_run_scope` 因此**先**用一次不加锁的只读查询拿到 ``course_id``，
再按上面的顺序依次加锁；旧实现在这里先锁会话、后锁课程，与取消、回写路径相反，
并发时会形成死锁环。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends

from app.core.errors import ResourceNotFoundError
from app.db.session import SessionDep
from app.modules.agent import repository as repo
from app.modules.agent import service as agent_service
from app.modules.auth.permissions import CurrentUserDep
from app.modules.chat import repository as chat_repo
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
    """会话所有者 → 课程成员 → 课程未归档；并按统一顺序锁住课程行与会话行。

    前置查询（取 ``course_id``）不加锁，拿到之后才按
    ``课程 → 会话`` 的顺序加锁，与取消、Worker 回写保持一致。
    """
    probe = await chat_repo.get_owned_session(
        session, session_id=session_id, user_id=user.id
    )
    if probe is None:
        raise ResourceNotFoundError()
    course_id = probe.course_id

    course = await courses_service.lock_member_course(
        session, user_id=user.id, course_id=course_id
    )
    courses_service.require_course_active(course)

    chat_session = await repo.lock_owned_session(
        session, session_id=session_id, user_id=user.id
    )
    if chat_session is None:  # pragma: no cover - 会话刚被并发删除
        raise ResourceNotFoundError()

    return RunScope(
        session_id=chat_session.id,
        course_id=course.id,
        is_staff=course.teacher_id == user.id,
    )


#: 已校验并锁定课程的 Run 前置上下文
RunScopeDep = Annotated[RunScope, Depends(_locked_run_scope)]


@dataclass(frozen=True, slots=True)
class ReadSessionScope:
    """只读会话接口（Run 列表 / active-run）所需的前置事实。"""

    session_id: uuid.UUID
    course_id: uuid.UUID
    is_staff: bool


async def _read_session_scope(
    session_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> ReadSessionScope:
    """只读路径**不加锁**，也**不要求课程未归档**。

    归档课程的历史问答应当仍然可读（契约 6.3 的列表语义），因此这里只做
    「会话属于我」+「我是课程成员」两项检查，且都统一 404 不暴露存在性。
    """
    chat_session = await chat_repo.get_owned_session(
        session, session_id=session_id, user_id=user.id
    )
    if chat_session is None:
        raise ResourceNotFoundError()
    course = await courses_service.require_member_course(
        session, user=user, course_id=chat_session.course_id
    )
    return ReadSessionScope(
        session_id=chat_session.id,
        course_id=course.id,
        is_staff=course.teacher_id == user.id,
    )


#: 只读会话接口的前置上下文
ReadSessionScopeDep = Annotated[ReadSessionScope, Depends(_read_session_scope)]


async def _cancellable_run(
    run_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> uuid.UUID:
    """取消接口的前置检查：Run 可见、且尚未结束。

    放在依赖里是为了让 404 / 409 早于请求体校验的 422（与其它模块一致）。
    """
    await agent_service.require_cancellable_run(session, user=user, run_id=run_id)
    return run_id


#: 已确认可取消的 Run ID
CancellableRunDep = Annotated[uuid.UUID, Depends(_cancellable_run)]


__all__ = [
    "CancellableRunDep",
    "ReadSessionScope",
    "ReadSessionScopeDep",
    "RunScope",
    "RunScopeDep",
]
