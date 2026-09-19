"""Auth 业务规则与事务边界。

事务边界在本层：repository 只暂存对象，service 决定何时 ``commit``。
这样"失败也要记一笔限流数据""刷新不改动会话到期时间"之类的规则才有明确的落点。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    EmailTakenError,
    InvalidCredentialsError,
    ResourceNotFoundError,
    TokenExpiredError,
    TooManyAttemptsError,
)
from app.core.time import utc_now
from app.modules.auth import repository as repo
from app.modules.auth import security
from app.modules.auth.models import AuthSession, User
from app.modules.auth.schemas import (
    LoginRequest,
    RegisterRequest,
    UserPublicSummary,
)

logger = logging.getLogger("app.auth")


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    """一次登录签发的一对令牌。"""

    access_token: str
    refresh_token: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class RefreshedAccessToken:
    """刷新得到的新 Access Token。"""

    access_token: str
    expires_in: int


async def register(session: AsyncSession, payload: RegisterRequest) -> User:
    """创建账号。

    刻意 **不做** "先查询再插入"：并发注册时两个请求都会通过查询，
    唯一防线必须是 ``users.email_normalized`` 上的唯一约束，
    本函数只负责把约束冲突翻译成契约错误码 ``AUTH_EMAIL_TAKEN``。
    """
    email = str(payload.email)
    email_normalized = security.normalize_email(email)
    user = repo.add_user(
        session,
        email=email,
        email_normalized=email_normalized,
        password_hash=security.hash_password(payload.password),
        display_name=payload.display_name,
        role=payload.role,
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if await _email_already_registered(session, email_normalized):
            raise EmailTakenError() from exc
        raise
    return user


async def _email_already_registered(
    session: AsyncSession, email_normalized: str
) -> bool:
    """判断完整性错误是否由邮箱重复引起。

    刻意不去解析驱动的错误文本：PostgreSQL 的 asyncpg/psycopg2 会带出约束名，
    而 SQLite 只报 ``UNIQUE constraint failed: users.email_normalized``。
    回查一次"该比较值是否已存在"是跨方言都成立的判据；错误路径上多一次
    SELECT 可以接受。其他完整性错误（例如未来的非空约束）会原样抛出。
    """
    return await repo.get_user_by_normalized_email(session, email_normalized) is not None


async def login(
    session: AsyncSession, settings: Settings, payload: LoginRequest
) -> tuple[User, IssuedTokens]:
    """登录并按契约签发两个令牌。

    顺序很重要：先做限流判断，再做（恒定开销的）密码校验，
    失败时 **先落库失败记录再抛错**，否则限流形同虚设。
    """
    email_normalized = security.normalize_email(str(payload.email))
    now = utc_now()
    window = timedelta(seconds=settings.login_rate_limit_window_seconds)
    window_start = now - window

    failures = await repo.count_login_failures_since(
        session, email_normalized, window_start
    )
    if failures >= settings.login_rate_limit_max_failures:
        oldest = await repo.get_oldest_login_failure_since(
            session, email_normalized, window_start
        )
        raise TooManyAttemptsError(
            details={
                "retry_after_seconds": _retry_after_seconds(oldest, now, window),
            }
        )

    user = await repo.get_user_by_normalized_email(session, email_normalized)
    password_ok = security.verify_password_constant_time(
        user.password_hash if user is not None else None, payload.password
    )

    if user is None or not password_ok or not user.is_active:
        # 账号不存在、密码错误、账号停用：对外都是同一条 AUTH_INVALID_CREDENTIALS，
        # 并且都会计入限流，避免通过响应差异或限流结果反推账号是否存在。
        repo.add_login_failure(
            session, email_normalized=email_normalized, failed_at=now
        )
        await session.commit()
        raise InvalidCredentialsError()

    await repo.delete_login_failures(session, email_normalized)
    issued = _issue_tokens(session, settings, user, now=now)
    await session.commit()
    return user, issued


async def get_public_user_summaries(
    session: AsyncSession, user_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, UserPublicSummary]:
    """批量取公开用户摘要（ID、姓名、平台角色，不含邮箱）。

    供课程等模块展示成员列表：跨模块只允许走这个入口，
    不允许直接查询 ``users`` 表（见 ``docs/architecture.md`` 第 4 节）。
    """
    return await repo.get_users_public_summaries(session, user_ids)


async def refresh_access_token(
    session: AsyncSession, settings: Settings, refresh_token: str
) -> RefreshedAccessToken:
    """用 Refresh Token 换取新的 Access Token。

    契约 2.4：成功响应只包含新的 Access Token；不轮换 Refresh Token，
    也不延长其有效期，因此这里 **不修改** 会话记录。
    """
    auth_session = await _load_valid_session(session, refresh_token)
    user = await repo.get_user_by_id(session, auth_session.user_id)
    if user is None or not user.is_active:
        raise TokenExpiredError()

    access_token, expires_in = security.create_access_token(
        user_id=user.id, role=user.role.value, settings=settings
    )
    return RefreshedAccessToken(access_token=access_token, expires_in=expires_in)


async def logout(
    session: AsyncSession, *, current_user: User, refresh_token: str
) -> None:
    """撤销当前用户的指定会话（契约 2.5）。

    - 只撤销请求体里那一个会话，不影响该用户的其他会话；
    - 会话不存在或属于其他用户时返回同一种结果（``RESOURCE_NOT_FOUND``），
      既避免枚举他人会话，也保证绝不撤销别人的会话；
    - 重复注销是幂等的。
    """
    token_hash = security.hash_refresh_token(refresh_token)
    auth_session = await repo.get_auth_session_by_refresh_hash(session, token_hash)
    if auth_session is None or auth_session.user_id != current_user.id:
        raise ResourceNotFoundError()

    if auth_session.revoked_at is None:
        auth_session.revoked_at = utc_now()
        await session.commit()


async def _load_valid_session(
    session: AsyncSession, refresh_token: str
) -> AuthSession:
    """按 Refresh Token 哈希取出仍可用的会话。

    只接受同时满足「存在」「未撤销」「未过期」的会话；三种失败对外一致，
    客户端统一走"清除本地令牌并跳转登录"的流程。
    """
    token_hash = security.hash_refresh_token(refresh_token)
    auth_session = await repo.get_auth_session_by_refresh_hash(session, token_hash)
    if (
        auth_session is None
        or auth_session.revoked_at is not None
        or auth_session.expires_at <= utc_now()
    ):
        raise TokenExpiredError()
    return auth_session


def _issue_tokens(
    session: AsyncSession, settings: Settings, user: User, *, now: datetime
) -> IssuedTokens:
    """签发 Access Token 并登记新的 Refresh Token 会话。"""
    access_token, expires_in = security.create_access_token(
        user_id=user.id, role=user.role.value, settings=settings, now=now
    )
    refresh_token = security.generate_refresh_token()
    repo.add_auth_session(
        session,
        user_id=user.id,
        refresh_token_hash=security.hash_refresh_token(refresh_token),
        # 契约 2.2/2.3：自登录签发起 7 天，刷新时不更新
        expires_at=now + timedelta(seconds=settings.refresh_token_expire_seconds),
        issued_at=now,
    )
    return IssuedTokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )


def _retry_after_seconds(
    oldest_failure_at: datetime | None, now: datetime, window: timedelta
) -> int:
    """最早一次失败滚出窗口所需秒数；无法计算时退化为整个窗口长度。"""
    if oldest_failure_at is None:
        return max(1, int(window.total_seconds()))
    remaining = (oldest_failure_at + window) - now
    return max(1, int(remaining.total_seconds()))
