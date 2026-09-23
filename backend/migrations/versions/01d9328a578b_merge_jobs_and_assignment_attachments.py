"""Merge the Jobs API and assignment attachment migration branches.

Revision ID: 01d9328a578b
Revises: 0013_jobs_contract, 0014_assignment_attachments
"""


# revision identifiers, used by Alembic.
revision: str = "01d9328a578b"
down_revision: tuple[str, ...] = (
    "0013_jobs_contract",
    "0014_assignment_attachments",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Both parent migrations are applied; no additional schema changes are needed."""


def downgrade() -> None:
    """The parent migrations own their respective downgrade operations."""
