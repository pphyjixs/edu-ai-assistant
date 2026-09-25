"""Agent Run 的数据访问层。

约定与其他模块一致：仓储层**只 add 不 commit**，事务边界由服务层与 Worker 决定。

``agent_runs`` 没有状态列，因此"是否还有未结束的 Run"必须与 ``jobs`` 连接查询
（``type=AGENT_RUN`` 且 ``resource_id = agent_runs.id``）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agent.models import AgentRun, AgentRunSource, AgentRunStep
from app.modules.chat.models import ChatSession
from app.modules.jobs import repository as jobs_repo
from app.modules.jobs.models import Job, JobStatusValue, JobType

_ACTIVE_RUN_SQL = text(
    """
    SELECT r.id
    FROM agent_runs AS r
    JOIN jobs AS j
      ON j.type = 'AGENT_RUN' AND j.resource_id = r.id
    WHERE r.session_id = :session_id
      AND j.status IN ('PENDING', 'RUNNING')
    ORDER BY r.created_at ASC
    LIMIT 1
    """
)


def add_run(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    action: object,
    input_message_id: uuid.UUID,
    client_request_id: str,
    entity_type: object | None,
    entity_id: uuid.UUID | None,
    section_id: uuid.UUID | None,
    selected_text: str | None,
    options: dict,
    now: datetime,
    request_fingerprint: str | None = None,
) -> AgentRun:
    """创建 Run 记录（未提交）。"""
    run = AgentRun(
        id=run_id,
        session_id=session_id,
        user_id=user_id,
        action=action,
        input_message_id=input_message_id,
        client_request_id=client_request_id,
        request_fingerprint=request_fingerprint,
        entity_type=entity_type,
        entity_id=entity_id,
        section_id=section_id,
        selected_text=selected_text,
        options=options,
        created_at=now,
        updated_at=now,
    )
    session.add(run)
    return run


def add_source(
    session: AsyncSession,
    *,
    source_id: uuid.UUID,
    run_id: uuid.UUID,
    order: int,
    source_type: object,
    target_id: uuid.UUID,
    material_id: uuid.UUID | None,
    chunk_id: uuid.UUID | None,
    location_start: int | None,
    location_end: int | None,
    label: str | None,
    snapshot: str | None,
    now: datetime,
) -> AgentRunSource:
    """记录一条实际注入的来源（未提交）。"""
    source = AgentRunSource(
        id=source_id,
        run_id=run_id,
        order=order,
        source_type=source_type,
        source_id=target_id,
        material_id=material_id,
        chunk_id=chunk_id,
        location_start=location_start,
        location_end=location_end,
        label=label,
        snapshot=snapshot,
        created_at=now,
    )
    session.add(source)
    return source


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> AgentRun | None:
    return await session.get(AgentRun, run_id)


async def get_run_for_update(
    session: AsyncSession, run_id: uuid.UUID
) -> AgentRun | None:
    result = await session.execute(
        select(AgentRun)
        .where(AgentRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_run_by_client_request_id(
    session: AsyncSession, *, user_id: uuid.UUID, client_request_id: str
) -> AgentRun | None:
    """幂等查询：同一用户的同一幂等键只对应一个 Run（契约 6.3）。"""
    result = await session.execute(
        select(AgentRun).where(
            AgentRun.user_id == user_id,
            AgentRun.client_request_id == client_request_id,
        )
    )
    return result.scalar_one_or_none()


async def find_active_run_id(
    session: AsyncSession, *, session_id: uuid.UUID
) -> uuid.UUID | None:
    """返回该会话中尚未结束（PENDING/RUNNING）的 Run，用于并发保护。"""
    row = (await session.execute(_ACTIVE_RUN_SQL, {"session_id": session_id})).first()
    return row.id if row is not None else None


async def get_active_run_with_job(
    session: AsyncSession, *, session_id: uuid.UUID
) -> tuple[AgentRun, Job] | None:
    """会话中尚未结束的 Run（含任务行），供 ``active-run`` 接口恢复轮询。"""
    row = (await session.execute(_ACTIVE_RUN_SQL, {"session_id": session_id})).first()
    if row is None:
        return None
    return await get_run_with_job(session, row.id)


async def list_runs_with_jobs(
    session: AsyncSession, *, session_id: uuid.UUID, limit: int
) -> list[tuple[AgentRun, Job]]:
    """会话内的 Run 列表（新→旧），含任务行。

    前端用它把 ``FAILED`` / ``CANCELLED`` 的原因显示在对应提问下面：
    刷新页面后，用户仍然能看到"这个问题为什么没有回答"（评审文档「一、#11」）。
    """
    result = await session.execute(
        select(AgentRun, Job)
        .join(Job, (Job.type == JobType.AGENT_RUN) & (Job.resource_id == AgentRun.id))
        .where(AgentRun.session_id == session_id)
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(max(1, limit))
    )
    return [(run, job) for run, job in result.all()]


async def count_runs(session: AsyncSession, *, session_id: uuid.UUID) -> int:
    """会话内的 Run 总数（列表分页用）。"""
    result = await session.execute(
        select(func.count()).select_from(AgentRun).where(AgentRun.session_id == session_id)
    )
    return int(result.scalar_one())


async def get_run_with_job(
    session: AsyncSession, run_id: uuid.UUID
) -> tuple[AgentRun, Job] | None:
    run = await session.get(AgentRun, run_id)
    if run is None:
        return None
    job = await jobs_repo.get_job_for_resource(
        session, job_type=JobType.AGENT_RUN, resource_id=run_id
    )
    if job is None:  # pragma: no cover - Run 与 Job 同事务创建
        return None
    return run, job


async def list_sources(
    session: AsyncSession, run_id: uuid.UUID
) -> list[AgentRunSource]:
    result = await session.execute(
        select(AgentRunSource)
        .where(AgentRunSource.run_id == run_id)
        .order_by(AgentRunSource.order.asc())
    )
    return list(result.scalars().all())


async def lock_owned_session(
    session: AsyncSession, *, session_id: uuid.UUID, user_id: uuid.UUID
) -> ChatSession | None:
    """锁住会话行并校验所有者（不存在或非所有者返回 ``None``，调用方转 404）。"""
    result = await session.execute(
        select(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.user_id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


# --------------------------------------------------------------------------- #
# 执行步骤审计（``agent_run_steps``，开发方案 7.1）
# --------------------------------------------------------------------------- #
def add_step(
    session: AsyncSession,
    *,
    step_id: uuid.UUID,
    run_id: uuid.UUID,
    step_order: int,
    kind: str,
    call_id: str,
    name: str,
    status: str,
    request_json: dict,
    response_json: dict | None,
    error_code: str | None,
    started_at: datetime | None,
    finished_at: datetime | None,
    now: datetime,
) -> AgentRunStep:
    """写一条执行步骤记录（未提交）。

    调用方负责在**短事务**里提交：工具执行很快，但等待模型期间绝不能持有事务
    （开发方案 5.2 的要求），因此步骤写入与模型调用在时间上完全分开。
    """
    step = AgentRunStep(
        id=step_id,
        run_id=run_id,
        step_order=step_order,
        kind=kind,
        call_id=call_id,
        name=name,
        status=status,
        request_json=request_json,
        response_json=response_json,
        error_code=error_code,
        started_at=started_at,
        finished_at=finished_at,
        created_at=now,
    )
    session.add(step)
    return step


async def next_step_order(session: AsyncSession, *, run_id: uuid.UUID) -> int:
    """下一个步骤序号（Run 内从 1 连续递增）。

    同一 Run 由单个 Worker 独占（租约 + ``run_token``），因此 ``max + 1``
    在真实并发下也只会被一个执行者计算。
    """
    value = await session.scalar(
        select(func.max(AgentRunStep.step_order)).where(AgentRunStep.run_id == run_id)
    )
    return int(value or 0) + 1


async def get_step_by_call_id(
    session: AsyncSession, *, run_id: uuid.UUID, call_id: str
) -> AgentRunStep | None:
    """按 ``(run_id, call_id)`` 读步骤，用于幂等重放。"""
    result = await session.execute(
        select(AgentRunStep).where(
            AgentRunStep.run_id == run_id, AgentRunStep.call_id == call_id
        )
    )
    return result.scalar_one_or_none()


async def list_steps(session: AsyncSession, run_id: uuid.UUID) -> list[AgentRunStep]:
    """按序号升序取一个 Run 的全部步骤。"""
    result = await session.execute(
        select(AgentRunStep)
        .where(AgentRunStep.run_id == run_id)
        .order_by(AgentRunStep.step_order.asc())
    )
    return list(result.scalars().all())


async def list_steps_for_runs(
    session: AsyncSession, *, run_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[AgentRunStep]]:
    """批量取多个 Run 的步骤，按 ``run_id`` 聚合（避免列表接口 N+1 查询）。"""
    if not run_ids:
        return {}
    result = await session.execute(
        select(AgentRunStep)
        .where(AgentRunStep.run_id.in_(run_ids))
        .order_by(AgentRunStep.run_id, AgentRunStep.step_order.asc())
    )
    grouped: dict[uuid.UUID, list[AgentRunStep]] = {}
    for step in result.scalars().all():
        grouped.setdefault(step.run_id, []).append(step)
    return grouped


async def count_active_runs(session: AsyncSession, *, session_id: uuid.UUID) -> int:
    """该会话未结束的 Run 数量（仅用于测试与断言）。"""
    row = (
        await session.execute(
            text(
                """
                SELECT count(*) AS total
                FROM agent_runs AS r
                JOIN jobs AS j ON j.type = 'AGENT_RUN' AND j.resource_id = r.id
                WHERE r.session_id = :session_id
                  AND j.status IN ('PENDING', 'RUNNING')
                """
            ),
            {"session_id": session_id},
        )
    ).first()
    return int(row.total) if row is not None else 0


__all__ = [
    "JobStatusValue",
    "add_run",
    "add_source",
    "add_step",
    "count_active_runs",
    "count_runs",
    "find_active_run_id",
    "get_active_run_with_job",
    "get_run",
    "get_run_by_client_request_id",
    "get_run_for_update",
    "get_run_with_job",
    "get_step_by_call_id",
    "list_runs_with_jobs",
    "list_sources",
    "list_steps",
    "list_steps_for_runs",
    "lock_owned_session",
    "next_step_order",
]
