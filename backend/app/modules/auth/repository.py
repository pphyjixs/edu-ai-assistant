"""Auth 数据访问层。

只做查询与写入，不含业务判断，不提交事务——事务边界属于 ``service.py``
（见 ``docs/architecture.md`` 第 4 节）。
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import AuthSession, LoginAttempt, User, UserRole
from app.modules.auth.schemas import UserPublicSummary


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    """按主键取用户。"""
    return await session.get(User, user_id)


async def get_user_by_normalized_email(
    session: AsyncSession, email_normalized: str
) -> User | None:
    """按邮箱比较值取用户；注册与登录使用同一套比较值。"""
    result = await session.execute(
        select(User).where(User.email_normalized == email_normalized)
    )
    return result.scalar_one_or_none()


def add_user(
    session: AsyncSession,
    *,
    email: str,
    email_normalized: str,
    password_hash: str,
    display_name: str,
    role: UserRole,
) -> User:
    """暂存新用户；唯一约束冲突在提交时由数据库抛出。"""
    user = User(
        email=email,
        email_normalized=email_normalized,
        password_hash=password_hash,
        display_name=display_name,
        role=role,
    )
    session.add(user)
    return user


def add_auth_session(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    refresh_token_hash: str,
    expires_at: datetime,
    issued_at: datetime,
) -> AuthSession:
    """暂存一条 Refresh Token 会话记录。"""
    auth_session = AuthSession(
        user_id=user_id,
        refresh_token_hash=refresh_token_hash,
        expires_at=expires_at,
        created_at=issued_at,
    )
    session.add(auth_session)
    return auth_session


async def get_auth_session_by_refresh_hash(
    session: AsyncSession, refresh_token_hash: str
) -> AuthSession | None:
    """按 Refresh Token 哈希取会话；不同用户的会话不会被误命中。"""
    result = await session.execute(
        select(AuthSession).where(AuthSession.refresh_token_hash == refresh_token_hash)
    )
    return result.scalar_one_or_none()


async def count_login_failures_since(
    session: AsyncSession, email_normalized: str, since: datetime
) -> int:
    """统计窗口内该规范化邮箱的失败次数。"""
    result = await session.execute(
        select(func.count())
        .select_from(LoginAttempt)
        .where(
            LoginAttempt.email_normalized == email_normalized,
            LoginAttempt.failed_at >= since,
        )
    )
    return int(result.scalar_one())


async def get_oldest_login_failure_since(
    session: AsyncSession, email_normalized: str, since: datetime
) -> datetime | None:
    """取窗口内最早的失败时间，用于计算还要等多久才能重试。"""
    result = await session.execute(
        select(func.min(LoginAttempt.failed_at)).where(
            LoginAttempt.email_normalized == email_normalized,
            LoginAttempt.failed_at >= since,
        )
    )
    return result.scalar_one_or_none()


def add_login_failure(
    session: AsyncSession, *, email_normalized: str, failed_at: datetime
) -> LoginAttempt:
    """记录一次登录失败。"""
    attempt = LoginAttempt(email_normalized=email_normalized, failed_at=failed_at)
    session.add(attempt)
    return attempt


async def get_users_public_summaries(
    session: AsyncSession, user_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, UserPublicSummary]:
    """批量取公开用户摘要，供其他模块展示成员等信息。

    返回 ``{user_id: 摘要}``；不存在的 ID 直接缺席，由调用方决定降级显示。
    """
    wanted = set(user_ids)
    if not wanted:
        return {}

    result = await session.execute(select(User).where(User.id.in_(wanted)))
    return {
        user.id: UserPublicSummary.model_validate(user)
        for user in result.scalars().all()
    }


async def delete_login_failures(
    session: AsyncSession, email_normalized: str
) -> None:
    """登录成功后清空该邮箱的失败记录，避免历史失败拖累后续登录。"""
    await session.execute(
        delete(LoginAttempt).where(LoginAttempt.email_normalized == email_normalized)
    )
