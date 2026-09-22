"""Agent Run 的服务层（``docs/local-development-agent-backend.md`` 第 6.2–6.5 节）。

创建 Run 的事务做八件事（文档 6.4）：

1. 锁定并验证会话所有权；
2. 读取会话课程并验证课程仍可写；
3. 验证目标对象属于课程且用户可见（:func:`context.validate_context_target`）；
4. 按 ``client_request_id`` 做幂等判断；
5. 同一会话只允许一个未结束的 Run；
6. 保存用户消息（并推进会话版本与 ``last_message_at``）；
7. 创建 ``agent_runs``；
8. 创建 ``AGENT_RUN`` 任务。

提交后立即返回 ``202``——模型调用由独立 Worker 在事务外完成，API 绝不等模型。

**锁顺序**：课程行 → 会话行 → Run 行 → 任务行（与练习模块的"课程 → 业务 → 任务"
一致），避免与其他写路径交叉时死锁。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    AgentRunInProgressError,
    AgentRunNotCancellableError,
    ChatConflictError,
    ResourceNotFoundError,
)
from app.core.time import utc_now
from app.modules.agent import context as context_module
from app.modules.agent import repository as repo
from app.modules.agent.deps import RunScope
from app.modules.agent.models import AgentRun
from app.modules.agent.schemas import AgentRunCreateRequest
from app.modules.auth.models import User
from app.modules.chat import repository as chat_repo
from app.modules.chat.models import ChatMessage, ChatMessageRole, ChatSession
from app.modules.courses import service as courses_service
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobStatusValue

logger = logging.getLogger("app.agent.service")


async def _require_owned_session(
    session: AsyncSession, *, user: User, session_id: uuid.UUID
) -> ChatSession:
    """会话所有者检查：不存在或非所有者统一 404（契约 6.1）。"""
    chat_session = await chat_repo.get_owned_session(
        session, session_id=session_id, user_id=user.id
    )
    if chat_session is None:
        raise ResourceNotFoundError()
    return chat_session


async def create_run(
    session: AsyncSession,
    *,
    user: User,
    scope: RunScope,
    request: AgentRunCreateRequest,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[AgentRun, Job]:
    """创建 Run 与任务（同一事务）。

    前置的认证、会话所有权、课程成员与归档检查已由 ``RunScopeDep`` 完成并锁住
    课程行，这里继续做上下文可见性、幂等与并发保护，然后写入。

    :raises AgentContextUnsupportedError: 实体类型未实现 / 缺少必填 ID
    :raises AgentContextNotReadyError: 资料尚未解析完成
    :raises AgentRunInProgressError: 该会话已有未结束的 Run
    """
    created_at = now or utc_now()
    session_id = scope.session_id

    # 目标对象属于本课程且对当前用户可见（资料还要检查解析状态）
    entity_type = request.context.entity_type if request.context else None
    entity_id = request.context.entity_id if request.context else None
    section_id = request.context.section_id if request.context else None
    selected_text = request.context.selected_text if request.context else None
    await context_module.validate_context_target(
        session,
        course_id=scope.course_id,
        entity_type=entity_type,
        entity_id=entity_id,
        section_id=section_id,
        is_staff=scope.is_staff,
    )

    # 幂等：同一用户的同一 client_request_id 返回既有 Run（文档 6.3）
    existing = await repo.get_run_by_client_request_id(
        session, user_id=user.id, client_request_id=request.client_request_id
    )
    if existing is not None:
        pair = await repo.get_run_with_job(session, existing.id)
        if pair is not None:
            logger.info(
                "幂等重放：run=%s client_request_id=%s",
                existing.id,
                request.client_request_id,
            )
            return pair

    # 第一版每个会话最多一个未结束的 Run（文档 6.5）
    if await repo.find_active_run_id(session, session_id=session_id) is not None:
        raise AgentRunInProgressError()

    # 会话行已由 RunScopeDep 锁定，版本不会被并发修改
    locked_session = await repo.lock_owned_session(
        session, session_id=session_id, user_id=user.id
    )
    if locked_session is None:  # pragma: no cover - 会话刚被删除
        raise ResourceNotFoundError()

    if not await chat_repo.bump_session_version(
        session,
        session_id=session_id,
        expected_version=locked_session.version,
        now=created_at,
    ):
        await session.rollback()
        raise ChatConflictError()

    input_message = chat_repo.add_message(
        session,
        message_id=uuid.uuid4(),
        session_id=session_id,
        role=ChatMessageRole.USER,
        content=request.input,
        grounded=None,
        now=created_at,
    )
    await session.flush()

    run = repo.add_run(
        session,
        run_id=uuid.uuid4(),
        session_id=session_id,
        user_id=user.id,
        action=request.action,
        input_message_id=input_message.id,
        client_request_id=request.client_request_id,
        entity_type=entity_type,
        entity_id=entity_id,
        section_id=section_id,
        selected_text=selected_text,
        options=request.options.model_dump(exclude_none=True) if request.options else {},
        now=created_at,
    )

    # 任务与 Run 同事务；状态、进度、错误只存在这张表
    job = jobs_service.create_agent_run_job(session, run_id=run.id, now=created_at)

    await session.commit()
    logger.info(
        "创建 Agent Run run=%s action=%s entity=%s",
        run.id,
        request.action.value,
        entity_type.value if entity_type else "COURSE",
    )
    return run, job


async def get_run(
    session: AsyncSession, *, user: User, run_id: uuid.UUID
) -> tuple[AgentRun, Job]:
    """按 ID 读取 Run；非所有者或不存在统一 404。"""
    pair = await repo.get_run_with_job(session, run_id)
    if pair is None or pair[0].user_id != user.id:
        raise ResourceNotFoundError()
    return pair


async def cancel_run(
    session: AsyncSession, *, user: User, run_id: uuid.UUID
) -> tuple[AgentRun, Job]:
    """取消一个尚未结束的 Run（文档 6.2 的可选接口）。

    只把任务置为 ``CANCELLED``；正在执行模型调用的 Worker 会在回写时因为
    ``status != RUNNING`` 而放弃写入，因此不会出现"取消后仍冒出助手消息"。

    :raises ResourceNotFoundError: Run 不存在或不属于当前用户
    :raises AgentRunNotCancellableError: 任务已经结束
    """
    pair = await repo.get_run_with_job(session, run_id)
    if pair is None or pair[0].user_id != user.id:
        raise ResourceNotFoundError()
    run, _ = pair

    chat_session = await _require_owned_session(session, user=user, session_id=run.session_id)

    # 统一锁顺序：课程 → 会话 → Run → 任务
    await courses_service.lock_member_course(
        session, user_id=user.id, course_id=chat_session.course_id
    )
    locked_session = await repo.lock_owned_session(
        session, session_id=run.session_id, user_id=user.id
    )
    if locked_session is None:  # pragma: no cover
        raise ResourceNotFoundError()
    locked_run = await repo.get_run_for_update(session, run_id)
    if locked_run is None:  # pragma: no cover
        raise ResourceNotFoundError()
    job = await jobs_service.lock_agent_run_job(session, run_id=run_id)
    if job is None:  # pragma: no cover
        raise ResourceNotFoundError()

    if job.status in (
        JobStatusValue.SUCCEEDED,
        JobStatusValue.FAILED,
        JobStatusValue.CANCELLED,
    ):
        raise AgentRunNotCancellableError()

    cancelled_at = utc_now()
    job.status = JobStatusValue.CANCELLED
    job.finished_at = cancelled_at
    locked_run.updated_at = cancelled_at
    await session.commit()
    logger.info("取消 Agent Run run=%s", run_id)
    return locked_run, job


def output_message_of(run: AgentRun, message: ChatMessage | None) -> uuid.UUID | None:
    """回填助手消息 ID 后的便捷读取（供测试与路由复用）。"""
    return run.output_message_id or (message.id if message is not None else None)
