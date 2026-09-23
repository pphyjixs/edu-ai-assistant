"""提交与批改：submissions 五表与两个原生枚举

Revision ID: 0012_submissions_grading
Revises: 0011_agent_runs
Create Date: 2026-09-22 10:00:00.000000

对应 ``docs/api-contract.md`` 第 9 节：

- ``submissions``：提交本体，``(assignment_id, student_id)`` 唯一，
  ``rubric_version_id`` 是**历史外键**（提交固定的评分规则版本）；
- ``submission_upload_sessions``：每次上传尝试与其完成快照；
- ``grade_reviews``：每份提交唯一一条批改记录（AI 原始值与教师终稿并存）；
- ``grade_items``：评分项快照与 AI/教师分数（``rubric_item_id`` 为历史外键），
  证据定位含来源类型白名单与位置区间约束；
- ``submission_grade_attempts``：每次 Worker 尝试的审计轨迹。

不变量：``submission_upload_sessions`` 上有部分唯一索引
``(submission_id) WHERE completed_at IS NOT NULL``——每份提交最多只有一个**完成**的
上传会话，顺序或并发完成不同会话都会被它拦下。

降级按外键反序删除表、索引，最后删除原生枚举。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012_submissions_grading"
down_revision: str | None = "0011_agent_runs"
branch_labels = None
depends_on = None

SUBMISSION_STATUS = sa.Enum(
    "UPLOADING",
    "SUBMITTED",
    "GRADING",
    "REVIEW_REQUIRED",
    "PUBLISHED",
    "FAILED",
    name="submission_status",
    native_enum=True,
)

GRADE_ATTEMPT_STATUS = sa.Enum(
    "SUCCEEDED",
    "FAILED",
    name="submission_grade_attempt_status",
    native_enum=True,
)

_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "submissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("status", SUBMISSION_STATUS, nullable=False),
        sa.Column("rubric_version_id", sa.Uuid(), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column(
            "is_late", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["assignments.id"],
            name="fk_submissions_assignment_id_assignments",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            name="fk_submissions_course_id_courses",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["users.id"],
            name="fk_submissions_student_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["rubric_version_id"],
            ["assignment_rubric_versions.id"],
            name="fk_submissions_rubric_version_id_versions",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_submissions"),
        sa.UniqueConstraint(
            "assignment_id", "student_id", name="uq_submissions_assignment_student"
        ),
        sa.UniqueConstraint("object_key", name="uq_submissions_object_key"),
    )
    op.create_index(
        "ix_submissions_assignment_submitted",
        "submissions",
        ["assignment_id", "submitted_at", "id"],
    )
    op.create_index("ix_submissions_student_id", "submissions", ["student_id"])
    op.create_index("ix_submissions_course_id", "submissions", ["course_id"])
    op.create_index("ix_submissions_status", "submissions", ["status"])

    op.create_table(
        "submission_upload_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "upload_url_expires_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column("confirm_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completion_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name="fk_submission_upload_sessions_submission_id_submissions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_submission_upload_sessions"),
        sa.UniqueConstraint(
            "object_key", name="uq_submission_upload_sessions_object_key"
        ),
    )
    op.create_index(
        "uq_submission_upload_sessions_submission_completed",
        "submission_upload_sessions",
        ["submission_id"],
        unique=True,
        postgresql_where=sa.text("completed_at IS NOT NULL"),
    )
    op.create_index(
        "ix_submission_upload_sessions_submission_id",
        "submission_upload_sessions",
        ["submission_id"],
    )
    op.create_index(
        "ix_submission_upload_sessions_confirm_deadline_at",
        "submission_upload_sessions",
        ["confirm_deadline_at"],
    )

    op.create_table(
        "grade_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("ai_summary", sa.Text(), nullable=False),
        sa.Column("teacher_summary", sa.Text(), nullable=False),
        sa.Column(
            "suggested_total_score", sa.Numeric(precision=10, scale=2), nullable=False
        ),
        sa.Column(
            "final_total_score", sa.Numeric(precision=10, scale=2), nullable=False
        ),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
        ),
        sa.CheckConstraint(
            "suggested_total_score >= 0", name="ck_grade_reviews_suggested_total_score"
        ),
        sa.CheckConstraint(
            "final_total_score >= 0", name="ck_grade_reviews_final_total_score"
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name="fk_grade_reviews_submission_id_submissions",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name="fk_grade_reviews_reviewed_by_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_grade_reviews"),
        sa.UniqueConstraint("submission_id", name="uq_grade_reviews_submission_id"),
    )

    op.create_table(
        "grade_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("rubric_item_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("max_score", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("ai_score", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("final_score", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("ai_comment", sa.Text(), nullable=False),
        sa.Column("evidence_quote", sa.String(length=2000), nullable=False),
        sa.Column("evidence_source_type", sa.String(length=32), nullable=False),
        sa.Column("evidence_location_start", sa.Integer(), nullable=False),
        sa.Column("evidence_location_end", sa.Integer(), nullable=False),
        sa.Column(
            "error_type", sa.String(length=100), server_default="", nullable=False
        ),
        sa.Column(
            "improvement_suggestion", sa.Text(), server_default="", nullable=False
        ),
        sa.Column("teacher_comment", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
        ),
        sa.CheckConstraint("max_score > 0", name="ck_grade_items_max_score"),
        sa.CheckConstraint('"order" > 0', name="ck_grade_items_order"),
        sa.CheckConstraint(
            "ai_score >= 0 AND ai_score <= max_score", name="ck_grade_items_ai_score"
        ),
        sa.CheckConstraint(
            "final_score >= 0 AND final_score <= max_score",
            name="ck_grade_items_final_score",
        ),
        sa.CheckConstraint(
            "evidence_source_type IN ('PDF_PAGE', 'DOCX_PARAGRAPH')",
            name="ck_grade_items_evidence_source",
        ),
        sa.CheckConstraint(
            "evidence_location_start > 0", name="ck_grade_items_evidence_start"
        ),
        sa.CheckConstraint(
            "evidence_location_end >= evidence_location_start",
            name="ck_grade_items_evidence_range",
        ),
        sa.ForeignKeyConstraint(
            ["review_id"],
            ["grade_reviews.id"],
            name="fk_grade_items_review_id_grade_reviews",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["rubric_item_id"],
            ["assignment_rubric_items.id"],
            name="fk_grade_items_rubric_item_id_rubric_items",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_grade_items"),
        sa.UniqueConstraint(
            "review_id", "rubric_item_id", name="uq_grade_items_review_rubric_item"
        ),
        sa.UniqueConstraint("review_id", "order", name="uq_grade_items_review_order"),
    )

    op.create_table(
        "submission_grade_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("status", GRADE_ATTEMPT_STATUS, nullable=False),
        sa.Column("model", sa.String(length=200), nullable=True),
        sa.Column("prompt_version", sa.String(length=100), nullable=True),
        sa.Column("item_count", sa.Integer(), nullable=True),
        sa.Column("raw_output", sa.String(length=20000), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=_NOW, nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name="fk_submission_grade_attempts_submission_id_submissions",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_submission_grade_attempts"),
    )
    op.create_index(
        "ix_submission_grade_attempts_submission_id",
        "submission_grade_attempts",
        ["submission_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_submission_grade_attempts_submission_id",
        table_name="submission_grade_attempts",
    )
    op.drop_table("submission_grade_attempts")
    op.drop_table("grade_items")
    op.drop_table("grade_reviews")
    op.drop_index(
        "uq_submission_upload_sessions_submission_completed",
        table_name="submission_upload_sessions",
    )
    op.drop_index(
        "ix_submission_upload_sessions_confirm_deadline_at",
        table_name="submission_upload_sessions",
    )
    op.drop_index(
        "ix_submission_upload_sessions_submission_id",
        table_name="submission_upload_sessions",
    )
    op.drop_table("submission_upload_sessions")
    op.drop_index("ix_submissions_status", table_name="submissions")
    op.drop_index("ix_submissions_course_id", table_name="submissions")
    op.drop_index("ix_submissions_student_id", table_name="submissions")
    op.drop_index("ix_submissions_assignment_submitted", table_name="submissions")
    op.drop_table("submissions")

    GRADE_ATTEMPT_STATUS.drop(op.get_bind(), checkfirst=True)
    SUBMISSION_STATUS.drop(op.get_bind(), checkfirst=True)
