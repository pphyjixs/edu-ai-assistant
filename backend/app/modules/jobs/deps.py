"""Jobs 写接口的前置守卫（契约 10.1 的两阶段处理）。

把"任务存在 → 关联资源可见性 → 角色 → 课程归档 → 类型/状态分流"放在依赖里，
请求体校验（``validate_empty_object_body``）自然排在它们之后：越权或归档请求
返回 ``401``/``404``/``403``/``409``，而不是 ``422``。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends

from app.db.session import SessionDep
from app.modules.auth.permissions import CurrentUserDep
from app.modules.jobs import service
from app.modules.jobs.service import RetryTarget


async def _retry_target(
    job_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> RetryTarget:
    """重试前置：完成可见性、角色、归档与类型分派（含行锁）。"""
    return await service.lock_retryable_job(session, user=user, job_id=job_id)


#: 已按类型分派并加锁的重试目标
RetryTargetDep = Annotated[RetryTarget, Depends(_retry_target)]

__all__ = ["RetryTargetDep"]
