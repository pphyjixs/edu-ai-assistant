"""练习写接口的前置守卫：两阶段处理中的"资源检查 + 加锁"阶段。

契约 7.1 固定了检查顺序：

    认证 → 资源可见性 → 角色 → 课程归档/资源状态 → 请求体结构与字段 → 业务写入冲突

FastAPI 先解析依赖、后校验请求体，因此把**资源检查与行锁**放在依赖里，
请求体校验（Pydantic）自然排在它们之后；守卫拿到的行锁与端点写入处于
同一个请求会话（同一事务），写入仍在锁内完成。

由此实现"HTTP 层无需手工解析请求体"，同时保留 OpenAPI 的请求体 Schema。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends

from app.db.session import SessionDep
from app.modules.auth.permissions import CurrentUserDep
from app.modules.courses.models import Course
from app.modules.practice import service
from app.modules.practice.models import PracticeSet


async def _locked_teacher_course(
    course_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> Course:
    """生成接口的前置：锁课程 → 成员（404）→ 创建教师（403）→ 未归档（409）。"""
    return await service.lock_teacher_course(session, user=user, course_id=course_id)


async def _locked_publish_set(
    set_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> PracticeSet:
    """发布接口的前置：锁课程 → 创建教师 → 锁练习 → 状态可发布。"""
    return await service.lock_set_for_publish(session, user=user, set_id=set_id)


async def _locked_submit_set(
    set_id: uuid.UUID, user: CurrentUserDep, session: SessionDep
) -> PracticeSet:
    """提交接口的前置：锁课程 → 成员/角色/归档 → 锁练习 → 已发布。"""
    return await service.lock_set_for_submit(session, user=user, set_id=set_id)


#: 已锁定的课程（生成接口）
LockedCourseDep = Annotated[Course, Depends(_locked_teacher_course)]
#: 已锁定的练习（发布接口）
LockedPublishSetDep = Annotated[PracticeSet, Depends(_locked_publish_set)]
#: 已锁定的练习（提交接口）
LockedSubmitSetDep = Annotated[PracticeSet, Depends(_locked_submit_set)]

#: 重试接口的守卫已上移到 :mod:`app.modules.jobs.deps`（契约 10.2 的统一分派）
__all__ = [
    "LockedCourseDep",
    "LockedPublishSetDep",
    "LockedSubmitSetDep",
]
