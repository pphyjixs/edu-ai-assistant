"""课程问答：chat 四表、pg_trgm 扩展与片段检索索引

Revision ID: 0008_chat_qa
Revises: 0007_material_chunks
Create Date: 2026-09-21 11:20:00.000000

对应 ``docs/api-contract.md`` 第 6 节：

- ``pg_trgm`` 扩展 + ``material_chunks.content`` 的 GIN trigram 索引：
  首版问答使用 PostgreSQL 文本检索（没有嵌入模型与 pgvector）；
- ``chat_sessions``：会话与所有者；``version`` 是服务端内部的乐观并发版本
  （并发发送只有一个成功，其余 409 CHAT_CONFLICT）；
- ``chat_messages``：一问一答同事务写入；``grounded`` 仅助手消息有值；
- ``chat_message_citations``：引用快照（冗余资料名、章节标题与摘录），
  **不设外键**，保证资料删除/重新解析后历史对话仍可完整回读；
- ``chat_generation_attempts``：模型名称、提示词版本、耗时与安全失败摘要。

``pg_trgm`` 需要数据库超级用户权限；部署环境必须预先启用该扩展
（见 ``docs/deployment-vercel.md``）。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008_chat_qa"
down_revision: str | None = "0007_material_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 文本检索依赖 pg_trgm（相似度函数与 GIN 索引的操作符族）
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_index(
        "ix_material_chunks_content_trgm",
        "material_chunks",
        ["content"],
        postgresql_using="gin",
        postgresql_ops={"content": "gin_trgm_ops"},
    )

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            name="fk_chat_sessions_course_id_courses",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_chat_sessions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_sessions"),
    )
    op.create_index(
        "ix_chat_sessions_course_user_last_message",
        "chat_sessions",
        ["course_id", "user_id", "last_message_at", "id"],
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role",
            sa.Enum("USER", "ASSISTANT", name="chat_message_role", native_enum=True),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("grounded", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.id"],
            name="fk_chat_messages_session_id_chat_sessions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_messages"),
    )
    op.create_index(
        "ix_chat_messages_session_created_id",
        "chat_messages",
        ["session_id", "created_at", "id"],
    )

    op.create_table(
        "chat_message_citations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        # 引用快照：不加外键，资料删除后历史对话仍可回读
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("material_name", sa.String(length=255), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=True),
        sa.Column("section_title", sa.String(length=255), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("location_start", sa.Integer(), nullable=False),
        sa.Column("location_end", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["chat_messages.id"],
            name="fk_chat_message_citations_message_id_chat_messages",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_message_citations"),
    )
    op.create_index(
        "ix_chat_message_citations_message_id", "chat_message_citations", ["message_id"]
    )

    op.create_table(
        "chat_generation_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "SUCCEEDED",
                "FAILED",
                name="chat_attempt_status",
                native_enum=True,
            ),
            nullable=False,
        ),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("retrieved_count", sa.Integer(), nullable=False),
        sa.Column("grounded", sa.Boolean(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.id"],
            name="fk_chat_generation_attempts_session_id_chat_sessions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_chat_generation_attempts"),
    )
    op.create_index(
        "ix_chat_generation_attempts_session_created",
        "chat_generation_attempts",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chat_generation_attempts_session_created",
        table_name="chat_generation_attempts",
    )
    op.drop_table("chat_generation_attempts")
    op.drop_index(
        "ix_chat_message_citations_message_id", table_name="chat_message_citations"
    )
    op.drop_table("chat_message_citations")
    op.drop_index("ix_chat_messages_session_created_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index(
        "ix_chat_sessions_course_user_last_message", table_name="chat_sessions"
    )
    op.drop_table("chat_sessions")
    sa.Enum(name="chat_attempt_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="chat_message_role").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_material_chunks_content_trgm", table_name="material_chunks")
    # 扩展不随迁移回滚删除：同一数据库的其他对象可能仍在使用
