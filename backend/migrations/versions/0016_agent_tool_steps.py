"""Agent 工具执行审计：``agent_run_steps`` 表与 ``agent_runs.orchestrator_version``。

Revision ID: 0016_agent_tool_steps
Revises: 0015_dashboard_indexes
Create Date: 2026-09-25 10:00:00.000000

对应开发方案第 7.1 节（数据库与 API 契约）：

- ``agent_run_steps`` 记录每次工具调用与每次 Skill 加载的**终态**，
  因此「工具调用最终可审计率 100%」可以在库侧直接验证；
- ``UNIQUE(run_id, call_id)`` 是 Worker 重试的**幂等防线**：
  同一个模型 ``tool_call_id``（写工具为语义请求摘要）只执行一次；
- ``CHECK(jsonb_typeof(request_json) = 'object')`` 保证请求侧永远是 JSON 对象，
  避免把数组或字符串悄悄写进审计表；
- ``agent_runs.orchestrator_version`` 让成功与失败都能回溯到具体编排逻辑版本
  （``agent-tools-v1``）。老行为 NULL，表示"工具循环之前"的执行方式。

本迁移只新增表与列，不修改任何既有行；``orchestrator_version`` 保持可空，
历史 Run 的语义不变。降级按反序删除。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016_agent_tool_steps"
down_revision: str | None = "0015_dashboard_indexes"
branch_labels = None
depends_on = None

_NOW = sa.text("now()")

#: 与 ``app.modules.agent.models`` 中的常量逐字一致
_ORCHESTRATOR_VERSION_LEN = 64
_STEP_KIND_LEN = 16
_STEP_STATUS_LEN = 16
_STEP_CALL_ID_LEN = 128
_STEP_NAME_LEN = 64
_STEP_ERROR_CODE_LEN = 64


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column(
            "orchestrator_version",
            sa.String(length=_ORCHESTRATOR_VERSION_LEN),
            nullable=True,
        ),
    )

    op.create_table(
        "agent_run_steps",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=_STEP_KIND_LEN), nullable=False),
        sa.Column("call_id", sa.String(length=_STEP_CALL_ID_LEN), nullable=False),
        sa.Column("name", sa.String(length=_STEP_NAME_LEN), nullable=False),
        sa.Column("status", sa.String(length=_STEP_STATUS_LEN), nullable=False),
        sa.Column(
            "request_json",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("response_json", JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.String(length=_STEP_ERROR_CODE_LEN), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_NOW,
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["agent_runs.id"],
            name="fk_agent_run_steps_run_id_agent_runs",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("run_id", "step_order", name="uq_agent_run_steps_run_order"),
        sa.UniqueConstraint("run_id", "call_id", name="uq_agent_run_steps_run_call"),
        sa.CheckConstraint(
            "jsonb_typeof(request_json) = 'object'",
            name="ck_agent_run_steps_request_object",
        ),
    )
    op.create_index(
        "ix_agent_run_steps_run_order",
        "agent_run_steps",
        ["run_id", "step_order"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_run_steps_run_order", table_name="agent_run_steps")
    op.drop_table("agent_run_steps")
    op.drop_column("agent_runs", "orchestrator_version")
