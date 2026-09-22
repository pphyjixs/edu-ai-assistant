"""实验任务：assignments 三表与原生枚举

Revision ID: 0010_assignments
Revises: 0009_practice_sets
Create Date: 2026-09-21 18:10:00.000000

对应 ``docs/api-contract.md`` 第 8 节：

- ``assignments``：任务本体与**当前评分规则版本**指针；
- ``assignment_rubric_versions``：不可变的评分规则版本（``(assignment_id, version)`` 唯一）；
- ``assignment_rubric_items``：某版本的评分项（``(rubric_version_id, order)`` 唯一）。

``assignments.current_rubric_version_id`` 与版本表互相引用，因此先建
``assignments``（该列此时只是普通列），建完版本表后再用
``op.create_foreign_key`` 补上外键；downgrade 按相反顺序删除。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010_assignments"
down_revision: str | None = "0009_practice_sets"
branch_labels = None
depends_on = None

ASSIGNMENT_STATUS = sa.Enum(
    "DRAFT", "PUBLISHED", "CLOSED", "ARCHIVED", name="assignment_status", native_enum=True
)

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "assignments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "allow_late_submission",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("status", ASSIGNMENT_STATUS, nullable=False),
        sa.Column("current_rubric_version_id", sa.Uuid(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            name="fk_assignments_course_id_courses",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_assignments_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assignments"),
    )
    op.create_index(
        "ix_assignments_course_status_created",
        "assignments",
        ["course_id", "status", "created_at", "id"],
    )

    op.create_table(
        "assignment_rubric_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("total_score", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
        ),
        sa.CheckConstraint(
            "total_score > 0", name="ck_assignment_rubric_versions_total_score"
        ),
        sa.CheckConstraint("version >= 1", name="ck_assignment_rubric_versions_version"),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["assignments.id"],
            name="fk_assignment_rubric_versions_assignment_id_assignments",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_assignment_rubric_versions_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assignment_rubric_versions"),
        sa.UniqueConstraint(
            "assignment_id", "version", name="uq_assignment_rubric_versions_version"
        ),
    )

    op.create_table(
        "assignment_rubric_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rubric_version_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("max_score", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
        ),
        sa.CheckConstraint("max_score > 0", name="ck_assignment_rubric_items_max_score"),
        sa.CheckConstraint('"order" > 0', name="ck_assignment_rubric_items_order"),
        sa.ForeignKeyConstraint(
            ["rubric_version_id"],
            ["assignment_rubric_versions.id"],
            name="fk_assignment_rubric_items_rubric_version_id_versions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assignment_rubric_items"),
        sa.UniqueConstraint(
            "rubric_version_id", "order", name="uq_assignment_rubric_items_version_order"
        ),
    )

    # 循环引用：版本表建好后再补 assignments → 版本的外键
    op.create_foreign_key(
        "fk_assignments_current_rubric_version_id_versions",
        "assignments",
        "assignment_rubric_versions",
        ["current_rubric_version_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_assignments_current_rubric_version_id_versions",
        "assignments",
        type_="foreignkey",
    )
    op.drop_table("assignment_rubric_items")
    op.drop_table("assignment_rubric_versions")
    op.drop_index("ix_assignments_course_status_created", table_name="assignments")
    op.drop_table("assignments")

    ASSIGNMENT_STATUS.drop(op.get_bind(), checkfirst=True)
