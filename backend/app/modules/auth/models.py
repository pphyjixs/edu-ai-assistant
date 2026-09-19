"""Auth 模块 ORM 模型。

三张表对应 ``docs/api-contract.md`` 第 2 节的三块数据：

- ``users``：账号。原邮箱用于展示，比较值 ``email_normalized`` 建唯一约束，
  由数据库兜住并发注册（先查后插会漏掉并发窗口）。
- ``auth_sessions``：Refresh Token 会话。**只保存哈希**，不保存 Token 明文；
  字段与契约 2.3 节逐条对应。
- ``login_attempts``：登录失败记录，按规范化邮箱统计限流。
  刻意不设指向 ``users`` 的外键——不存在的邮箱同样要被限流，否则可通过
  是否被限流反推账号是否存在。

所有时间列都使用 :class:`app.db.types.UtcDateTime`，读写两端均为 UTC。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, UniqueConstraint, func, true
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.core.time import utc_now
from app.db.base import Base
from app.db.types import UtcDateTime

#: 邮箱列长度：RFC 5321 允许的最大长度（本地部分 64 + ``@`` + 域名 255）
EMAIL_MAX_LENGTH = 320

#: 姓名列长度
DISPLAY_NAME_MAX_LENGTH = 64

#: Argon2id 哈希字符串长度，预留算法参数升级空间
PASSWORD_HASH_MAX_LENGTH = 255

#: Refresh Token 哈希列长度（sha256 十六进制摘要固定 64 字符）
REFRESH_TOKEN_HASH_LENGTH = 64


class UserRole(str, enum.Enum):
    """平台角色。

    平台角色只是默认权限，课程资源仍必须校验成员关系
    （见 ``docs/modules.md`` 第 1 节）。
    """

    TEACHER = "TEACHER"
    STUDENT = "STUDENT"


class User(Base):
    """账号。"""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    #: 注册时提交的原始邮箱，用于展示
    email: Mapped[str] = mapped_column(String(EMAIL_MAX_LENGTH), nullable=False)

    #: 比较值：仅域名部分小写化，本地部分保留大小写。
    #: 唯一约束建在本列上，冲突时由数据库返回 IntegrityError。
    email_normalized: Mapped[str] = mapped_column(
        String(EMAIL_MAX_LENGTH), nullable=False
    )

    #: 仅为 Argon2id 哈希，禁止写入明文或可逆编码
    password_hash: Mapped[str] = mapped_column(
        String(PASSWORD_HASH_MAX_LENGTH), nullable=False
    )

    display_name: Mapped[str] = mapped_column(
        String(DISPLAY_NAME_MAX_LENGTH), nullable=False
    )

    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role", native_enum=True), nullable=False
    )

    #: 账号状态。第一版注册后即可用，不使用该字段做审批
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("email_normalized", name="uq_users_email_normalized"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<User id={self.id} role={self.role.value}>"


class AuthSession(Base):
    """Refresh Token 会话记录（契约 2.3 节）。"""

    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_auth_sessions_user_id_users"),
        nullable=False,
    )

    #: Refresh Token 的 sha256 摘要；Token 本身是密码学安全随机值，
    #: 且只通过登录响应下发一次，因此哈希即可，无需加盐慢哈希。
    refresh_token_hash: Mapped[str] = mapped_column(
        String(REFRESH_TOKEN_HASH_LENGTH), nullable=False
    )

    #: 登录签发时间 + 7 天；刷新时不更新
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    #: 撤销时间；未撤销时为 NULL
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    #: 会话创建（登录签发）时间
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="sessions")

    __table_args__ = (
        UniqueConstraint(
            "refresh_token_hash", name="uq_auth_sessions_refresh_token_hash"
        ),
        Index("ix_auth_sessions_user_id", "user_id"),
    )


class LoginAttempt(Base):
    """一次登录失败记录，用于按规范化邮箱限流。"""

    __tablename__ = "login_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    #: 与 users.email_normalized 使用同一套规范化规则，但不设外键：
    #: 不存在的邮箱也要计数，否则限流结果会泄露账号是否存在。
    email_normalized: Mapped[str] = mapped_column(
        String(EMAIL_MAX_LENGTH), nullable=False
    )

    failed_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        # 统计窗口内的失败次数是限流的热路径，按 (邮箱, 时间) 建复合索引
        Index("ix_login_attempts_email_normalized_failed_at", "email_normalized", "failed_at"),
    )
