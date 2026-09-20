"""给上传会话增加 expired_at 过期清理标记

Revision ID: 0004_upload_session_expired_at
Revises: 0003_materials
Create Date: 2026-09-20 13:40:00.000000

对应 ``docs/api-contract.md`` 第 4.6 节的过期清理：

- ``material_upload_sessions.expired_at``：清理命令在删除过期未完成会话的孤立对象后，
  写入该标记（非 NULL 表示已清理）。清理命令据此幂等：已标记的会话不再重复扫描。
- 已完成的会话（``completed_material_id`` 非空）永不进入清理范围，
  其资料（``materials``）与对象永久保留，不受清理影响。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004_upload_session_expired_at"
down_revision: str | None = "0003_materials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "material_upload_sessions",
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("material_upload_sessions", "expired_at")
