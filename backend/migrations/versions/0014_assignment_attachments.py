"""作业附件表 ``assignment_attachments``（契约 8.15）。

教师可以给任务附参考文件（实验指导、数据集说明等），学生可下载。
一行同时承担"待上传会话"与"附件"两种身份：

- ``completed_at IS NULL``：这次上传还没确认，对读接口**不可见**；
- ``completed_at`` 非空：有效附件。

之所以不额外建一张上传会话表：附件只属于教师本人与这份任务，不需要像
实验报告那样按学生、按提交复用同一条记录，因此一个待完成行就够了。

唯一的完整性约束是"同一作业下**已完成**的附件不允许同名"，
用部分唯一索引实现（未完成的 pending 行可以有多条，重复初始化会留下多条）。

对象键由课程、任务与上传 UUID 推导（``courses/<课程>/assignments/<任务>/attachments/<上传><扩展名>``），
**不含用户文件名**，与实验报告前缀分开、互不覆盖。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.db.types import UtcDateTime

# revision identifiers, used by Alembic.
revision: str = "0014_assignment_attachments"
down_revision: str | None = "0013_merge_agent_and_submissions"
branch_labels = None
depends_on = None

#: 服务器默认时间：与既有迁移一致，用数据库时钟生成 created_at / updated_at
_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "assignment_attachments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column("upload_url_expires_at", UtcDateTime(), nullable=False),
        sa.Column("confirm_deadline_at", UtcDateTime(), nullable=False),
        sa.Column("completed_at", UtcDateTime(), nullable=True),
        sa.Column("created_at", UtcDateTime(), server_default=_NOW, nullable=False),
        sa.Column("updated_at", UtcDateTime(), server_default=_NOW, nullable=False),
        sa.CheckConstraint("size > 0", name="ck_assignment_attachments_size"),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["assignments.id"],
            name="fk_assignment_attachments_assignment_id_assignments",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
            name="fk_assignment_attachments_uploaded_by_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assignment_attachments"),
        sa.UniqueConstraint(
            "object_key", name="uq_assignment_attachments_object_key"
        ),
        sa.UniqueConstraint("upload_id", name="uq_assignment_attachments_upload_id"),
    )
    op.create_index(
        "ix_assignment_attachments_assignment_id",
        "assignment_attachments",
        ["assignment_id"],
    )
    # 同一作业下已完成的附件不允许同名；未完成的 pending 行不受此约束
    op.create_index(
        "uq_assignment_attachments_completed_filename",
        "assignment_attachments",
        ["assignment_id", "filename"],
        unique=True,
        postgresql_where=sa.text("completed_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_assignment_attachments_completed_filename",
        table_name="assignment_attachments",
        postgresql_where=sa.text("completed_at IS NOT NULL"),
    )
    op.drop_index(
        "ix_assignment_attachments_assignment_id", table_name="assignment_attachments"
    )
    op.drop_table("assignment_attachments")
