"""Agent Run 的 HTTP 接口（``docs/local-development-agent-backend.md`` 第 6.2 节）。

保留既有会话与历史查询接口不变，这里只新增异步 Run 的三条路径：

- ``POST /chat-sessions/{session_id}/runs`` → ``202``，**数百毫秒内返回**，不等模型；
- ``GET /agent-runs/{run_id}`` → ``200``，前端按 30s/2s、之后/5s 的节奏轮询；
- ``POST /agent-runs/{run_id}/cancel`` → ``202``。

错误优先级与其他模块一致：认证 → 会话可见 → 课程成员 → 归档 → 上下文可见性
→ 请求体结构与字段。因此越权请求返回 404/403/409，而不是 422。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, status

from app.core.deps import SettingsDep
from app.core.errors import ErrorCode
from app.core.request_body import EMPTY_OBJECT_REQUEST_BODY, body_loader
from app.core.schemas import ErrorResponse
from app.db.session import SessionDep
from app.modules.agent import repository as repo
from app.modules.agent import service
from app.modules.agent.deps import RunScopeDep
from app.modules.agent.models import AgentRun
from app.modules.agent.schemas import (
    AgentRunCreateRequest,
    AgentRunSchema,
    AgentRunSourceSchema,
)
from app.modules.auth.permissions import CurrentUserDep
from app.modules.jobs.models import Job

agent_router = APIRouter(tags=["agent"])

#: 用原始 Request 手工解析、需要显式补进 OpenAPI 组件的请求模型
AGENT_REQUEST_MODELS = (AgentRunCreateRequest,)

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

_AGENT_ERRORS: dict[int | str, dict] = {
    403: {
        "model": ErrorResponse,
        "description": "课程已归档时为 COURSE_ARCHIVED；上下文不可用见 409/422",
    },
    404: {
        "model": ErrorResponse,
        "description": "会话不存在、不是所有者，或目标对象不属于本课程/不可见（RESOURCE_NOT_FOUND）",
    },
    409: {
        "model": ErrorResponse,
        "description": (
            "AGENT_RUN_IN_PROGRESS（该会话已有未结束的 Run）、"
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


def _run_schema(
    run: AgentRun, job: Job, sources: list | None = None
) -> AgentRunSchema:
    """把「Run 行 + 任务行」组装成响应；状态与进度来自任务行。"""
    return AgentRunSchema(
        id=run.id,
        session_id=run.session_id,
        action=run.action,
        status=job.status,
        progress=job.progress,
        input_message_id=run.input_message_id,
        output_message_id=run.output_message_id,
        error=job.error,
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
    )


@agent_router.post(
    "/chat-sessions/{session_id}/runs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AgentRunSchema,
    summary="创建 Agent Run",
    description=(
        "在指定会话中创建一个异步 Agent Run：同一事务写入用户消息、"
        "`agent_runs` 与 `AGENT_RUN` 任务后立即返回 202，模型调用由独立 Worker 完成。"
        "`client_request_id` 在用户范围内唯一，网络重试会返回同一个 Run。"
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
    "/agent-runs/{run_id}",
    status_code=status.HTTP_200_OK,
    response_model=AgentRunSchema,
    summary="查询 Agent Run",
    description=(
        "仅 Run 的所有者可读。`status` / `progress` / `error` 来自关联任务；"
        "成功后会带上 `output_message_id` 与本次实际注入的来源列表。"
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
    return _run_schema(run, job, sources)


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
    user: CurrentUserDep,
    session: SessionDep,
) -> AgentRunSchema:
    run, job = await service.cancel_run(session, user=user, run_id=run_id)
    sources = await repo.list_sources(session, run_id)
    return _run_schema(run, job, sources)


__all__ = [
    "AGENT_REQUEST_MODELS",
    "ErrorCode",
    "agent_router",
]
