"""允许零分且报告中完全缺失对应内容时没有证据位置。

Revision ID: 0017_optional_grade_evidence
Revises: 0016_agent_tool_steps
"""

from alembic import op

revision = "0017_optional_grade_evidence"
down_revision = "0016_agent_tool_steps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        "evidence_quote", "evidence_source_type",
        "evidence_location_start", "evidence_location_end",
    ):
        op.alter_column("grade_items", column, nullable=True)
    op.create_check_constraint(
        "ck_grade_items_evidence_all_or_none", "grade_items",
        "(evidence_quote IS NULL AND evidence_source_type IS NULL AND "
        "evidence_location_start IS NULL AND evidence_location_end IS NULL) OR "
        "(evidence_quote IS NOT NULL AND evidence_source_type IS NOT NULL AND "
        "evidence_location_start IS NOT NULL AND evidence_location_end IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_grade_items_evidence_all_or_none", "grade_items", type_="check")
    # 老版本不能表示无证据的零分项；先删除这些新记录才可降级。
    for column in (
        "evidence_quote", "evidence_source_type",
        "evidence_location_start", "evidence_location_end",
    ):
        op.alter_column("grade_items", column, nullable=False)
