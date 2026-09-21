"""material_chunks 表：可检索的原文片段

Revision ID: 0007_material_chunks
Revises: 0006_deletion_pipeline
Create Date: 2026-09-21 09:40:00.000000

对应 ``docs/api-contract.md`` 5.5（解析产物落库）与 6.1（问答只检索原文片段）：

- 片段保存资料、原文（``content``，无长度限制）、顺序与来源定位；
- ``(material_id, order)`` 唯一 + 全量重写：解析失败、旧执行者回写与重复
  解析都不会留下重复或半份片段；
- ``material_id`` 级联删除：资料被删除时片段随之消失，不会再被检索。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007_material_chunks"
down_revision: str | None = "0006_deletion_pipeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "material_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("location_start", sa.Integer(), nullable=False),
        sa.Column("location_end", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["material_id"],
            ["materials.id"],
            name="fk_material_chunks_material_id_materials",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_material_chunks"),
        sa.UniqueConstraint(
            "material_id", "order", name="uq_material_chunks_material_order"
        ),
    )
    op.create_index(
        "ix_material_chunks_material_id", "material_chunks", ["material_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_material_chunks_material_id", table_name="material_chunks")
    op.drop_table("material_chunks")
