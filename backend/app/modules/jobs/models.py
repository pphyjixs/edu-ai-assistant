"""Jobs 模块 ORM 模型。

``jobs`` 表对应 ``docs/api-contract.md`` 第 10 节的 ``JobStatus`` 响应结构，也是
``docs/modules.md`` 第 8 节「统一管理异步状态」的落点：

- 任务用 ``(resource_type, resource_id)`` **泛化引用**业务资源，不建外键——
  资料、练习、提交分属不同模块，外键会让 jobs 反向依赖所有业务表；
- ``(type, resource_id)`` 唯一约束保证同一资源上同类任务只有一条。
  课件上传正是靠这条约束做到「重复完成上传不会创建第二个解析任务」；
- 状态与进度由 Worker 推进；第一版不实现 Worker，任务创建后如实保持 ``PENDING``。

时间列沿用 :class:`app.db.types.UtcDateTime`，读写两端均为 UTC。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Index, Integer, String, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.core.time import utc_now
from app.db.base import Base
from app.db.types import UtcDateTime

#: 失败原因列长度：只保存可安全展示的摘要，不保存堆栈
JOB_ERROR_MAX_LENGTH = 500


class JobType(str, enum.Enum):
    """任务类型。"""

    MATERIAL_PARSE = "MATERIAL_PARSE"
    PRACTICE_GENERATE = "PRACTICE_GENERATE"
    SUBMISSION_GRADE = "SUBMISSION_GRADE"


class JobStatusValue(str, enum.Enum):
    """任务状态的**取值域**。

    命名刻意与响应模型 :class:`app.modules.jobs.schemas.JobStatus` 区分开：
    两者同名会让 pydantic 在 OpenAPI 里生成
    ``app__modules__jobs__models__JobStatus`` 这类限定组件名，前端生成的类型会很难用。

    ``PENDING`` 为初始状态，终态为 ``SUCCEEDED`` / ``FAILED`` / ``CANCELLED``。
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobResourceType(str, enum.Enum):
    """任务关联的资源类型，与 ``JobType`` 一一对应。"""

    MATERIAL = "MATERIAL"
    PRACTICE_SET = "PRACTICE_SET"
    SUBMISSION = "SUBMISSION"


class Job(Base):
    """异步任务。"""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    type: Mapped[JobType] = mapped_column(
        SAEnum(JobType, name="job_type", native_enum=True), nullable=False
    )

    #: 初始状态为 PENDING，由 Worker 推进；第一版没有 Worker，因此不会变化
    status: Mapped[JobStatusValue] = mapped_column(
        SAEnum(JobStatusValue, name="job_status", native_enum=True),
        nullable=False,
        default=JobStatusValue.PENDING,
    )

    #: 0–100 的整数百分比；PENDING 为 0
    progress: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    resource_type: Mapped[JobResourceType] = mapped_column(
        SAEnum(JobResourceType, name="job_resource_type", native_enum=True),
        nullable=False,
    )

    #: 关联资源 ID。不建外键：资源类型不同，无法用单一外键表达
    resource_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    #: 失败原因的安全摘要；非 FAILED 时为 NULL
    error: Mapped[str | None] = mapped_column(
        String(JOB_ERROR_MAX_LENGTH), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        # 同一资源上同类任务只有一条：重复触发解析不会创建第二个任务
        UniqueConstraint("type", "resource_id", name="uq_jobs_type_resource_id"),
        Index("ix_jobs_resource_type_resource_id", "resource_type", "resource_id"),
        Index("ix_jobs_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<Job id={self.id} type={self.type.value} status={self.status.value}>"
