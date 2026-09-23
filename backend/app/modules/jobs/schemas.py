"""Jobs 模块响应模型。

``JobStatus`` 的字段与 ``docs/api-contract.md`` 第 10 节逐条对应，
课件上传的完成响应（契约 4.7）复用同一结构，避免同一份业务事实出现两种形状。
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

from app.core.time import UtcTimestamp
from app.modules.jobs.models import JobResourceType, JobStatusValue, JobType


class JobStatus(BaseModel):
    """任务状态（契约 4.7 / 第 10 节）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: JobType
    status: JobStatusValue
    #: 0–100；PENDING 为 0
    progress: int
    resource_type: JobResourceType
    resource_id: uuid.UUID
    #: 失败原因的安全摘要；非 FAILED 时为 null
    error: str | None
    #: 失败阶段码（``DOWNLOAD`` / ``NATIVE_EXTRACT`` / ``OUTLINE_GENERATION`` …）；
    #: 非 FAILED 时为 null。前端据此区分「文件读不出来」与「模型侧失败」。
    failure_stage: str | None = None
    created_at: UtcTimestamp
    started_at: UtcTimestamp | None
    finished_at: UtcTimestamp | None
