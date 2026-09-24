"""Agent Run：`agent_runs`、`agent_run_sources` 两张表与相关原生枚举。

设计依据 ``docs/local-development-agent-backend.md`` 第 6.5 节：

- ``agent_runs`` **不复制**一套状态字段。状态、进度、错误、尝试次数、运行令牌
  与租约仍然只存在于 ``jobs``（``type=AGENT_RUN`` / ``resource_type=AGENT_RUN``），
  避免两处状态互相竞争。
- ``agent_run_sources`` 记录本次 Run **实际注入**的来源快照，便于回溯与审计。

``job_type`` / ``job_resource_type`` 是已存在的 PostgreSQL 原生枚举，本迁移用
``ALTER TYPE ... ADD VALUE IF NOT EXISTS`` 追加 ``AGENT_RUN``。

注意：PostgreSQL 不允许在**同一个事务**里新增枚举值后立即使用它。本迁移只新增
取值、不写入任何使用该取值的行，因此安全；真正使用发生在迁移提交之后。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011_agent_runs"
down_revision: str | None = "0010_assignments"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")

AGENT_RUN_ACTION = sa.Enum(
    "ASK",
    "SUMMARIZE_CONTEXT",
    "BREAK_DOWN_ASSIGNMENT",
    "CHECK_SUBMISSION",
    name="agent_run_action",
    native_enum=True,
)

AGENT_ENTITY_TYPE = sa.Enum(
    "COURSE",
    "MATERIAL",
    "MATERIAL_SECTION",
    "ASSIGNMENT",
    "SUBMISSION",
    "GRADE",
    name="agent_entity_type",
    native_enum=True,
)

AGENT_SOURCE_TYPE = sa.Enum(
    "COURSE",
    "MATERIAL_CHUNK",
    "MATERIAL_OUTLINE",
    "ASSIGNMENT",
    name="agent_source_type",
    native_enum=True,
)


def upgrade() -> None:
    # PostgreSQL 要求新增枚举值先提交，后续事务才能在约束或数据中引用它。
    # Alembic 默认会把一次 ``upgrade head`` 的多条迁移放进同一事务，因此这里
    # 显式使用 autocommit block，保证全新数据库可以一路升级到最新版本。
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE job_type ADD VALUE IF NOT EXISTS 'AGENT_RUN'")
        op.execute("ALTER TYPE job_resource_type ADD VALUE IF NOT EXISTS 'AGENT_RUN'")

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("action", AGENT_RUN_ACTION, nullable=False),
        # 本次提问保存下来的用户消息；Run 创建与它同事务
        sa.Column("input_message_id", sa.Uuid(), nullable=False),
        # 助手消息在 Run 成功后才回填
        sa.Column("output_message_id", sa.Uuid(), nullable=True),
        sa.Column("entity_type", AGENT_ENTITY_TYPE, nullable=True),
        sa.Column("entity_id", sa.Uuid(), nullable=True),
        sa.Column("section_id", sa.Uuid(), nullable=True),
        sa.Column("selected_text", sa.Text(), nullable=True),
        sa.Column("options", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        # 幂等键：同一用户重复提交返回同一个 Run（契约 6.3）
        sa.Column("client_request_id", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["chat_sessions.id"],
            name="fk_agent_runs_session_id_chat_sessions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_agent_runs_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["input_message_id"],
            ["chat_messages.id"],
            name="fk_agent_runs_input_message_id_chat_messages",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["output_message_id"],
            ["chat_messages.id"],
            name="fk_agent_runs_output_message_id_chat_messages",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
        sa.UniqueConstraint(
            "user_id", "client_request_id", name="uq_agent_runs_user_client_request_id"
        ),
    )
    op.create_index(
        "ix_agent_runs_session_created_id", "agent_runs", ["session_id", "created_at", "id"]
    )

    op.create_table(
        "agent_run_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("source_type", AGENT_SOURCE_TYPE, nullable=False),
        # 来源自身的 ID：课堂片段 id / 章节 id / 作业 id / 课程 id
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=True),
        sa.Column("chunk_id", sa.Uuid(), nullable=True),
        sa.Column("location_start", sa.Integer(), nullable=True),
        sa.Column("location_end", sa.Integer(), nullable=True),
        # 人类可读的来源标签（例如「资料《实验二要求.pdf》· 章节「二、实验要求」」），
        # 供前端展示与排错；snapshot 保存进提示词的受控文本
        sa.Column("label", sa.String(length=255), nullable=True),
        # 受控文本快照：进提示词的那一段（已按上下文字符预算截断）
        sa.Column("snapshot", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name="fk_agent_run_sources_run_id_agent_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["material_id"],
            ["materials.id"],
            name="fk_agent_run_sources_material_id_materials",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["material_chunks.id"],
            name="fk_agent_run_sources_chunk_id_material_chunks",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_run_sources"),
    )
    op.create_index("ix_agent_run_sources_run_order", "agent_run_sources", ["run_id", "order"])


def downgrade() -> None:
    op.drop_index("ix_agent_run_sources_run_order", table_name="agent_run_sources")
    op.drop_table("agent_run_sources")
    op.drop_index("ix_agent_runs_session_created_id", table_name="agent_runs")
    op.drop_table("agent_runs")

    AGENT_SOURCE_TYPE.drop(op.get_bind(), checkfirst=True)
    AGENT_ENTITY_TYPE.drop(op.get_bind(), checkfirst=True)
    AGENT_RUN_ACTION.drop(op.get_bind(), checkfirst=True)

    # PostgreSQL 不支持从枚举中删除取值，因此 `job_type` / `job_resource_type` 上的
    # `AGENT_RUN` 保留；回滚到上一版本时该取值不再被任何代码写入，属于安全残留。
