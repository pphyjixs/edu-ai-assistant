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

from sqlalchemy import CheckConstraint, Index, Integer, String, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.core.time import utc_now
from app.db.base import Base
from app.db.types import UtcDateTime

#: 失败原因列长度：只保存可安全展示的摘要，不保存堆栈
JOB_ERROR_MAX_LENGTH = 500

#: 失败阶段列的取值域（**不是**原生枚举：阶段码会随流水线演进增加，
#: 用字符串列可以让新增阶段不必发迁移）。取值见 :class:`JobFailureStage`。
FAILURE_STAGE_MAX_LENGTH = 32


class JobFailureStage(str, enum.Enum):
    """任务失败发生在哪个阶段（评审文档「一、#4.8 / #9」）。

    只用于**展示与排错**：前端据此区分「文件本身读不出来」「模型侧失败」
    「服务不可用」，而不是把三种情况都画成同一句失败原因。
    """

    #: 从对象存储读取/校验原文件
    DOWNLOAD = "DOWNLOAD"
    #: 原生文本提取与切块
    NATIVE_EXTRACT = "NATIVE_EXTRACT"
    #: 页面渲染为图片（PDF/PPTX 视觉解析路径）
    RENDER = "RENDER"
    #: 视觉识别（OCR / 图表 / 公式）
    VISION_OCR = "VISION_OCR"
    #: 大纲生成
    OUTLINE_GENERATION = "OUTLINE_GENERATION"
    #: 模型客户端构造或配置缺失
    MODEL_CALL = "MODEL_CALL"
    #: 结果发布（写库）
    PUBLISH = "PUBLISH"
    #: 未归类
    UNKNOWN = "UNKNOWN"

#: ``progress`` 的取值域约束（契约 10.0：0–100 的整数百分比）。
#: 迁移 ``0013_jobs_contract`` 使用**完全相同的表达式**，保证 ORM 元数据与库侧一致。
JOB_PROGRESS_CHECK = "progress BETWEEN 0 AND 100"

#: 任务类型与资源类型必须正确配对（契约 10.0）。
#: 含三类公开任务以及内部 ``AGENT_RUN``；未知组合在写入时即被数据库拒绝。
JOB_TYPE_RESOURCE_CHECK = (
    "(type = 'MATERIAL_PARSE' AND resource_type = 'MATERIAL')"
    " OR (type = 'PRACTICE_GENERATE' AND resource_type = 'PRACTICE_SET')"
    " OR (type = 'SUBMISSION_GRADE' AND resource_type = 'SUBMISSION')"
    " OR (type = 'AGENT_RUN' AND resource_type = 'AGENT_RUN')"
)


class JobType(str, enum.Enum):
    """任务类型。"""

    MATERIAL_PARSE = "MATERIAL_PARSE"
    PRACTICE_GENERATE = "PRACTICE_GENERATE"
    SUBMISSION_GRADE = "SUBMISSION_GRADE"
    #: 上下文 Agent Run（异步对话与动作编排，见 docs/local-development-agent-backend.md 第 6 节）
    AGENT_RUN = "AGENT_RUN"


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
    #: 关联 ``agent_runs.id``
    AGENT_RUN = "AGENT_RUN"


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

    #: 失败阶段码（``JobFailureStage`` 的取值）；非 FAILED 时为 NULL。
    #: 用字符串列而不是原生枚举：新增阶段不需要发迁移。
    failure_stage: Mapped[str | None] = mapped_column(
        String(FAILURE_STAGE_MAX_LENGTH), nullable=True
    )

    #: 执行尝试次数：Worker 每次原子领取 +1（契约 5.5 的重试语义）
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    #: 运行令牌：领取时生成，回写必须携带匹配值，
    #: 防止超出租约的过期 Worker 覆盖新一轮执行
    run_token: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: 租约到期时间：RUNNING 状态超过租约即视为执行者失联，
    #: 状态推进由重试解析接口兜底（契约 5.5 第 6 步）
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        # 同一资源上同类任务只有一条：重复触发解析不会创建第二个任务
        UniqueConstraint("type", "resource_id", name="uq_jobs_type_resource_id"),
        # 进度必须是 0–100 的整数（契约 10.0）
        CheckConstraint(JOB_PROGRESS_CHECK, name="ck_jobs_progress_range"),
        # 任务类型与资源类型必须配对（含内部 AGENT_RUN，契约 10.0）
        CheckConstraint(JOB_TYPE_RESOURCE_CHECK, name="ck_jobs_type_resource_match"),
        Index("ix_jobs_resource_type_resource_id", "resource_type", "resource_id"),
        Index("ix_jobs_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<Job id={self.id} type={self.type.value} status={self.status.value}>"
