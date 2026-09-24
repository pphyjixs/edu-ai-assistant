"""Dashboard 只读聚合索引

Revision ID: 0015_dashboard_indexes
Revises: 01d9328a578b
Create Date: 2026-09-23 12:00:00.000000

对应 ``docs/api-contract.md`` 第 11 节（Dashboard 接口）：

Dashboard 是跨模块只读聚合，不新增表、枚举、约束或历史数据，只为本模块的
热路径建立复合索引，其中三个是**部分索引**，只覆盖聚合需要的行：

- ``submissions(course_id, submitted_at, id) WHERE submitted_at IS NOT NULL``：
  教师「最近提交」只读正式提交（``UPLOADING`` 的 ``submitted_at`` 为 NULL）；
- ``submissions(student_id, status, updated_at, id)``：
  按学生与其提交状态定位本人记录（判断「是否有正式提交」与状态过滤）；
- ``assignments(course_id, status, due_at, id)``：
  学生「待完成任务」按课程 + 发布状态 + 截止时间定位；
- ``materials(course_id, status, updated_at, id) WHERE deleted_at IS NULL``：
  教师/学生「资料处理状态」只统计未删除资料；
- ``grade_reviews(published_at, id, submission_id) WHERE published_at IS NOT NULL``：
  学生「最近反馈」只取已发布成绩。

索引列顺序与谓词表达式必须和 ORM ``__table_args__`` 中的定义**逐字一致**，
保证 Alembic autogenerate 检查无差异。降级按反序删除全部新增索引，不修改表、
枚举或历史数据。
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "0015_dashboard_indexes"
down_revision: str | None = "01d9328a578b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 教师最近提交 / 待批改：按课程聚合，只读正式提交（submitted_at 非空）
    op.create_index(
        "ix_submissions_course_submitted_id",
        "submissions",
        ["course_id", "submitted_at", "id"],
        postgresql_where=text("submitted_at IS NOT NULL"),
    )
    # 学生本人提交：按学生 + 状态定位（判断待完成任务是否已有正式提交）
    op.create_index(
        "ix_submissions_student_status_updated_id",
        "submissions",
        ["student_id", "status", "updated_at", "id"],
    )
    # 学生待完成任务：按课程 + 发布状态 + 截止时间定位
    op.create_index(
        "ix_assignments_course_status_due_id",
        "assignments",
        ["course_id", "status", "due_at", "id"],
    )
    # 资料处理状态：按课程 + 状态聚合，只统计未删除资料
    op.create_index(
        "ix_materials_course_status_updated_id",
        "materials",
        ["course_id", "status", "updated_at", "id"],
        postgresql_where=text("deleted_at IS NULL"),
    )
    # 学生最近反馈：只取已发布成绩，按发布时间倒序
    op.create_index(
        "ix_grade_reviews_published_id_submission_id",
        "grade_reviews",
        ["published_at", "id", "submission_id"],
        postgresql_where=text("published_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_grade_reviews_published_id_submission_id", table_name="grade_reviews"
    )
    op.drop_index("ix_materials_course_status_updated_id", table_name="materials")
    op.drop_index("ix_assignments_course_status_due_id", table_name="assignments")
    op.drop_index(
        "ix_submissions_student_status_updated_id", table_name="submissions"
    )
    op.drop_index("ix_submissions_course_submitted_id", table_name="submissions")
