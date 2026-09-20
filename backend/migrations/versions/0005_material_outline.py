"""解析产物表与资料标记删除列

Revision ID: 0005_material_outline
Revises: 0004_upload_session_expired_at
Create Date: 2026-09-20 15:00:00.000000

对应 ``docs/api-contract.md`` 第 5 节：

- ``materials.deleted_at``：契约 5.2 的标记删除列。NULL 表示未删除；非 NULL 的
  资料从所有读接口中消失，记录本身保留（上传会话与审计依赖它）。
- ``material_sections`` / ``material_knowledge_points``：契约 5.4 的解析产物，
  由解析 Worker 在成功时一次性写入。``(material_id, order)`` 与
  ``(section_id, order)`` 唯一约束保证顺序号不重复；外键均为
  ``ondelete CASCADE``，删除资料记录时（如未来引入硬删除）产物一并消失。
- ``ix_materials_course_id_created_at_id``：资料列表（契约 5.1）的
  过滤 + 排序 + 分页复合索引。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005_material_outline"
down_revision: str | None = "0004_upload_session_expired_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "materials",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_materials_course_id_created_at_id",
        "materials",
        ["course_id", "created_at", "id"],
    )

    op.create_table(
        "material_sections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "material_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "materials.id",
                ondelete="CASCADE",
                name="fk_material_sections_material_id_materials",
            ),
            nullable=False,
        ),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("location_start", sa.Integer(), nullable=False),
        sa.Column("location_end", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "material_id", "order", name="uq_material_sections_material_order"
        ),
    )
    op.create_index(
        "ix_material_sections_material_id", "material_sections", ["material_id"]
    )

    op.create_table(
        "material_knowledge_points",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "section_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "material_sections.id",
                ondelete="CASCADE",
                name="fk_material_knowledge_points_section_id_material_sections",
            ),
            nullable=False,
        ),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=False),
        sa.Column("quote", sa.String(length=2000), nullable=False),
        sa.Column("location_start", sa.Integer(), nullable=False),
        sa.Column("location_end", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "section_id", "order", name="uq_material_knowledge_points_section_order"
        ),
    )
    op.create_index(
        "ix_material_knowledge_points_section_id",
        "material_knowledge_points",
        ["section_id"],
    )


def downgrade() -> None:
    op.drop_table("material_knowledge_points")
    op.drop_table("material_sections")
    op.drop_index(
        "ix_materials_course_id_created_at_id", table_name="materials"
    )
    op.drop_column("materials", "deleted_at")
