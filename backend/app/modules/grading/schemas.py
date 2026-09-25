"""Grading 请求/响应模型与纯校验（``docs/api-contract.md`` 第 9 节）。

设计要点（与第 8 节 Assignments 保持一致）：

- **严格类型**：文件名/类型/摘要用严格字符串，大小用严格整数；分数字段只接受
  JSON number（拒绝布尔、字符串、`NaN` 与 `Infinity`），解析时经 ``str()`` 转
  ``Decimal``，避免二进制浮点误差。
- **显式 ``null``**：写请求一律拒绝（`422 VALIDATION_ERROR`）。
- **结构校验在 Pydantic，业务规则在 service**：文件类型白名单、MIME 一致性、
  大小范围、sha256 格式等抛 ``422 UPLOAD_INVALID`` 并带稳定 ``details.reason``；
  "评分项必须恰好覆盖该提交引用的评分版本" 与分数上限由
  :func:`ensure_review_covers_rubric` 在服务层判定。
- **OpenAPI 与运行时一致**：分数声明为 JSON ``number`` 且 ``multipleOf: 0.01``；
  复核请求声明 ``minItems: 1``、``maxItems: 50`` 与必填 ``summary``/``items``。
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from app.core.errors import ValidationError
from app.core.time import UtcTimestamp
from app.modules.grading.models import (
    COMMENT_MAX_LENGTH,
    SCORE_PRECISION,
    SCORE_SCALE,
    SUMMARY_MAX_LENGTH,
    SubmissionStatus,
)
from app.modules.jobs.schemas import JobStatus

#: 扩展名（含点，比较时忽略大小写）→ 规范 MIME（契约 9.2 = 4.2 的子集）
CANONICAL_CONTENT_TYPE_BY_EXTENSION: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
}

#: 规范 MIME → 规范扩展名。对象键只使用这里的扩展名，不取用户文件名的写法
CANONICAL_EXTENSION_BY_CONTENT_TYPE: dict[str, str] = {
    content_type: extension
    for extension, content_type in CANONICAL_CONTENT_TYPE_BY_EXTENSION.items()
}

#: 实验报告支持的扩展名（**不接受旧版 .doc**）
SUPPORTED_EXTENSIONS = frozenset(CANONICAL_CONTENT_TYPE_BY_EXTENSION)

#: sha256 的十六进制长度
SHA256_HEX_LENGTH = 64

#: 复核评分项数量边界（与评分规则的上限一致）
MIN_REVIEW_ITEMS = 1
MAX_REVIEW_ITEMS = 50

#: 分数的最小单位（分）
SCORE_STEP = 10.0 ** -SCORE_SCALE

#: 分数在 OpenAPI 中的声明：只有 JSON number，且以分为最小单位。
#: 上界由运行时按评分项的 ``max_score`` 动态判定，因此这里只声明下界。
_SCORE_JSON_SCHEMA: dict = {
    "type": "number",
    "minimum": 0,
    "multipleOf": SCORE_STEP,
    "description": (
        f"不小于 0、最多 {SCORE_SCALE} 位小数（{SCORE_STEP} 的整数倍），"
        "且不得超过该评分项的满分；只接受 JSON number"
        "（拒绝布尔、字符串、NaN 与 Infinity）"
    ),
}

#: 分数：Python 侧是 ``Decimal``，JSON 侧只有 number
ScoreValue = Annotated[Decimal, WithJsonSchema(_SCORE_JSON_SCHEMA, mode="validation")]

#: 分数范围与精度的字段约束（与库列精度一致）
_SCORE_CONSTRAINTS: dict = {
    "ge": 0,
    "max_digits": SCORE_PRECISION,
    "decimal_places": SCORE_SCALE,
}

_SUMMARY_DESCRIPTION = f"去除首尾空白后必须是 1–{SUMMARY_MAX_LENGTH} 个字符"


def _errors(message: str, field: str = "body") -> dict:
    return {"errors": [{"field": field, "message": message}]}


def _decimal_from_json(value: object) -> Decimal:
    """把 JSON number 转成 ``Decimal``（拒绝布尔、字符串、NaN 与无穷值）。"""
    if isinstance(value, bool):
        raise ValueError("分数字段必须是 JSON number，不接受布尔值")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("分数字段不接受 NaN 或 Infinity")
        return Decimal(str(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("分数字段不接受 NaN 或 Infinity")
        return value
    raise ValueError("分数字段必须是 JSON number")


class _StrictRequest(BaseModel):
    """拒绝未声明字段、拒绝显式 ``null`` 的请求基类（契约 9.1）。"""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: object) -> object:
        if isinstance(data, dict):
            null_fields = sorted(str(key) for key, value in data.items() if value is None)
            if null_fields:
                raise ValueError(f"字段不能为 null：{'、'.join(null_fields)}")
        return data


class SubmissionUploadInitRequest(_StrictRequest):
    """初始化报告上传请求（契约 9.2）。

    只声明类型与必填；长度、白名单、MIME 一致性、大小范围与 sha256 格式由
    service 判定并返回 ``UPLOAD_INVALID``，避免被框架统一降级成
    ``VALIDATION_ERROR``。
    """

    filename: StrictStr
    content_type: StrictStr
    size: StrictInt
    sha256: StrictStr


class GradeItemReviewRequest(_StrictRequest):
    """复核请求中的单个评分项（契约 9.8）。"""

    rubric_item_id: uuid.UUID
    final_score: ScoreValue = Field(**_SCORE_CONSTRAINTS)
    teacher_comment: StrictStr = Field(default="", max_length=COMMENT_MAX_LENGTH)

    @field_validator("final_score", mode="before")
    @classmethod
    def _score(cls, value: object) -> Decimal:
        return _decimal_from_json(value)


class GradeReviewUpdateRequest(_StrictRequest):
    """教师复核请求（契约 9.8）：完整快照，summary 与 items 都必填。"""

    summary: StrictStr = Field(min_length=1, max_length=SUMMARY_MAX_LENGTH)
    items: list[GradeItemReviewRequest] = Field(
        min_length=MIN_REVIEW_ITEMS, max_length=MAX_REVIEW_ITEMS
    )

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, value: object) -> object:
        """摘要：先去除首尾空白，再检查 1–20,000 字符（空字符串返回 422）。"""
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not 1 <= len(stripped) <= SUMMARY_MAX_LENGTH:
            raise ValueError(f"摘要{_SUMMARY_DESCRIPTION}")
        return stripped

    @model_validator(mode="after")
    def _check_unique_items(self) -> GradeReviewUpdateRequest:
        ids = [item.rubric_item_id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("items 中的 rubric_item_id 不能重复")
        return self


# --------------------------------------------------------------------------- #
# 响应（契约 9.10）
# --------------------------------------------------------------------------- #
class SubmissionSummarySchema(BaseModel):
    """提交摘要（列表用）。"""

    id: uuid.UUID
    assignment_id: uuid.UUID
    course_id: uuid.UUID
    student_id: uuid.UUID
    status: SubmissionStatus
    filename: str
    content_type: str
    size: int
    is_late: bool
    #: 提交固定的评分规则版本号；未提交时为 null
    rubric_version: int | None
    submitted_at: UtcTimestamp | None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


class SubmissionDetailSchema(SubmissionSummarySchema):
    """提交详情：摘要 + 内容摘要 + 临时下载地址。"""

    sha256: str
    #: 预签名 GET 地址；未完成提交时为 null
    download_url: str | None
    download_expires_at: UtcTimestamp | None


class SubmissionUploadInitSchema(BaseModel):
    """初始化上传响应（契约 9.2）。"""

    submission_id: uuid.UUID
    upload_id: uuid.UUID
    upload_url: str
    method: str
    headers: dict[str, str]
    #: PUT 地址到期时间
    expires_at: UtcTimestamp
    #: 完成确认截止时间
    confirm_deadline_at: UtcTimestamp


class GradeItemDetailSchema(BaseModel):
    """批改后的单个评分项（契约 9.10）。"""

    id: uuid.UUID
    rubric_item_id: uuid.UUID
    order: int
    title: str
    max_score: float
    #: AI 建议分；学生已发布视角为 null
    ai_score: float | None
    final_score: float
    #: AI 判断说明；学生已发布视角为 null
    ai_comment: str | None
    #: 证据定位：摘录 + 来源类型（PDF_PAGE / DOCX_PARAGRAPH）+ 位置区间（从 1 开始）。
    #: 服务端已核对非空摘录确实出现在该区间内；零分且完全缺失时可为空。
    evidence_quote: str | None
    #: 证据来源类型：PDF 页码或 DOCX 段落（服务端校验时确定）
    evidence_source_type: Literal["PDF_PAGE", "DOCX_PARAGRAPH"] | None
    #: 证据位置区间（从 1 开始；两个端点都是实际提取单元）
    evidence_location_start: Annotated[int, Field(ge=1)] | None
    evidence_location_end: Annotated[int, Field(ge=1)] | None
    error_type: str
    improvement_suggestion: str
    teacher_comment: str


class GradeReviewDetailSchema(BaseModel):
    """批改详情（契约 9.10）。"""

    id: uuid.UUID
    submission_id: uuid.UUID
    #: AI 原始摘要；学生已发布视角为 null
    ai_summary: str | None
    teacher_summary: str
    #: AI 建议总分；学生已发布视角为 null
    suggested_total_score: float | None
    final_total_score: float
    items: list[GradeItemDetailSchema]
    reviewed_by: uuid.UUID | None
    reviewed_at: UtcTimestamp | None
    published_at: UtcTimestamp | None


__all__ = [
    "CANONICAL_CONTENT_TYPE_BY_EXTENSION",
    "CANONICAL_EXTENSION_BY_CONTENT_TYPE",
    "GradeItemDetailSchema",
    "GradeItemReviewRequest",
    "GradeReviewDetailSchema",
    "GradeReviewUpdateRequest",
    "JobStatus",
    "MAX_REVIEW_ITEMS",
    "MIN_REVIEW_ITEMS",
    "SHA256_HEX_LENGTH",
    "SUPPORTED_EXTENSIONS",
    "ScoreValue",
    "SubmissionDetailSchema",
    "SubmissionSummarySchema",
    "SubmissionUploadInitRequest",
    "SubmissionUploadInitSchema",
    "ensure_review_covers_rubric",
]


def ensure_review_covers_rubric(
    submitted_ids: Sequence[uuid.UUID],
    rubric_item_ids: Sequence[uuid.UUID],
    max_scores: dict[uuid.UUID, Decimal],
    submitted_scores: dict[uuid.UUID, Decimal],
) -> None:
    """复核请求必须**恰好覆盖**提交所引用评分版本的全部评分项（契约 9.8）。

    :raises ValidationError: 缺失、多余、重复或分数超出满分/为负（统一 422）。
    """
    expected = set(rubric_item_ids)
    if len(submitted_ids) != len(set(submitted_ids)):
        raise ValidationError(
            "评分项不能重复", details=_errors("items 中的 rubric_item_id 重复")
        )
    submitted = set(submitted_ids)
    missing = expected - submitted
    if missing:
        raise ValidationError(
            "复核必须覆盖全部评分项",
            details={
                "errors": [
                    {"field": "items", "message": "缺少评分项", "missing": str(item_id)}
                    for item_id in sorted(missing)
                ]
            },
        )
    unknown = submitted - expected
    if unknown:
        raise ValidationError(
            "复核包含不属于该提交评分版本的评分项",
            details={
                "errors": [
                    {
                        "field": "items",
                        "message": "评分项不属于该提交引用的评分版本",
                        "unknown": str(item_id),
                    }
                    for item_id in sorted(unknown)
                ]
            },
        )

    for item_id, score in submitted_scores.items():
        limit = max_scores[item_id]
        if score < 0 or score > limit:
            raise ValidationError(
                "评分超出该评分项的满分范围",
                details={
                    "errors": [
                        {
                            "field": "final_score",
                            "message": f"必须在 0 到 {limit} 之间",
                            "rubric_item_id": str(item_id),
                        }
                    ]
                },
            )
