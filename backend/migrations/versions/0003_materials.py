"""materials 表：jobs、material_upload_sessions、materials

Revision ID: 0003_materials
Revises: 0002_courses
Create Date: 2026-09-20 10:35:00.000000

对应 ``docs/api-contract.md`` 第 4 节的课件上传协议：

- ``jobs``：异步任务。用 ``(resource_type, resource_id)`` 泛化引用业务资源，
  ``(type, resource_id)`` 唯一约束保证同一资源上同类任务只有一条——重复完成上传
  不会创建第二个 ``MATERIAL_PARSE`` 任务。
- ``material_upload_sessions``：上传会话。保存课程、发起教师、随机对象键、
  预期大小、类型、哈希、PUT 地址到期时间、确认截止时间与完成结果。
  ``object_key`` 全局唯一，客户端无法猜测或覆盖别人的对象。
- ``materials``：资料。``upload_id`` 唯一外键指向上传会话，是幂等完成的最终防线。

``completed_material_id`` 与 ``materials.upload_id`` 互相引用构成循环外键，
因此会话侧的外键在两张表建好之后单独补（对应 ORM 的 ``use_alter=True``）。

全部时间列为 ``TIMESTAMP WITH TIME ZONE``，应用侧统一写入 UTC。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_materials"
down_revision: str | None = "0002_courses"
branch_labels = None
depends_on = None

#: 与 app.modules.materials.models / app.modules.jobs.models 保持一致
FILENAME_MAX_LENGTH = 255
CONTENT_TYPE_MAX_LENGTH = 255
SHA256_HEX_LENGTH = 64
OBJECT_KEY_MAX_LENGTH = 512
MATERIAL_ERROR_MAX_LENGTH = 500
JOB_ERROR_MAX_LENGTH = 500


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "type",
            sa.Enum(
                "MATERIAL_PARSE",
                "PRACTICE_GENERATE",
                "SUBMISSION_GRADE",
                name="job_type",
                native_enum=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                name="job_status",
                native_enum=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "progress", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "resource_type",
            sa.Enum(
                "MATERIAL",
                "PRACTICE_SET",
                "SUBMISSION",
                name="job_resource_type",
                native_enum=True,
            ),
            nullable=False,
        ),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("error", sa.String(length=JOB_ERROR_MAX_LENGTH), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        # 同一资源上同类任务只有一条
        sa.UniqueConstraint("type", "resource_id", name="uq_jobs_type_resource_id"),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index(
        "ix_jobs_resource_type_resource_id",
        "jobs",
        ["resource_type", "resource_id"],
    )

    op.create_table(
        "material_upload_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column(
            "object_key", sa.String(length=OBJECT_KEY_MAX_LENGTH), nullable=False
        ),
        sa.Column(
            "filename", sa.String(length=FILENAME_MAX_LENGTH), nullable=False
        ),
        sa.Column(
            "content_type",
            sa.String(length=CONTENT_TYPE_MAX_LENGTH),
            nullable=False,
        ),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=SHA256_HEX_LENGTH), nullable=False),
        sa.Column("upload_url_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirm_deadline_at", sa.DateTime(timezone=True), nullable=False),
        # 完成结果；外键在 materials 建好后补齐（循环外键）
        sa.Column("completed_material_id", sa.Uuid(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            name="fk_material_upload_sessions_course_id_courses",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["teacher_id"],
            ["users.id"],
            name="fk_material_upload_sessions_teacher_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_material_upload_sessions"),
        # 对象键全局唯一：不同上传不会互相覆盖
        sa.UniqueConstraint(
            "object_key", name="uq_material_upload_sessions_object_key"
        ),
        sa.UniqueConstraint(
            "completed_material_id",
            name="uq_material_upload_sessions_completed_material_id",
        ),
    )
    op.create_index(
        "ix_material_upload_sessions_course_id",
        "material_upload_sessions",
        ["course_id"],
    )
    op.create_index(
        "ix_material_upload_sessions_teacher_id",
        "material_upload_sessions",
        ["teacher_id"],
    )
    op.create_index(
        "ix_material_upload_sessions_confirm_deadline_at",
        "material_upload_sessions",
        ["confirm_deadline_at"],
    )

    op.create_table(
        "materials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column(
            "filename", sa.String(length=FILENAME_MAX_LENGTH), nullable=False
        ),
        sa.Column(
            "content_type",
            sa.String(length=CONTENT_TYPE_MAX_LENGTH),
            nullable=False,
        ),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=SHA256_HEX_LENGTH), nullable=False),
        sa.Column(
            "storage_key", sa.String(length=OBJECT_KEY_MAX_LENGTH), nullable=False
        ),
        sa.Column(
            "status",
            sa.Enum(
                "UPLOADING",
                "UPLOADED",
                "PROCESSING",
                "READY",
                "FAILED",
                name="material_status",
                native_enum=True,
            ),
            nullable=False,
        ),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column(
            "error_message",
            sa.String(length=MATERIAL_ERROR_MAX_LENGTH),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            name="fk_materials_course_id_courses",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["upload_id"],
            ["material_upload_sessions.id"],
            name="fk_materials_upload_id_material_upload_sessions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
            name="fk_materials_uploaded_by_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_materials"),
        # 一次上传只产生一条资料：重复完成的最终防线
        sa.UniqueConstraint("upload_id", name="uq_materials_upload_id"),
        sa.UniqueConstraint("storage_key", name="uq_materials_storage_key"),
    )
    op.create_index("ix_materials_course_id", "materials", ["course_id"])
    op.create_index("ix_materials_uploaded_by", "materials", ["uploaded_by"])

    # 循环外键：会话指向本次上传创建的资料，必须等 materials 建好后再补
    op.create_foreign_key(
        "fk_material_upload_sessions_completed_material_id_materials",
        "material_upload_sessions",
        "materials",
        ["completed_material_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    # 先删循环外键，再删表
    op.drop_constraint(
        "fk_material_upload_sessions_completed_material_id_materials",
        "material_upload_sessions",
        type_="foreignkey",
    )

    op.drop_index("ix_materials_uploaded_by", table_name="materials")
    op.drop_index("ix_materials_course_id", table_name="materials")
    op.drop_table("materials")

    op.drop_index(
        "ix_material_upload_sessions_confirm_deadline_at",
        table_name="material_upload_sessions",
    )
    op.drop_index(
        "ix_material_upload_sessions_teacher_id",
        table_name="material_upload_sessions",
    )
    op.drop_index(
        "ix_material_upload_sessions_course_id",
        table_name="material_upload_sessions",
    )
    op.drop_table("material_upload_sessions")

    op.drop_index("ix_jobs_resource_type_resource_id", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_table("jobs")

    # 枚举类型不会随表一起删除，必须显式清理
    sa.Enum(name="material_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="job_resource_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="job_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="job_type").drop(op.get_bind(), checkfirst=True)
