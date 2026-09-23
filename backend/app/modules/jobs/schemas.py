"""Jobs 模块响应模型。

``JobStatus`` 的字段与 ``docs/api-contract.md`` 第 10 节逐条对应，
课件上传的完成响应（契约 4.7）复用同一结构，避免同一份业务事实出现两种形状。

**公开枚举与内部枚举分离**（契约 10.0）：

- ORM 的 :class:`~app.modules.jobs.models.JobType` / ``JobResourceType`` 是**内部**枚举，
  含 ``AGENT_RUN``，由 Agent 模块与各 Worker 使用；
- 本模块定义**公开**枚举 ``JobType`` / ``JobResourceType``，只含三类通用任务
  （`MATERIAL_PARSE` / `PRACTICE_GENERATE` / `SUBMISSION_GRADE`），
  组件名保持 ``JobType`` / ``JobResourceType``，生成类型名称不变；
- 内部枚举在本模块统一以 ``DbJobType`` / ``DbJobResourceType`` 别名引用，避免混淆。

``AGENT_RUN`` 不属于公开范围：服务层在读取任务后先拒绝它（``404``），
因此不会走到响应校验；若真的走到，公开枚举校验会失败而不是静默通过。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    field_validator,
)

from app.core.time import isoformat_z
from app.modules.jobs.models import JOB_ERROR_MAX_LENGTH, JobStatusValue
from app.modules.jobs.models import JobResourceType as DbJobResourceType
from app.modules.jobs.models import JobType as DbJobType

#: JobStatus 的时间字段：UTC ISO 8601，并在 OpenAPI 声明 ``format: date-time``。
#: （``PlainSerializer`` 只描述序列化后的字符串，需显式补上 format。）
UtcTimestampWithFormat = Annotated[
    datetime,
    PlainSerializer(isoformat_z, return_type=str),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]


class JobType(str, enum.Enum):
    """**公开**任务类型（契约 10.0）：通用 Jobs 接口只服务这三类。"""

    MATERIAL_PARSE = "MATERIAL_PARSE"
    PRACTICE_GENERATE = "PRACTICE_GENERATE"
    SUBMISSION_GRADE = "SUBMISSION_GRADE"


class JobResourceType(str, enum.Enum):
    """**公开**资源类型，与 :class:`JobType` 一一对应。"""

    MATERIAL = "MATERIAL"
    PRACTICE_SET = "PRACTICE_SET"
    SUBMISSION = "SUBMISSION"


#: 公开任务类型 → 公开资源类型的固定配对（契约 10.0 的表格）
PUBLIC_RESOURCE_TYPE_BY_JOB_TYPE: dict[JobType, JobResourceType] = {
    JobType.MATERIAL_PARSE: JobResourceType.MATERIAL,
    JobType.PRACTICE_GENERATE: JobResourceType.PRACTICE_SET,
    JobType.SUBMISSION_GRADE: JobResourceType.SUBMISSION,
}


def public_job_type(job_type: DbJobType) -> JobType | None:
    """把内部任务类型映射为公开任务类型；``AGENT_RUN`` 返回 ``None``。"""
    try:
        return JobType(job_type.value)
    except ValueError:
        return None


def is_public_job(job_type: DbJobType, resource_type: DbJobResourceType) -> bool:
    """任务是否属于公开 Jobs 范围：类型公开且资源类型与其正确配对。"""
    public = public_job_type(job_type)
    if public is None:
        return False
    return PUBLIC_RESOURCE_TYPE_BY_JOB_TYPE[public].value == resource_type.value


class JobStatus(BaseModel):
    """任务状态（契约 4.7 / 第 10 节）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: JobType
    status: JobStatusValue
    #: 0–100；PENDING 为 0，SUCCEEDED 为 100
    progress: Annotated[int, Field(ge=0, le=100)]
    resource_type: JobResourceType
    resource_id: uuid.UUID
    #: 失败原因的安全摘要；非 FAILED 时为 null，最长 500 字符
    error: Annotated[str | None, Field(max_length=JOB_ERROR_MAX_LENGTH)]
    created_at: UtcTimestampWithFormat
    started_at: UtcTimestampWithFormat | None
    finished_at: UtcTimestampWithFormat | None

    @field_validator("type", "resource_type", mode="before")
    @classmethod
    def _normalize_internal_enum(cls, value: object) -> object:
        """把 ORM 的内部枚举归一化为其字符串取值，再交给公开枚举校验。

        ``AGENT_RUN`` 会在这里校验失败（服务层已先行返回 404，属于防御性兜底）。
        """
        return getattr(value, "value", value)


__all__ = [
    "PUBLIC_RESOURCE_TYPE_BY_JOB_TYPE",
    "JobResourceType",
    "JobStatus",
    "JobType",
    "UtcTimestampWithFormat",
    "is_public_job",
    "public_job_type",
]
