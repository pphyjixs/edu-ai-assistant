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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    AgentContextUnsupportedError,
    AgentIdempotencyConflictError,
    AgentRunInProgressError,
    AgentRunNotCancellableError,
    ChatConflictError,
    ResourceNotFoundError,
)
from app.core.time import utc_now
from app.modules.agent import context as context_module
from app.modules.agent import repository as repo
from app.modules.agent import skills, user_skills
from app.modules.agent.deps import RunScope
from app.modules.agent.models import AgentRun
from app.modules.agent.schemas import (
    ACTION_CONTEXT_MATRIX,
    TERMINAL_STATUSES,
    AgentRunCreateRequest,
    allowed_contexts,
    request_fingerprint,
)
from app.modules.auth.models import User
from app.modules.chat import repository as chat_repo
from app.modules.chat.models import ChatMessage, ChatMessageRole, ChatSession
from app.modules.courses import service as courses_service
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobStatusValue

logger = logging.getLogger("app.agent.service")

#: 任务的终态（与 :data:`app.modules.agent.schemas.TERMINAL_STATUSES` 同源）
TERMINAL_JOB_STATUSES = TERMINAL_STATUSES


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


def _require_allowed_combination(
    request: AgentRunCreateRequest,
) -> None:
    """校验 action ↔ context 矩阵（评审文档「一、#8」）。

    旧实现接受 ``CHECK_SUBMISSION + COURSE``、``BREAK_DOWN_ASSIGNMENT + MATERIAL``
    这类无意义组合，结果是要么生成跑题的回答、要么跑到一半才发现上下文用不上。
    这里在创建 Run 时就同步拒绝，并把**允许的组合**写进提示，方便前端修。
    """
    entity_type = request.context.entity_type if request.context else None
    if entity_type in ACTION_CONTEXT_MATRIX[request.action]:
        return
    raise AgentContextUnsupportedError(
        f"{request.action.value} 不支持 "
        f"{'COURSE' if entity_type is None else entity_type.value} 上下文；"
        f"允许的是 {allowed_contexts(request.action)}"
    )


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

    :raises AgentContextUnsupportedError: 实体类型未实现 / action-context 组合非法
    :raises AgentContextNotReadyError: 资料尚未解析完成
    :raises AgentRunInProgressError: 该会话已有未结束的 Run
    :raises AgentIdempotencyConflictError: 同一幂等键被用于不同请求
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

    # 可见性之后才校验组合：契约的错误优先级是「可见状态 → action/context 矩阵」，
    # 反过来会让不可见的对象被组合校验提前暴露成 422 而不是 404。
    _require_allowed_combination(request)

    if request.options and request.options.selected_skill_ids:
        try:
            user_skills.assert_owned_skill_ids(
                settings, user.id, request.options.selected_skill_ids
            )
        except ValueError as exc:
            raise ResourceNotFoundError(str(exc)) from exc
    if request.options and request.options.selected_skill_names:
        available = set(skills.load_skill_catalog().names())
        if any(name not in available for name in request.options.selected_skill_names):
            raise ResourceNotFoundError("所选系统 Skill 不存在")

    fingerprint = request_fingerprint(request)

    # 幂等：同一用户的同一 client_request_id 返回既有 Run（文档 6.3）
    existing = await repo.get_run_by_client_request_id(
        session, user_id=user.id, client_request_id=request.client_request_id
    )
    if existing is not None:
        return await _replay_existing_run(
            session,
            existing,
            fingerprint=fingerprint,
            client_request_id=request.client_request_id,
        )

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
        request_fingerprint=fingerprint,
        entity_type=entity_type,
        entity_id=entity_id,
        section_id=section_id,
        selected_text=selected_text,
        options=request.options.model_dump(mode="json", exclude_none=True) if request.options else {},
        now=created_at,
    )

    # 任务与 Run 同事务；状态、进度、错误只存在这张表
    job = jobs_service.create_agent_run_job(session, run_id=run.id, now=created_at)

    try:
        await session.commit()
    except IntegrityError:
        # 并发重放：两个请求同时带着同一个 client_request_id 进来时，
        # 先查后插会撞上唯一约束。回滚后回查即可拿到"已经存在的那一个"
        # （评审文档「一、#12」），而不是把 500 抛给用户。
        await session.rollback()
        winner = await repo.get_run_by_client_request_id(
            session, user_id=user.id, client_request_id=request.client_request_id
        )
        if winner is None:  # pragma: no cover - 冲突不是来自这条约束
            raise
        return await _replay_existing_run(
            session,
            winner,
            fingerprint=fingerprint,
            client_request_id=request.client_request_id,
        )

    logger.info(
        "创建 Agent Run run=%s action=%s entity=%s",
        run.id,
        request.action.value,
        entity_type.value if entity_type else "COURSE",
    )
    return run, job


async def _replay_existing_run(
    session: AsyncSession,
    existing: AgentRun,
    *,
    fingerprint: str,
    client_request_id: str,
) -> tuple[AgentRun, Job]:
    """幂等重放：校验请求指纹一致后返回既有 Run。"""
    if existing.request_fingerprint and existing.request_fingerprint != fingerprint:
        logger.warning(
            "幂等键 %s 被复用于不同请求，返回冲突（run=%s）",
            client_request_id,
            existing.id,
        )
        raise AgentIdempotencyConflictError(
            "这次请求与之前用同一个请求编号提交的内容不一致，请换一个请求编号重试"
        )
    pair = await repo.get_run_with_job(session, existing.id)
    if pair is None:  # pragma: no cover - Run 与 Job 同事务创建
        raise ResourceNotFoundError()
    logger.info("幂等重放：run=%s client_request_id=%s", existing.id, client_request_id)
    return pair


async def get_run(
    session: AsyncSession, *, user: User, run_id: uuid.UUID
) -> tuple[AgentRun, Job]:
    """按 ID 读取 Run；非所有者或不存在统一 404。"""
    pair = await repo.get_run_with_job(session, run_id)
    if pair is None or pair[0].user_id != user.id:
        raise ResourceNotFoundError()
    return pair


async def get_active_run(
    session: AsyncSession, *, user: User, session_id: uuid.UUID
) -> tuple[AgentRun, Job] | None:
    """会话中尚未结束的 Run（评审文档「一、#11」）。

    页面打开时调用：拿到就用它恢复轮询，刷新页面不会让"正在生成"的提示消失。
    会话不可见时统一 404，避免用这个接口探测别人的会话。
    """
    chat_session = await chat_repo.get_owned_session(
        session, session_id=session_id, user_id=user.id
    )
    if chat_session is None:
        raise ResourceNotFoundError()
    return await repo.get_active_run_with_job(session, session_id=session_id)


async def list_runs(
    session: AsyncSession, *, user: User, session_id: uuid.UUID, limit: int
) -> tuple[list[tuple[AgentRun, Job]], int]:
    """会话内的 Run 列表（新→旧）与总数。

    前端把 ``FAILED`` / ``CANCELLED`` 的原因挂在对应的提问下面，
    因此刷新之后用户仍然能知道"上一次为什么没有回答"。
    """
    chat_session = await chat_repo.get_owned_session(
        session, session_id=session_id, user_id=user.id
    )
    if chat_session is None:
        raise ResourceNotFoundError()
    items = await repo.list_runs_with_jobs(session, session_id=session_id, limit=limit)
    total = await repo.count_runs(session, session_id=session_id)
    return items, total


async def require_cancellable_run(
    session: AsyncSession, *, user: User, run_id: uuid.UUID
) -> None:
    """取消操作的**前置检查**（不写库、不加锁）。

    放在请求体校验之前，保证错误优先级是 404/409 → 422，与其它模块一致：
    请求体格式不对不应该掩盖"这条 Run 已经结束了"这个更重要的结论。
    """
    pair = await repo.get_run_with_job(session, run_id)
    if pair is None or pair[0].user_id != user.id:
        raise ResourceNotFoundError()
    _, job = pair
    if job.status in TERMINAL_JOB_STATUSES:
        raise AgentRunNotCancellableError()


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

    if job.status in TERMINAL_JOB_STATUSES:
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
