"""Chat HTTP 路由（``docs/api-contract.md`` 第 6 节的四个接口）。

只做协议转换与依赖注入，业务规则全部在 :mod:`app.modules.chat.service`。

``get_ai_client_factory`` 是**可替换的 AI 适配层入口**：生产环境返回
``None``（由适配层自行构造 ``httpx`` 客户端），测试通过
``app.dependency_overrides`` 注入假模型服务（``httpx.MockTransport``）。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from app.core.deps import SettingsDep
from app.core.pagination import Page, PaginationDep
from app.core.request_body import (
    EMPTY_OBJECT_REQUEST_BODY,
    validate_empty_object_body,
)
from app.core.schemas import ErrorResponse
from app.db.session import SessionDep
from app.modules.auth.permissions import CurrentUserDep
from app.modules.chat import repository, service
from app.modules.chat.schemas import (
    Citation,
    ChatMessageSchema,
    ChatQuestionRequest,
    ChatSessionSchema,
)

chat_router = APIRouter(tags=["chat"])

#: 生成 AI 客户端的工厂签名（测试注入假模型服务）
AiClientFactory = Callable[[], object]

_AUTH_ERRORS: dict[int | str, dict] = {
    401: {
        "model": ErrorResponse,
        "description": "Access Token 缺失、无效或已过期（AUTH_TOKEN_EXPIRED）",
    }
}

_NOT_FOUND_COURSE = {
    "model": ErrorResponse,
    "description": "课程不存在，或当前用户不是课程成员（RESOURCE_NOT_FOUND）",
}

_NOT_FOUND_SESSION = {
    "model": ErrorResponse,
    "description": "会话不存在，或当前用户不是会话所有者（RESOURCE_NOT_FOUND）",
}

#: 创建会话没有请求字段：OpenAPI 声明为可选对象、请求体手工校验
#: （区分「省略」与「显式 null」，见 :mod:`app.core.request_body`）。


def get_ai_client_factory() -> AiClientFactory | None:
    """返回用于构造模型 HTTP 客户端的工厂（默认 ``None`` = 适配层自建）。"""
    return None


AiClientFactoryDep = Annotated[
    AiClientFactory | None, Depends(get_ai_client_factory)
]


@chat_router.post(
    "/courses/{course_id}/chat-sessions",
    status_code=status.HTTP_201_CREATED,
    response_model=ChatSessionSchema,
    summary="创建问答会话",
    description=(
        "课程成员（教师或学生）均可创建；创建者即会话所有者。"
        "归档课程返回 409 COURSE_ARCHIVED。课程不存在或非成员统一 404。"
        "请求体可省略、也可传空对象 {}；显式 null 或含未声明字段返回 422。"
    ),
    responses={
        201: {"description": "创建成功"},
        404: _NOT_FOUND_COURSE,
        409: {
            "model": ErrorResponse,
            "description": "课程已归档，不能创建会话（COURSE_ARCHIVED）",
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "请求体为显式 null，或含未声明字段（VALIDATION_ERROR）"
            ),
        },
        **_AUTH_ERRORS,
    },
    # 没有请求字段：手工校验区分「省略请求体」与「显式 null」
    openapi_extra=EMPTY_OBJECT_REQUEST_BODY,
)
async def create_chat_session(
    course_id: uuid.UUID,
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
) -> ChatSessionSchema:
    await validate_empty_object_body(request)
    chat_session = await service.create_session(
        session, user=user, course_id=course_id
    )
    return ChatSessionSchema.model_validate(chat_session)


@chat_router.get(
    "/courses/{course_id}/chat-sessions",
    status_code=status.HTTP_200_OK,
    response_model=Page[ChatSessionSchema],
    summary="我的问答会话列表",
    description=(
        "只返回当前用户在该课程创建的会话，按最近消息时间倒序；"
        "归档课程的列表仍返回 200。课程不存在或非成员统一 404。"
    ),
    responses={
        404: _NOT_FOUND_COURSE,
        422: {
            "model": ErrorResponse,
            "description": "分页参数不合法（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
)
async def list_chat_sessions(
    course_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    pagination: PaginationDep,
) -> Page[ChatSessionSchema]:
    sessions, total = await service.list_sessions(
        session, user=user, course_id=course_id, pagination=pagination
    )
    return Page[ChatSessionSchema](
        items=[ChatSessionSchema.model_validate(item) for item in sessions],
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )


@chat_router.get(
    "/chat-sessions/{session_id}",
    status_code=status.HTTP_200_OK,
    response_model=ChatSessionSchema,
    summary="会话详情",
    description=(
        "仅会话所有者可读；不是所有者与会话不存在统一 404。"
        "首页中央会话页刷新时只有 sessionId，用响应的 course_id 恢复课程上下文，"
        "不依赖 URL 或本地存储里的课程 ID。归档课程的本人会话仍可读。"
    ),
    responses={
        404: _NOT_FOUND_SESSION,
        **_AUTH_ERRORS,
    },
)
async def get_chat_session(
    session_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> ChatSessionSchema:
    chat_session = await service.get_session(
        session, user=user, session_id=session_id
    )
    return ChatSessionSchema.model_validate(chat_session)


@chat_router.get(
    "/chat-sessions/{session_id}/messages",
    status_code=status.HTTP_200_OK,
    response_model=Page[ChatMessageSchema],
    summary="会话消息",
    description=(
        "仅会话所有者可读，按时间升序返回一问一答及其引用；"
        "不是所有者与会话不存在统一 404。归档课程的本人会话仍可读。"
    ),
    responses={
        404: _NOT_FOUND_SESSION,
        422: {
            "model": ErrorResponse,
            "description": "分页参数不合法（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
)
async def list_chat_messages(
    session_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    pagination: PaginationDep,
) -> Page[ChatMessageSchema]:
    messages, total = await service.list_messages(
        session, user=user, session_id=session_id, pagination=pagination
    )
    items = []
    for message, citations in messages:
        schema = ChatMessageSchema.model_validate(message)
        schema.citations = [Citation.model_validate(item) for item in citations]
        items.append(schema)
    return Page[ChatMessageSchema](
        items=items,
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )


@chat_router.post(
    "/chat-sessions/{session_id}/messages",
    status_code=status.HTTP_201_CREATED,
    response_model=ChatMessageSchema,
    summary="发送问题",
    description=(
        "仅会话所有者可发送。回答只依据本课程中未删除、READY 资料的原文片段；"
        "没有依据时返回固定文案、grounded=false 与空引用（仍是 201）。"
        "生成期间会话被并发修改返回 409 CHAT_CONFLICT（不写入任何消息）；"
        "模型未配置返回 503 SERVICE_UNAVAILABLE；模型调用或输出失败返回 "
        "502 AI_JOB_FAILED（不写入任何消息）。"
    ),
    responses={
        201: {"description": "返回助手消息（可能无依据）"},
        404: _NOT_FOUND_SESSION,
        409: {
            "model": ErrorResponse,
            "description": (
                "课程已归档（COURSE_ARCHIVED）或会话被并发修改（CHAT_CONFLICT）"
            ),
        },
        502: {
            "model": ErrorResponse,
            "description": "模型调用或输出失败（AI_JOB_FAILED，不落库消息）",
        },
        503: {
            "model": ErrorResponse,
            "description": "模型端点或模型名未配置（SERVICE_UNAVAILABLE）",
        },
        422: {
            "model": ErrorResponse,
            "description": "问题内容缺失、为空、超长或含未声明字段（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
)
async def send_chat_question(
    session_id: uuid.UUID,
    payload: ChatQuestionRequest,
    user: CurrentUserDep,
    session: SessionDep,
    settings: SettingsDep,
    ai_client_factory: AiClientFactoryDep = None,
) -> ChatMessageSchema:
    message = await service.send_question(
        session,
        user=user,
        session_id=session_id,
        content=payload.content,
        settings=settings,
        ai_client_factory=ai_client_factory,
    )
    schema = ChatMessageSchema.model_validate(message)
    # 本次生成的引用随助手消息返回（与 6.4 读到的一致）
    grouped = await repository.list_citations_for_messages(
        session, message_ids=[message.id]
    )
    schema.citations = [
        Citation.model_validate(item) for item in grouped.get(message.id, [])
    ]
    return schema


__all__ = ["chat_router", "get_ai_client_factory"]
