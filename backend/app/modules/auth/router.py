"""Auth HTTP 路由。

只做协议转换与依赖注入，业务规则全部在 :mod:`app.modules.auth.service`
（见 ``docs/architecture.md`` 第 4 节）。路径、状态码与响应结构以
``docs/api-contract.md`` 第 2 节为准。
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.core.deps import SettingsDep
from app.core.schemas import ErrorResponse
from app.db.session import SessionDep
from app.modules.auth import service
from app.modules.auth.permissions import CurrentUserDep
from app.modules.auth.schemas import (
    LoginRequest,
    LoginResponse,
    LogoutRequest,
    RefreshRequest,
    RefreshResponse,
    RegisterRequest,
    UserProfile,
    UserSummary,
)

#: 认证相关接口：/auth/*
auth_router = APIRouter(prefix="/auth", tags=["auth"])

#: 当前用户资料：/users/me。
#: 契约把 /users/me 归在「认证接口」一节，因此暂时由本模块提供；
#: 后续 users 模块接手用户管理接口时再迁移，避免跨模块读用户表。
me_router = APIRouter(prefix="/users", tags=["users"])


@auth_router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserProfile,
    summary="注册教师或学生账户",
    description=(
        "第一版开放注册，无需邀请码或审批。并发注册由数据库唯一约束兜住，"
        "冲突时返回 409 AUTH_EMAIL_TAKEN。注册不返回令牌，注册完成后调用登录接口。"
    ),
    responses={
        201: {"description": "注册成功"},
        409: {
            "model": ErrorResponse,
            "description": "邮箱已被占用（AUTH_EMAIL_TAKEN）",
        },
        422: {
            "model": ErrorResponse,
            "description": "请求参数不合法（VALIDATION_ERROR）",
        },
    },
)
async def register(
    payload: RegisterRequest, session: SessionDep
) -> UserProfile:
    user = await service.register(session, payload)
    return UserProfile.model_validate(user)


@auth_router.post(
    "/login",
    response_model=LoginResponse,
    summary="登录",
    description=(
        "邮箱登录，返回 Access Token 与 Refresh Token。"
        "登录失败按规范化邮箱限流；账号不存在与密码错误返回同一条错误信息。"
    ),
    responses={
        200: {"description": "登录成功"},
        401: {
            "model": ErrorResponse,
            "description": "账号或密码错误（AUTH_INVALID_CREDENTIALS）",
        },
        422: {
            "model": ErrorResponse,
            "description": "请求参数不合法（VALIDATION_ERROR）",
        },
        429: {
            "model": ErrorResponse,
            "description": "失败次数过多，短时间内拒绝登录（AUTH_TOO_MANY_ATTEMPTS）",
        },
    },
)
async def login(
    payload: LoginRequest, session: SessionDep, settings: SettingsDep
) -> LoginResponse:
    user, issued = await service.login(session, settings, payload)
    return LoginResponse(
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        expires_in=issued.expires_in,
        user=UserSummary(
            id=user.id, display_name=user.display_name, role=user.role
        ),
    )


@auth_router.post(
    "/refresh",
    response_model=RefreshResponse,
    summary="刷新访问令牌",
    description=(
        "通过 JSON Body 提交 Refresh Token，不要求有效的 Access Token。"
        "只返回新的 Access Token，不轮换也不延长 Refresh Token 有效期。"
    ),
    responses={
        200: {"description": "刷新成功"},
        401: {
            "model": ErrorResponse,
            "description": "会话不存在、已过期或已撤销（AUTH_TOKEN_EXPIRED）",
        },
        422: {
            "model": ErrorResponse,
            "description": "请求参数不合法（VALIDATION_ERROR）",
        },
    },
)
async def refresh(
    payload: RefreshRequest, session: SessionDep, settings: SettingsDep
) -> RefreshResponse:
    refreshed = await service.refresh_access_token(
        session, settings, payload.refresh_token
    )
    return RefreshResponse(
        access_token=refreshed.access_token, expires_in=refreshed.expires_in
    )


@auth_router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    response_class=Response,
    summary="注销当前会话",
    description=(
        "需要有效的 Access Token，并通过 JSON Body 指定要撤销的 Refresh Token 会话。"
        "只撤销该会话；已签发的 Access Token 仍可能有效至自身过期。"
    ),
    responses={
        204: {"description": "注销成功，无响应体"},
        401: {
            "model": ErrorResponse,
            "description": "Access Token 缺失、无效或已过期（AUTH_TOKEN_EXPIRED）",
        },
        404: {
            "model": ErrorResponse,
            "description": "会话不存在或不属于当前用户（RESOURCE_NOT_FOUND）",
        },
        422: {
            "model": ErrorResponse,
            "description": "请求参数不合法（VALIDATION_ERROR）",
        },
    },
)
async def logout(
    payload: LogoutRequest,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> Response:
    await service.logout(
        session, current_user=current_user, refresh_token=payload.refresh_token
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@me_router.get(
    "/me",
    response_model=UserProfile,
    summary="当前用户资料",
    responses={
        200: {"description": "当前登录用户"},
        401: {
            "model": ErrorResponse,
            "description": "Access Token 缺失、无效或已过期（AUTH_TOKEN_EXPIRED）",
        },
    },
)
async def read_current_user(current_user: CurrentUserDep) -> UserProfile:
    return UserProfile.model_validate(current_user)
