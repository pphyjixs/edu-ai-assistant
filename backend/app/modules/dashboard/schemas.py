"""Dashboard 响应模型（``docs/api-contract.md`` 第 11 节）。

Dashboard 是跨模块只读聚合：每个接口固定返回首屏摘要与最近 5 项记录，
不提供分页或查询参数。所有时间字段都是 UTC ``date-time``（以 ``Z`` 结尾）；
分数是 JSON number，最多两位小数。

状态字段**不复用完整的领域枚举**：聚合结果只可能出现其中一部分取值，因此用基于现有
领域枚举成员的受限 ``Literal`` 收窄响应契约（``UPLOADING`` 等不会出现在 Dashboard 里），
既保证 OpenAPI 枚举与契约 §11 完全一致，也不在此处复制状态机。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PlainSerializer, WithJsonSchema

from app.core.time import isoformat_z
from app.modules.grading.models import SubmissionStatus
from app.modules.materials.models import MaterialStatus

#: 教师「最近提交」允许的状态（契约 11.1）：正式提交中可能出现的五种，
#: **排除**未完成上传的 ``UPLOADING``。成员取自 ``SubmissionStatus``。
TeacherSubmissionStatus = Literal[
    SubmissionStatus.SUBMITTED,
    SubmissionStatus.GRADING,
    SubmissionStatus.REVIEW_REQUIRED,
    SubmissionStatus.PUBLISHED,
    SubmissionStatus.FAILED,
]

#: 学生「资料处理状态」允许的状态（契约 11.2）：只含处理中与失败。
#: 成员取自 ``MaterialStatus``，**排除** ``UPLOADING`` / ``UPLOADED`` / ``READY``。
StudentMaterialProcessingStatus = Literal[
    MaterialStatus.PROCESSING,
    MaterialStatus.FAILED,
]

#: 每个列表固定返回的最近记录条数上限（契约 11）
RECENT_ITEM_LIMIT = 5

#: Dashboard 时间字段：UTC ISO 8601，并在 OpenAPI 声明 ``format: date-time``
#: （``PlainSerializer`` 只描述序列化后的字符串，需显式补上 format）。
UtcTimestampWithFormat = Annotated[
    datetime,
    PlainSerializer(isoformat_z, return_type=str),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]

#: 分数的最小单位（分），与评分规则/提交的 ``Numeric(10, 2)`` 精度一致
SCORE_STEP = 0.01

#: 分数在 OpenAPI 中的声明：JSON number、非负、最多两位小数
_SCORE_JSON_SCHEMA: dict = {
    "type": "number",
    "minimum": 0,
    "multipleOf": SCORE_STEP,
    "description": f"总分（最多两位小数，{SCORE_STEP} 的整数倍）；只接受 JSON number",
}

#: 响应分数：服务层由 ``Decimal`` 转成 ``float``，OpenAPI 声明为带两位小数的 number
ScoreNumber = Annotated[
    float, WithJsonSchema(_SCORE_JSON_SCHEMA, mode="serialization")
]


class TeacherRecentSubmission(BaseModel):
    """教师视角最近一份正式提交。"""

    submission_id: uuid.UUID
    assignment_id: uuid.UUID
    course_id: uuid.UUID
    #: 提交学生的标识与显示名
    student_id: uuid.UUID
    student_name: str
    #: 正式提交可能出现的状态；不含 UPLOADING
    status: TeacherSubmissionStatus
    #: 是否补交
    is_late: bool
    submitted_at: UtcTimestampWithFormat


class TeacherFailedMaterial(BaseModel):
    """教师视角最近一份失败资料。"""

    material_id: uuid.UUID
    course_id: uuid.UUID
    filename: str
    #: 可安全展示的失败原因摘要；非 FAILED 时为 null
    error_message: str | None
    updated_at: UtcTimestampWithFormat


class TeacherDashboard(BaseModel):
    """教师工作台摘要（契约 11.1）。"""

    active_course_count: int
    pending_grading_count: int
    failed_material_count: int
    recent_submissions: list[TeacherRecentSubmission] = Field(
        max_length=RECENT_ITEM_LIMIT
    )
    failed_materials: list[TeacherFailedMaterial] = Field(
        max_length=RECENT_ITEM_LIMIT
    )


class StudentPendingAssignment(BaseModel):
    """学生视角一个待完成任务。"""

    assignment_id: uuid.UUID
    course_id: uuid.UUID
    title: str
    #: 截止时间；无截止为 null
    due_at: UtcTimestampWithFormat | None
    allow_late_submission: bool
    #: 发布时间；发布任务必有值，这里仍按可空建模以对齐源列
    published_at: UtcTimestampWithFormat | None


class StudentRecentFeedback(BaseModel):
    """学生视角最近一条已发布反馈。"""

    review_id: uuid.UUID
    submission_id: uuid.UUID
    assignment_id: uuid.UUID
    course_id: uuid.UUID
    #: 教师最终总分
    final_total_score: ScoreNumber
    #: 提交固定评分版本的总分
    total_score: ScoreNumber
    published_at: UtcTimestampWithFormat


class StudentMaterialStatus(BaseModel):
    """学生视角一份处理中或失败资料。"""

    material_id: uuid.UUID
    course_id: uuid.UUID
    filename: str
    #: 仅 ``PROCESSING`` 或 ``FAILED``
    status: StudentMaterialProcessingStatus
    #: 可安全展示的失败原因摘要；非 FAILED 时为 null
    error_message: str | None
    updated_at: UtcTimestampWithFormat


class StudentDashboard(BaseModel):
    """学生工作台摘要（契约 11.2）。"""

    active_course_count: int
    pending_assignment_count: int
    processing_material_count: int
    failed_material_count: int
    pending_assignments: list[StudentPendingAssignment] = Field(
        max_length=RECENT_ITEM_LIMIT
    )
    recent_feedback: list[StudentRecentFeedback] = Field(
        max_length=RECENT_ITEM_LIMIT
    )
    material_statuses: list[StudentMaterialStatus] = Field(
        max_length=RECENT_ITEM_LIMIT
    )


__all__ = [
    "RECENT_ITEM_LIMIT",
    "ScoreNumber",
    "StudentDashboard",
    "StudentMaterialProcessingStatus",
    "StudentMaterialStatus",
    "StudentPendingAssignment",
    "StudentRecentFeedback",
    "TeacherDashboard",
    "TeacherFailedMaterial",
    "TeacherRecentSubmission",
    "TeacherSubmissionStatus",
    "UtcTimestampWithFormat",
]
