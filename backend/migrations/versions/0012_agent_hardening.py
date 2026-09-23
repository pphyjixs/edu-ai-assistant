"""Agent 与解析流水线的加固列（评审文档「一、当前代码的 Bug」#2 / #8 / #9 / #12）。

本迁移只做**加列与放开约束**，不改变任何既有行的语义：

1. ``jobs.failure_stage`` / ``materials.failure_stage``：解析与生成失败的**阶段码**
   （``DOWNLOAD`` / ``NATIVE_EXTRACT`` / ``OUTLINE_GENERATION`` / ``MODEL_CALL`` …）。
   前端据此区分「PDF 本身读不出来」和「模型侧失败」，而不是只看到一句失败原因。
2. ``agent_runs.request_fingerprint``：幂等键对应的**请求指纹**。同一个
   ``client_request_id`` 复用了不同请求体时，服务端返回 409 而不是静默返回旧 Run。
3. ``agent_runs.evidence_level``：本次回答的依据充分度（``FULL`` / ``PARTIAL`` / ``NONE``），
   供审计与「部分依据」提示使用。
4. ``chat_message_citations`` 扩展为**通用引用**：新增 ``source_kind`` / ``source_id`` /
   ``source_label``，并把 ``material_id`` / ``material_name`` / ``location_*`` 放开为可空。
   作业与评分标准是可信业务对象，应当能支撑 grounded answer 并显示为
   「依据：当前作业要求 / 评分标准」，而不必被降级成「未找到依据」。
   既有行的 ``source_kind`` 由 ``server_default='MATERIAL'`` 填为 ``MATERIAL``，语义不变。

PostgreSQL 的 ``ALTER COLUMN ... DROP NOT NULL`` 与 ``ADD COLUMN`` 都是元数据操作，
不重写表数据；``server_default`` 只影响此后插入的行，历史行由本迁移显式回填。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_agent_hardening"
down_revision: str | None = "0011_agent_runs"
branch_labels = None
depends_on = None

#: 失败阶段码的列长；与 ``app.modules.jobs.models.FAILURE_STAGE_MAX_LENGTH`` 一致
_STAGE_LEN = 32
#: 请求指纹列长（sha256 十六进制 = 64）
_FINGERPRINT_LEN = 64
#: 依据充分度列长
_EVIDENCE_LEN = 8
#: 引用来源类别列长
_SOURCE_KIND_LEN = 16


def upgrade() -> None:
    # ---------------------- 1. 解析/生成失败的阶段码 ---------------------- #
    op.add_column(
        "jobs",
        sa.Column("failure_stage", sa.String(length=_STAGE_LEN), nullable=True),
    )
    op.add_column(
        "materials",
        sa.Column("failure_stage", sa.String(length=_STAGE_LEN), nullable=True),
    )

    # ---------------------- 2. Run 的幂等指纹与依据等级 ---------------------- #
    op.add_column(
        "agent_runs",
        sa.Column("request_fingerprint", sa.String(length=_FINGERPRINT_LEN), nullable=True),
    )
    op.add_column(
        "agent_runs",
        sa.Column("evidence_level", sa.String(length=_EVIDENCE_LEN), nullable=True),
    )

    # ---------------------- 3. 引用扩展为通用来源 ---------------------- #
    op.add_column(
        "chat_message_citations",
        sa.Column(
            "source_kind",
            sa.String(length=_SOURCE_KIND_LEN),
            nullable=False,
            server_default="MATERIAL",
        ),
    )
    op.add_column(
        "chat_message_citations",
        sa.Column("source_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "chat_message_citations",
        sa.Column("source_label", sa.String(length=255), nullable=True),
    )

    # 历史行回填：来源自身 ID 就是资料 ID，标签取资料名
    op.execute(
        sa.text(
            "UPDATE chat_message_citations "
            "SET source_id = material_id, source_label = material_name "
            "WHERE source_id IS NULL"
        )
    )

    op.alter_column("chat_message_citations", "material_id", nullable=True)
    op.alter_column("chat_message_citations", "material_name", nullable=True)
    op.alter_column("chat_message_citations", "location_start", nullable=True)
    op.alter_column("chat_message_citations", "location_end", nullable=True)

    # 说明：``agent_runs`` 的会话列表查询复用 0011 已建的
    # ``ix_agent_runs_session_created_id``（(session_id, created_at, id)），
    # 按 created_at 倒序扫描正是它的反向扫描，因此这里不再新建同形索引。


def downgrade() -> None:
    # 作业/评分标准引用的资料字段为空，无法回填成 NOT NULL，只能先删除这些行；
    # 它们只在 0012 之后由 agent 模块写入。
    op.execute(sa.text("DELETE FROM chat_message_citations WHERE source_kind <> 'MATERIAL'"))

    op.alter_column("chat_message_citations", "location_end", nullable=False)
    op.alter_column("chat_message_citations", "location_start", nullable=False)
    op.alter_column("chat_message_citations", "material_name", nullable=False)
    op.alter_column("chat_message_citations", "material_id", nullable=False)

    op.drop_column("chat_message_citations", "source_label")
    op.drop_column("chat_message_citations", "source_id")
    op.drop_column("chat_message_citations", "source_kind")

    op.drop_column("agent_runs", "evidence_level")
    op.drop_column("agent_runs", "request_fingerprint")

    op.drop_column("materials", "failure_stage")
    op.drop_column("jobs", "failure_stage")
