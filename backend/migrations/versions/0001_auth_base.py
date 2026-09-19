"""auth 基础表：users、auth_sessions、login_attempts

Revision ID: 0001_auth_base
Revises:
Create Date: 2026-09-19 13:40:00.000000

对应 ``docs/api-contract.md`` 第 2 节：

- ``users``：账号。``email_normalized`` 建唯一约束，由数据库兜住并发注册；
  密码列只放 Argon2id 哈希。
- ``auth_sessions``：Refresh Token 会话，只存 sha256 哈希，
  含签发时间（``created_at``）、到期时间（``expires_at``）与撤销时间（``revoked_at``）。
- ``login_attempts``：登录失败记录，按规范化邮箱做窗口限流。
  故意不设指向 ``users`` 的外键，使不存在的邮箱同样被计数。

全部时间列为 ``TIMESTAMP WITH TIME ZONE``，应用侧统一写入 UTC。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_auth_base"
down_revision: str | None = None
branch_labels = None
depends_on = None

#: 邮箱列长度，与 app.modules.auth.models.EMAIL_MAX_LENGTH 保持一致
EMAIL_MAX_LENGTH = 320


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=EMAIL_MAX_LENGTH), nullable=False),
        sa.Column(
            "email_normalized", sa.String(length=EMAIL_MAX_LENGTH), nullable=False
        ),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column(
            "role",
            sa.Enum("TEACHER", "STUDENT", name="user_role", native_enum=True),
            nullable=False,
        ),
        sa.Column(
            "is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        # 并发注册的唯一防线：先查后插无法覆盖并发窗口
        sa.UniqueConstraint("email_normalized", name="uq_users_email_normalized"),
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # 只保存哈希，Token 明文只在登录 / 刷新响应中出现一次
        sa.Column("refresh_token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_auth_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_auth_sessions"),
        sa.UniqueConstraint(
            "refresh_token_hash", name="uq_auth_sessions_refresh_token_hash"
        ),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])

    op.create_table(
        "login_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        # 不设外键：不存在的邮箱也要参与限流
        sa.Column(
            "email_normalized", sa.String(length=EMAIL_MAX_LENGTH), nullable=False
        ),
        sa.Column(
            "failed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_login_attempts"),
    )
    op.create_index(
        "ix_login_attempts_email_normalized_failed_at",
        "login_attempts",
        ["email_normalized", "failed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_login_attempts_email_normalized_failed_at", table_name="login_attempts"
    )
    op.drop_table("login_attempts")

    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")

    op.drop_table("users")
    # 枚举类型不会随表一起删除，必须显式清理
    sa.Enum(name="user_role").drop(op.get_bind(), checkfirst=True)
