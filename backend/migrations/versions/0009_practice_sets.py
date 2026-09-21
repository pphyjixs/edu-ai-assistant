"""课程练习：practice 六表与四个原生枚举

Revision ID: 0009_practice_sets
Revises: 0008_chat_qa
Create Date: 2026-09-21 15:40:00.000000

对应 ``docs/api-contract.md`` 第 7 节：

- ``practice_sets``：练习集与生成状态、难度、请求题数/题型；
- ``practice_set_materials``：生成时选择的来源资料与顺序（**快照引用**，
  不设外键，资料被软删除或重新解析不影响历史练习）；
- ``practice_questions``：题目、JSONB 选项与标准答案、简答评分要点、
  来源资料/片段定位/原文摘录快照；
- ``practice_attempts`` / ``practice_attempt_answers``：答题记录与每题得分，
  ``(practice_set_id, student_id)`` 唯一约束保证"每生每题集只提交一次"；
- ``practice_generation_attempts``：生成尝试记录（安全失败摘要）。

题型、难度、状态与生成尝试状态使用 PostgreSQL 原生枚举。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009_practice_sets"
down_revision: str | None = "0008_chat_qa"
branch_labels = None
depends_on = None

PRACTICE_STATUS = sa.Enum(
    "GENERATING", "DRAFT", "PUBLISHED", "FAILED", "CANCELLED",
    name="practice_status",
    native_enum=True,
)
PRACTICE_DIFFICULTY = sa.Enum(
    "EASY", "MEDIUM", "HARD", name="practice_difficulty", native_enum=True
)
PRACTICE_QUESTION_TYPE = sa.Enum(
    "SINGLE_CHOICE", "TRUE_FALSE", "SHORT_ANSWER",
    name="practice_question_type",
    native_enum=True,
)
PRACTICE_GENERATION_STATUS = sa.Enum(
    "SUCCEEDED", "FAILED", name="practice_generation_status", native_enum=True
)

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "practice_sets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", PRACTICE_STATUS, nullable=False),
        sa.Column("difficulty", PRACTICE_DIFFICULTY, nullable=False),
        sa.Column("requested_question_count", sa.Integer(), nullable=False),
        sa.Column("question_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("question_types", JSONB(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            "requested_question_count >= 1 AND requested_question_count <= 20",
            name="ck_practice_sets_requested_count",
        ),
        sa.CheckConstraint(
            "question_count >= 0 AND question_count <= 20",
            name="ck_practice_sets_question_count",
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            name="fk_practice_sets_course_id_courses",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["teacher_id"],
            ["users.id"],
            name="fk_practice_sets_teacher_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_practice_sets"),
    )
    op.create_index(
        "ix_practice_sets_course_status_published",
        "practice_sets",
        ["course_id", "status", "published_at", "id"],
    )

    op.create_table(
        "practice_set_materials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("practice_set_id", sa.Uuid(), nullable=False),
        sa.Column("material_id", sa.Uuid(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["practice_set_id"],
            ["practice_sets.id"],
            name="fk_practice_set_materials_practice_set_id_practice_sets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_practice_set_materials"),
        sa.UniqueConstraint(
            "practice_set_id", "order", name="uq_practice_set_materials_set_order"
        ),
        sa.UniqueConstraint(
            "practice_set_id",
            "material_id",
            name="uq_practice_set_materials_set_material",
        ),
    )
    op.create_index(
        "ix_practice_set_materials_set_id", "practice_set_materials", ["practice_set_id"]
    )

    op.create_table(
        "practice_questions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("practice_set_id", sa.Uuid(), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("type", PRACTICE_QUESTION_TYPE, nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("options", JSONB(), nullable=False),
        sa.Column("correct_answer", JSONB(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("knowledge_point", sa.String(length=255), nullable=True),
        sa.Column("grading_points", JSONB(), nullable=False),
        sa.Column("source_material_id", sa.Uuid(), nullable=False),
        sa.Column("source_material_name", sa.String(length=255), nullable=False),
        sa.Column("source_location_start", sa.Integer(), nullable=False),
        sa.Column("source_location_end", sa.Integer(), nullable=False),
        sa.Column("source_quote", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["practice_set_id"],
            ["practice_sets.id"],
            name="fk_practice_questions_practice_set_id_practice_sets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_practice_questions"),
        sa.UniqueConstraint(
            "practice_set_id", "order", name="uq_practice_questions_set_order"
        ),
    )
    op.create_index(
        "ix_practice_questions_set_id", "practice_questions", ["practice_set_id"]
    )

    op.create_table(
        "practice_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("practice_set_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("total_score", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            "total_score >= 0 AND total_score <= 100",
            name="ck_practice_attempts_total_score",
        ),
        sa.ForeignKeyConstraint(
            ["practice_set_id"],
            ["practice_sets.id"],
            name="fk_practice_attempts_practice_set_id_practice_sets",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["users.id"],
            name="fk_practice_attempts_student_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_practice_attempts"),
        sa.UniqueConstraint(
            "practice_set_id", "student_id", name="uq_practice_attempts_set_student"
        ),
    )
    op.create_index(
        "ix_practice_attempts_student_submitted",
        "practice_attempts",
        ["student_id", "submitted_at"],
    )

    op.create_table(
        "practice_attempt_answers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("question_order", sa.Integer(), nullable=False),
        sa.Column("submitted_answer", JSONB(), nullable=False),
        sa.Column("is_correct", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("matched_points", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.CheckConstraint(
            "score >= 0 AND score <= 100", name="ck_practice_attempt_answers_score"
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["practice_attempts.id"],
            name="fk_practice_attempt_answers_attempt_id_practice_attempts",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_practice_attempt_answers"),
        sa.UniqueConstraint(
            "attempt_id", "question_id", name="uq_practice_attempt_answers_question"
        ),
    )
    op.create_index(
        "ix_practice_attempt_answers_attempt_id",
        "practice_attempt_answers",
        ["attempt_id"],
    )

    op.create_table(
        "practice_generation_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("practice_set_id", sa.Uuid(), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column("status", PRACTICE_GENERATION_STATUS, nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("requested_count", sa.Integer(), nullable=False),
        sa.Column("question_count", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False),
        sa.ForeignKeyConstraint(
            ["practice_set_id"],
            ["practice_sets.id"],
            name="fk_practice_generation_attempts_set_id_practice_sets",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_practice_generation_attempts"),
    )
    op.create_index(
        "ix_practice_generation_attempts_set_created",
        "practice_generation_attempts",
        ["practice_set_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_practice_generation_attempts_set_created",
        table_name="practice_generation_attempts",
    )
    op.drop_table("practice_generation_attempts")
    op.drop_index(
        "ix_practice_attempt_answers_attempt_id",
        table_name="practice_attempt_answers",
    )
    op.drop_table("practice_attempt_answers")
    op.drop_index(
        "ix_practice_attempts_student_submitted", table_name="practice_attempts"
    )
    op.drop_table("practice_attempts")
    op.drop_index("ix_practice_questions_set_id", table_name="practice_questions")
    op.drop_table("practice_questions")
    op.drop_index(
        "ix_practice_set_materials_set_id", table_name="practice_set_materials"
    )
    op.drop_table("practice_set_materials")
    op.drop_index(
        "ix_practice_sets_course_status_published", table_name="practice_sets"
    )
    op.drop_table("practice_sets")

    PRACTICE_GENERATION_STATUS.drop(op.get_bind(), checkfirst=True)
    PRACTICE_QUESTION_TYPE.drop(op.get_bind(), checkfirst=True)
    PRACTICE_DIFFICULTY.drop(op.get_bind(), checkfirst=True)
    PRACTICE_STATUS.drop(op.get_bind(), checkfirst=True)
