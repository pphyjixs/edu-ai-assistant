"""courses 表：courses、course_members

Revision ID: 0002_courses
Revises: 0001_auth_base
Create Date: 2026-09-19 20:40:00.000000

对应 ``docs/api-contract.md`` 第 3 节：

- ``courses``：课程。``invite_code`` 建全局唯一约束，并发生成冲突由数据库兜住；
  ``teacher_id`` 指向创建教师，使用 ``RESTRICT``——教师名下有课程时不应被删除。
- ``course_members``：课程成员。``(course_id, user_id)`` 唯一约束保证同一课程内
  用户只有一条记录，并发重复加入正是被这条约束拦住。

全部时间列为 ``TIMESTAMP WITH TIME ZONE``，应用侧统一写入 UTC。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_courses"
down_revision: str | None = "0001_auth_base"
branch_labels = None
depends_on = None

#: 与 app.modules.courses.models 保持一致
COURSE_NAME_MAX_LENGTH = 100
COURSE_DESCRIPTION_MAX_LENGTH = 2000
INVITE_CODE_LENGTH = 12


def upgrade() -> None:
    op.create_table(
        "courses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=COURSE_NAME_MAX_LENGTH), nullable=False),
        sa.Column(
            "description",
            sa.String(length=COURSE_DESCRIPTION_MAX_LENGTH),
            server_default=sa.text("''"),
            nullable=False,
        ),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "ARCHIVED", name="course_status", native_enum=True),
            nullable=False,
        ),
        sa.Column(
            "invite_code", sa.String(length=INVITE_CODE_LENGTH), nullable=False
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
            ["teacher_id"],
            ["users.id"],
            name="fk_courses_teacher_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_courses"),
        # 邀请码全局唯一：并发生成冲突的唯一防线
        sa.UniqueConstraint("invite_code", name="uq_courses_invite_code"),
    )
    op.create_index("ix_courses_teacher_id", "courses", ["teacher_id"])

    op.create_table(
        "course_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "course_role",
            sa.Enum("TEACHER", "STUDENT", name="course_role", native_enum=True),
            nullable=False,
        ),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            name="fk_course_members_course_id_courses",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_course_members_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_course_members"),
        # 并发重复加入的唯一防线：不做先查后插
        sa.UniqueConstraint(
            "course_id", "user_id", name="uq_course_members_course_id_user_id"
        ),
    )
    op.create_index("ix_course_members_user_id", "course_members", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_course_members_user_id", table_name="course_members")
    op.drop_table("course_members")

    op.drop_index("ix_courses_teacher_id", table_name="courses")
    op.drop_table("courses")

    # 枚举类型不会随表一起删除，必须显式清理
    sa.Enum(name="course_role").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="course_status").drop(op.get_bind(), checkfirst=True)
