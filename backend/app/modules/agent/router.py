"""Agent Run 的 HTTP 接口（``docs/local-development-agent-backend.md`` 第 6.2 节 +
``docs/agent-backend-implementation-review.md``「一、#11 / #12」）。

路径一览：

- ``POST /chat-sessions/{session_id}/runs`` → ``202``，**数百毫秒内返回**，不等模型；
- ``GET /agent-runs/{run_id}`` → ``200``，前端按 30s/2s、之后/5s 的节奏轮询；
- ``POST /agent-runs/{run_id}/cancel`` → ``202``；
- ``GET /chat-sessions/{session_id}/runs`` → ``200`` 分页列表（含失败原因）；
- ``GET /chat-sessions/{session_id}/active-run`` → ``200``，``run`` 可能为 ``null``。

最后两条是为了"刷新后仍能恢复"：前端打开会话时先查 active run，有就继续轮询；
历史里 ``FAILED`` / ``CANCELLED`` 的 Run 也带着原因，因此用户能知道
上一次为什么没有回答。

错误优先级与其他模块一致：认证 → 会话可见 → 课程成员 → 归档 → 上下文可见性／
action-context 组合 → 请求体结构与字段。因此越权请求返回 404/403/409/422，
而不是笼统的 422。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, Request, status

from app.core.deps import SettingsDep
from app.core.errors import ErrorCode
from app.core.pagination import Page
from app.core.request_body import (
    EMPTY_OBJECT_REQUEST_BODY,
    body_loader,
    validate_empty_object_body,
)
from app.core.schemas import ErrorResponse
from app.db.session import SessionDep
from app.modules.agent import repository as repo
from app.modules.agent import service
from app.modules.agent.deps import (
    CancellableRunDep,
    ReadSessionScopeDep,
    RunScopeDep,
)
from app.modules.agent.models import AgentRun, AgentRunStep
from app.modules.agent.schemas import (
    AgentActiveRunSchema,
    AgentRunArtifactSchema,
    AgentRunCreateRequest,
    AgentRunSchema,
    AgentRunSourceSchema,
    AgentRunStepSchema,
)
from app.modules.auth.permissions import CurrentUserDep
from app.modules.jobs.models import Job

agent_router = APIRouter(tags=["agent"])

#: 用原始 Request 手工解析、需要显式补进 OpenAPI 组件的请求模型
AGENT_REQUEST_MODELS = (AgentRunCreateRequest,)

#: ``GET /chat-sessions/{id}/runs`` 的返回条数上限
RUN_LIST_DEFAULT_LIMIT = 20
RUN_LIST_MAX_LIMIT = 50

RUN_REQUEST_BODY: dict = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/AgentRunCreateRequest"}
            }
        },
    }
}

_AUTH_ERRORS: dict[int | str, dict] = {
    401: {
        "model": ErrorResponse,
        "description": "Access Token 缺失、无效或已过期（AUTH_TOKEN_EXPIRED）",
    }
}

_NOT_FOUND: dict = {
    "model": ErrorResponse,
    "description": "会话不存在、不是所有者，或目标对象不属于本课程/不可见（RESOURCE_NOT_FOUND）",
}

_AGENT_ERRORS: dict[int | str, dict] = {
    403: {
        "model": ErrorResponse,
        "description": "课程已归档时为 COURSE_ARCHIVED；上下文不可用见 409/422",
    },
    404: _NOT_FOUND,
    409: {
        "model": ErrorResponse,
        "description": (
            "AGENT_RUN_IN_PROGRESS（该会话已有未结束的 Run）、"
            "AGENT_IDEMPOTENCY_CONFLICT（同一请求编号被用于不同内容）、"
            "COURSE_ARCHIVED（课程已归档）或 AGENT_CONTEXT_NOT_READY（资料未解析完成）"
        ),
    },
    422: {
        "model": ErrorResponse,
        "description": (
            "AGENT_CONTEXT_UNSUPPORTED（实体类型或 action/context 组合未实现）"
            "或 VALIDATION_ERROR（字段结构、长度与未知字段校验失败）"
        ),
    },
}


def _artifacts_of(steps: list[AgentRunStep]) -> list[AgentRunArtifactSchema]:
    """从步骤的 ``response_json`` 里聚合本次 Run 产出的业务结果。

    artifact 与"是哪一次工具调用产生的"绑定在一起，因此不需要单独建表；
    按 ``(kind, id)`` 去重，重试重放时不会重复出现同一张卡片。
    """
    seen: set[tuple[str, str]] = set()
    artifacts: list[AgentRunArtifactSchema] = []
    for step in steps:
        payload = step.response_json or {}
        for item in payload.get("artifacts") or []:
            if not isinstance(item, dict):
                continue
            try:
                artifact = AgentRunArtifactSchema.model_validate(item)
            except Exception:  # noqa: BLE001 - 老数据或裁剪过的条目直接跳过
                continue
            key = (artifact.kind, str(artifact.id))
            if key in seen:
                continue
            seen.add(key)
            artifacts.append(artifact)
    return artifacts


def _run_schema(
    run: AgentRun,
    job: Job,
    sources: list | None = None,
    steps: list[AgentRunStep] | None = None,
) -> AgentRunSchema:
    """把「Run 行 + 任务行 + 步骤」组装成响应；状态与进度来自任务行。"""
    step_rows = steps or []
    return AgentRunSchema(
        id=run.id,
        session_id=run.session_id,
        action=run.action,
        status=job.status,
        progress=job.progress,
        input_message_id=run.input_message_id,
        output_message_id=run.output_message_id,
        error=job.error,
        failure_stage=job.failure_stage,
        evidence_level=run.evidence_level,
        created_at=run.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        sources=[
            AgentRunSourceSchema(
                order=source.order,
                source_type=source.source_type.value
                if hasattr(source.source_type, "value")
                else str(source.source_type),
                source_id=source.source_id,
                material_id=source.material_id,
                chunk_id=source.chunk_id,
                location_start=source.location_start,
                location_end=source.location_end,
                label=source.label or "",
            )
            for source in (sources or [])
        ],
        steps=[
            AgentRunStepSchema(
                order=step.step_order,
                kind=step.kind,
                name=step.name,
                status=step.status,
                error_code=step.error_code,
            )
            for step in step_rows
        ],
        artifacts=_artifacts_of(step_rows),
    )


@agent_router.post(
    "/chat-sessions/{session_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AgentRunSchema,
    summary="创建 Agent Run",
    description=(
        "在指定会话中创建一个异步 Agent Run：同一事务写入用户消息、"
        "`agent_runs` 与 `AGENT_RUN` 任务后立即返回 202，模型调用由独立 Worker 完成。"
        "`client_request_id` 在用户范围内唯一，网络重试会返回同一个 Run；"
        "把同一个编号用于**不同内容**会返回 409 AGENT_IDEMPOTENCY_CONFLICT。"
        "`action` 与 `context` 的组合必须落在允许矩阵内，否则 422。"
        "第一版每个会话同时只允许一个未结束的 Run。"
    ),
    responses={
        202: {"description": "已受理，返回 Run（状态为 PENDING）"},
        **_AGENT_ERRORS,
        **_AUTH_ERRORS,
    },
    openapi_extra=RUN_REQUEST_BODY,
)
async def create_agent_run(
    session_id: uuid.UUID,
    request: Request,
    scope: RunScopeDep,
    user: CurrentUserDep,
    session: SessionDep,
    settings: SettingsDep,
) -> AgentRunSchema:
    # 前置检查已在 RunScopeDep 完成；这里才解析请求体（错误优先级固定）
    payload = await body_loader(request, AgentRunCreateRequest)()
    run, job = await service.create_run(
        session, user=user, scope=scope, request=payload, settings=settings
    )
    return _run_schema(run, job)


@agent_router.get(
    "/chat-sessions/{session_id}/runs",
    status_code=status.HTTP_200_OK,
    response_model=Page[AgentRunSchema],
    summary="会话内的 Agent Run 列表",
    description=(
        "按创建时间倒序返回本会话的 Run（新→旧），含状态、失败阶段与错误摘要。"
        "前端用它把 FAILED / CANCELLED 的原因显示在对应提问下面，"
        "因此刷新页面后用户仍然知道上一次为什么没有回答。只返回自己的会话。"
    ),
    responses={
        200: {"description": "Run 列表（最多 limit 条）"},
        404: _NOT_FOUND,
        422: {
            "model": ErrorResponse,
            "description": "limit 超出范围（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
)
async def list_agent_runs(
    session_id: uuid.UUID,
    scope: ReadSessionScopeDep,
    user: CurrentUserDep,
    session: SessionDep,
    limit: int = Query(default=RUN_LIST_DEFAULT_LIMIT, ge=1, le=RUN_LIST_MAX_LIMIT),
) -> Page[AgentRunSchema]:
    items, total = await service.list_runs(
        session, user=user, session_id=scope.session_id, limit=limit
    )
    grouped = await repo.list_steps_for_runs(
        session, run_ids=[run.id for run, _job in items]
    )
    return Page[AgentRunSchema](
        items=[
            _run_schema(run, job, steps=grouped.get(run.id, []))
            for run, job in items
        ],
        page=1,
        page_size=limit,
        total=total,
    )


@agent_router.get(
    "/chat-sessions/{session_id}/active-run",
    status_code=status.HTTP_200_OK,
    response_model=AgentActiveRunSchema,
    summary="会话中尚未结束的 Run",
    description=(
        "打开会话时先调用它：`run` 非空则按正常节奏恢复轮询，"
        "因此刷新页面不会让「正在生成」的提示消失，也不会丢掉已经发出的提问。"
        "没有进行中的 Run 时返回 200 且 `run` 为 null——这是常规状态，不是错误。"
    ),
    responses={
        200: {"description": "进行中的 Run，或 null"},
        404: _NOT_FOUND,
        **_AUTH_ERRORS,
    },
)
async def get_active_agent_run(
    session_id: uuid.UUID,
    scope: ReadSessionScopeDep,
    user: CurrentUserDep,
    session: SessionDep,
) -> AgentActiveRunSchema:
    pair = await service.get_active_run(session, user=user, session_id=scope.session_id)
    if pair is None:
        return AgentActiveRunSchema(run=None)
    run, job = pair
    sources = await repo.list_sources(session, run.id)
    steps = await repo.list_steps(session, run.id)
    return AgentActiveRunSchema(run=_run_schema(run, job, sources, steps))


@agent_router.get(
    "/agent-runs/{run_id}",
    status_code=status.HTTP_200_OK,
    response_model=AgentRunSchema,
    summary="查询 Agent Run",
    description=(
        "仅 Run 的所有者可读。`status` / `progress` / `error` / `failure_stage` 来自"
        "关联任务；成功后会带上 `output_message_id`、`evidence_level` 与本次实际注入的来源列表。"
    ),
    responses={
        200: {"description": "Run 当前状态"},
        404: _AGENT_ERRORS[404],
        **_AUTH_ERRORS,
    },
)
async def get_agent_run(
    run_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> AgentRunSchema:
    run, job = await service.get_run(session, user=user, run_id=run_id)
    sources = await repo.list_sources(session, run_id)
    steps = await repo.list_steps(session, run_id)
    return _run_schema(run, job, sources, steps)


@agent_router.post(
    "/agent-runs/{run_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AgentRunSchema,
    summary="取消 Agent Run",
    description=(
        "把尚未结束的任务置为 `CANCELLED`。正在执行模型调用的 Worker 会因为"
        "状态不再是 `RUNNING` 而放弃回写，因此不会出现取消后仍产生助手消息的情况。"
        "已结束的 Run 返回 409 AGENT_RUN_NOT_CANCELLABLE。请求体可省略或传 {}。"
    ),
    responses={
        202: {"description": "已取消"},
        404: _AGENT_ERRORS[404],
        409: {
            "model": ErrorResponse,
            "description": "任务已结束（AGENT_RUN_NOT_CANCELLABLE）",
        },
        422: {
            "model": ErrorResponse,
            "description": "请求体为显式 null 或含未声明字段（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
    openapi_extra=EMPTY_OBJECT_REQUEST_BODY,
)
async def cancel_agent_run(
    run_id: uuid.UUID,
    request: Request,
    cancellable_id: CancellableRunDep,
    user: CurrentUserDep,
    session: SessionDep,
) -> AgentRunSchema:
    # 前置检查（可见性 + 是否可取消）已在依赖里完成，与契约的错误优先级一致；
    # 请求体校验在它们之后，所以这里才真正解析 body（评审文档「一、#12」）。
    await validate_empty_object_body(request)
    run, job = await service.cancel_run(
        session, user=user, run_id=cancellable_id
    )
    sources = await repo.list_sources(session, run_id)
    steps = await repo.list_steps(session, run_id)
    return _run_schema(run, job, sources, steps)


__all__ = [
    "AGENT_REQUEST_MODELS",
    "ErrorCode",
    "agent_router",
]
