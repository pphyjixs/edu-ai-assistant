"""异步任务契约：jobs 的进度范围与类型/资源配对约束

Revision ID: 0013_jobs_contract
Revises: 0012_submissions_grading
Create Date: 2026-09-23 10:00:00.000000

对应 ``docs/api-contract.md`` 第 10 节（通用异步任务接口）：

- ``ck_jobs_progress_range``：``progress BETWEEN 0 AND 100``——
  进度只是文档约定时，Worker 的笔误可以直接落库；现在由数据库兜住；
- ``ck_jobs_type_resource_match``：任务类型与资源类型必须配对，
  含三类公开任务（``MATERIAL_PARSE / MATERIAL``、``PRACTICE_GENERATE / PRACTICE_SET``、
  ``SUBMISSION_GRADE / SUBMISSION``）与内部 ``AGENT_RUN / AGENT_RUN``；
  未知组合（例如把 ``PRACTICE_GENERATE`` 关联到提交）在写入时即被拒绝。

约束表达式与 ORM ``Job.__table_args__`` 中的定义**逐字一致**，
保证 Alembic autogenerate 检查无差异。降级只删除这两条约束，不动数据与枚举。
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_jobs_contract"
down_revision: str | None = "0012_submissions_grading"
branch_labels = None
depends_on = None

#: 与 app.modules.jobs.models 保持一致的约束表达式
JOB_PROGRESS_CHECK = "progress BETWEEN 0 AND 100"
JOB_TYPE_RESOURCE_CHECK = (
    "(type = 'MATERIAL_PARSE' AND resource_type = 'MATERIAL')"
    " OR (type = 'PRACTICE_GENERATE' AND resource_type = 'PRACTICE_SET')"
    " OR (type = 'SUBMISSION_GRADE' AND resource_type = 'SUBMISSION')"
    " OR (type = 'AGENT_RUN' AND resource_type = 'AGENT_RUN')"
)


def upgrade() -> None:
    op.create_check_constraint(
        "ck_jobs_progress_range", "jobs", JOB_PROGRESS_CHECK
    )
    op.create_check_constraint(
        "ck_jobs_type_resource_match", "jobs", JOB_TYPE_RESOURCE_CHECK
    )


def downgrade() -> None:
    # 命名约定（ck_%(table_name)s_%(constraint_name)s）在创建与删除时都会生效，
    # 因此这里传**基础名**，落库名与升级时一致：ck_jobs_ck_jobs_<name>
    op.drop_constraint("ck_jobs_type_resource_match", "jobs", type_="check")
    op.drop_constraint("ck_jobs_progress_range", "jobs", type_="check")
