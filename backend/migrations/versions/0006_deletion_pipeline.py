"""对象删除待办、任务执行字段与完成响应快照

Revision ID: 0006_deletion_pipeline
Revises: 0005_material_outline
Create Date: 2026-09-20 17:00:00.000000

对应 ``docs/api-contract.md`` 第 5 节的删除与解析执行机制：

- ``material_delete_todos``：对象删除待办。删除资料时在同一事务中写入，
  由独立维护命令在原 PUT 地址过期并经过缓冲期后删除对象——避免与
  仍在途的浏览器直传竞争；失败持续重试（``attempts``/``last_error``），
  删除后再次核查晚到的 PUT，通过后标记 ``DONE``。
- ``jobs.attempts`` / ``jobs.run_token`` / ``jobs.lease_expires_at``：
  Worker 原子领取时递增尝试次数、生成运行令牌并设置租约；
  回写必须携带匹配的运行令牌，防止过期 Worker 覆盖新一轮执行。
- ``material_upload_sessions.completion_snapshot``：完成响应快照（JSONB）。
  首次完成时保存 ``{material, job}``，重复确认（含资料已删除或状态变化后）
  一律回填快照，保证"重复完成上传返回首次结果"。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006_deletion_pipeline"
down_revision: str | None = "0005_material_outline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "jobs",
        sa.Column("run_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "jobs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "material_upload_sessions",
        sa.Column("completion_snapshot", postgresql.JSONB(), nullable=True),
    )

    op.create_table(
        "material_delete_todos",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "material_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "materials.id",
                ondelete="CASCADE",
                name="fk_material_delete_todos_material_id_materials",
            ),
            nullable=False,
            unique=True,
        ),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("upload_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "DONE",
                name="material_delete_todo_status",
                native_enum=True,
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_material_delete_todos_status", "material_delete_todos", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_material_delete_todos_status", table_name="material_delete_todos")
    op.drop_table("material_delete_todos")
    sa.Enum(name="material_delete_todo_status").drop(op.get_bind(), checkfirst=True)
    op.drop_column("material_upload_sessions", "completion_snapshot")
    op.drop_column("jobs", "lease_expires_at")
    op.drop_column("jobs", "run_token")
    op.drop_column("jobs", "attempts")
