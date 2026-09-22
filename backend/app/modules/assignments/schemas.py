"""Assignments 请求/响应模型与纯校验（``docs/api-contract.md`` 第 8 节）。

设计要点：

- **严格类型**：标题/说明用严格字符串，`allow_late_submission` 用严格布尔，
  `order` 用严格整数；分数字段只接受 JSON number（拒绝布尔与字符串），
  并在解析时把 ``float`` 经 ``str()`` 转成 ``Decimal``，避免二进制浮点误差。
- **显式 ``null``**：除 `due_at` 外一律拒绝（`due_at: null` 表示清除截止时间）。
- **标题先去除首尾空白再检查长度**（契约 8.2）：长度校验在 ``mode="before"``
  校验器里对 **strip 之后**的值执行，因此"首尾带空白、去除后恰好 200 字符"是
  合法输入。JSON Schema 只声明 ``string``——trim 规则无法用 JSON Schema 准确
  表达（用 ``maxLength`` 会拒掉运行时接受的带空白输入，用 ``pattern`` 会因
  ECMA-262 与 Python 的空白字符集不同而产生假拒绝），规则写在字段 ``description``。
- **OpenAPI 与运行时一致**：分数字段显式声明为 JSON ``number`` 且
  ``multipleOf: 0.01``（对应运行时的 ``decimal_places=2``；Pydantic 默认还会把
  ``Decimal`` 声明成 ``number | string``）；可省略字段显式去掉 ``null`` 分支
  与 ``default: null``（运行时对显式 `null` 返回 422）；修改请求声明
  ``minProperties: 1``（运行时拒绝空对象）。
- **总分校验在服务层**：字段结构校验通过后才比较"评分项之和 == 总分"，
  不匹配由 :func:`ensure_rubric_total_matches` 抛 ``RubricScoreMismatchError``
  （`422 RUBRIC_SCORE_MISMATCH`），保证契约 8.1 的错误优先级。
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from app.core.errors import RubricScoreMismatchError
from app.core.time import UtcTimestamp
from app.modules.assignments.models import (
    DESCRIPTION_MAX_LENGTH,
    MAX_RUBRIC_ITEMS,
    MAX_SCORE,
    MIN_RUBRIC_ITEMS,
    RUBRIC_ITEM_DESCRIPTION_MAX_LENGTH,
    SCORE_PRECISION,
    SCORE_SCALE,
    TITLE_MAX_LENGTH,
    AssignmentStatus,
)

#: 分数范围与精度的字段约束（与库列精度一致）
_SCORE_CONSTRAINTS: dict = {
    "gt": 0,
    "le": MAX_SCORE,
    "max_digits": SCORE_PRECISION,
    "decimal_places": SCORE_SCALE,
}

#: 允许的分数小数位由列精度决定（两位）
_SCORE_FIELD = Field(**_SCORE_CONSTRAINTS)

#: 分数的最小单位（分）：JSON Schema 里必须是 JSON number，
#: 位数由 :data:`~app.modules.assignments.models.SCORE_SCALE` 决定
SCORE_STEP = 10.0 ** -SCORE_SCALE

#: 分数在 OpenAPI 中的声明：**只有 JSON number**，且以分为最小单位。
#:
#: Pydantic 默认把 ``Decimal`` 声明成 ``number | string``（宽松模式接受数字字符串），
#: 而运行时通过 :func:`_decimal_from_json` 明确拒绝字符串、布尔、``NaN`` 与
#: ``Infinity``；两者必须一致，因此这里显式覆盖 JSON Schema。
#:
#: ``multipleOf`` 对应运行时的 ``decimal_places=2``（值必须是 0.01 的整数倍）。
#: 注意：用浮点做 ``multipleOf`` 判定的工具可能把它判错（``40.55 / 0.01``
#: 在二进制浮点下是 ``4054.999...``），需要用十进制比较或精度容差
#: （例如 AJV 的 ``multipleOfPrecision``）；服务端始终以 ``decimal_places=2`` 为准。
_SCORE_JSON_SCHEMA: dict = {
    "type": "number",
    "exclusiveMinimum": 0,
    # JSON Schema 里必须是 JSON number：``MAX_SCORE`` 是 ``Decimal``，
    # 用 ``float()`` 转换后边界值仍与运行时的 ``le=MAX_SCORE`` 一致
    "maximum": float(MAX_SCORE),
    "multipleOf": SCORE_STEP,
    "description": (
        f"正数，最多 {SCORE_SCALE} 位小数（{SCORE_STEP} 的整数倍）；只接受 JSON number"
        "（拒绝布尔、字符串、NaN 与 Infinity）"
    ),
}

#: 必填分数：Python 侧是 ``Decimal``，JSON 侧只有 number
ScoreValue = Annotated[Decimal, WithJsonSchema(_SCORE_JSON_SCHEMA, mode="validation")]

#: 可省略分数：与 :data:`ScoreValue` 相同，但允许省略
OptionalScoreValue = Annotated[
    Decimal | None, WithJsonSchema(_SCORE_JSON_SCHEMA, mode="validation")
]

#: 标题规则说明（JSON Schema 无法表达"先 trim 再判长度"）
_TITLE_DESCRIPTION = f"去除首尾空白后必须是 1–{TITLE_MAX_LENGTH} 个字符"

_ZULU_SUFFIX = re.compile(r"[zZ]$")


def _omitted() -> None:
    """可省略字段的哨兵默认值。

    用 ``default_factory`` 而不是 ``default=None``：后者会让 JSON Schema 生成
    ``default: null``，而运行时把显式 `null` 判为 ``422``，两者会自相矛盾。
    """
    return None


def _drop_null_branch(field_schema: dict) -> None:
    """去掉字段 JSON Schema 里的 ``null`` 分支。

    "可省略"与"可为 ``null``"是两件事：修改接口除 `due_at` 外**拒绝**显式 `null`，
    因此这些字段必须声明为**非可空**（``None`` 只是"未提供"的内部哨兵）。
    """
    branches = field_schema.get("anyOf")
    if not branches:
        return
    remaining = [branch for branch in branches if branch.get("type") != "null"]
    if len(remaining) == 1:
        field_schema.pop("anyOf")
        field_schema.update(remaining[0])


def _omittable(**constraints: object) -> Field:
    """可省略、但**不接受显式 ``null``** 的字段声明（可附加范围/精度约束）。"""
    return Field(
        default_factory=_omitted,
        json_schema_extra=_drop_null_branch,
        **constraints,
    )


def _trimmed_title(value: object) -> object:
    """标题：先去除首尾空白，再检查 1–200 字符（契约 8.2）。

    长度检查放在这里而不是 ``Field(min_length/max_length)``：Pydantic 的长度约束
    作用于**原始值**，会让"首尾带空白、去除后恰好 200 字符"的合法标题被拒。

    :raises ValueError: 去除空白后为空或超过 200 字符（统一转 ``422``）。
    """
    if not isinstance(value, str):
        return value  # 类型错误交给 StrictStr 报告
    stripped = value.strip()
    if not 1 <= len(stripped) <= TITLE_MAX_LENGTH:
        raise ValueError(f"标题{_TITLE_DESCRIPTION}")
    return stripped


def _decimal_from_json(value: object) -> Decimal:
    """把 JSON number 转成 ``Decimal``（拒绝布尔、字符串、NaN 与无穷值）。"""
    if isinstance(value, bool):
        raise ValueError("分数字段必须是 JSON number，不接受布尔值")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("分数字段不接受 NaN 或 Infinity")
        # 经 str() 转换，避免 0.1 这类二进制浮点误差进入 Decimal
        return Decimal(str(value))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("分数字段不接受 NaN 或 Infinity")
        return value
    raise ValueError("分数字段必须是 JSON number")


def _parse_aware_datetime(value: object) -> datetime:
    """解析带时区的 ISO 8601 时间并转为 UTC。"""
    if not isinstance(value, str):
        raise ValueError("due_at 必须是带时区的 ISO 8601 字符串")
    text = _ZULU_SUFFIX.sub("+00:00", value.strip())
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("due_at 不是合法的 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError("due_at 必须带时区（例如 2026-09-25T15:59:00Z）")
    return parsed.astimezone(timezone.utc)


class _StrictRequest(BaseModel):
    """拒绝未声明字段的请求基类。"""

    model_config = ConfigDict(extra="forbid")


class RubricItemRequest(_StrictRequest):
    """评分项请求（契约 8.2）。"""

    title: StrictStr = Field(description=_TITLE_DESCRIPTION)
    description: StrictStr = Field(default="", max_length=RUBRIC_ITEM_DESCRIPTION_MAX_LENGTH)
    max_score: ScoreValue = _SCORE_FIELD
    order: StrictInt = Field(ge=1)

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, value: object) -> object:
        return _trimmed_title(value)

    @field_validator("max_score", mode="before")
    @classmethod
    def _score(cls, value: object) -> Decimal:
        return _decimal_from_json(value)


class _RubricItemsMixin(BaseModel):
    """共享的评分项顺序校验：从 1 开始、连续且不重复。"""

    @staticmethod
    def _validate_orders(items: Sequence[RubricItemRequest]) -> None:
        if not (MIN_RUBRIC_ITEMS <= len(items) <= MAX_RUBRIC_ITEMS):
            raise ValueError(
                f"rubric_items 必须包含 {MIN_RUBRIC_ITEMS}–{MAX_RUBRIC_ITEMS} 项"
            )
        orders = sorted(item.order for item in items)
        expected = list(range(1, len(items) + 1))
        if orders != expected:
            raise ValueError("rubric_items 的 order 必须从 1 开始且连续、不重复")


class AssignmentCreateRequest(_RubricItemsMixin, _StrictRequest):
    """创建任务请求（契约 8.2）。"""

    title: StrictStr = Field(description=_TITLE_DESCRIPTION)
    description: StrictStr = Field(default="", max_length=DESCRIPTION_MAX_LENGTH)
    total_score: ScoreValue = _SCORE_FIELD
    #: 省略或 null 都表示不限时间（唯一允许显式 null 的字段）
    due_at: datetime | None = None
    allow_late_submission: StrictBool = False
    #: 1–50 项（与 :meth:`_RubricItemsMixin._validate_orders` 的运行时规则一致）
    rubric_items: list[RubricItemRequest] = Field(
        min_length=MIN_RUBRIC_ITEMS, max_length=MAX_RUBRIC_ITEMS
    )

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, value: object) -> object:
        return _trimmed_title(value)

    @field_validator("total_score", mode="before")
    @classmethod
    def _total_score(cls, value: object) -> Decimal:
        return _decimal_from_json(value)

    @field_validator("due_at", mode="before")
    @classmethod
    def _due_at(cls, value: object) -> datetime | None:
        if value is None:
            return None
        return _parse_aware_datetime(value)

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: object) -> object:
        if isinstance(data, dict):
            for key, value in data.items():
                if value is None and key != "due_at":
                    raise ValueError(f"字段 {key} 不接受 null")
        return data

    @model_validator(mode="after")
    def _check_rubric(self) -> AssignmentCreateRequest:
        self._validate_orders(self.rubric_items)
        return self


class AssignmentUpdateRequest(_RubricItemsMixin, _StrictRequest):
    """修改任务请求（契约 8.5）：全部字段可省略，至少提供一个。

    除 `due_at` 外字段都**不接受显式 ``null``**（省略表示"不修改"，
    `due_at: null` 表示"清除截止时间"），因此 JSON Schema 里它们都不是可空类型，
    整个对象声明 ``minProperties: 1``（空对象 `{}` 返回 422）。
    """

    model_config = ConfigDict(extra="forbid", json_schema_extra={"minProperties": 1})

    title: StrictStr | None = _omittable(description=_TITLE_DESCRIPTION)
    description: StrictStr | None = _omittable(max_length=DESCRIPTION_MAX_LENGTH)
    #: 与创建一致：正数、最多两位小数（字段级校验，早于总分一致性校验）
    total_score: OptionalScoreValue = _omittable(**_SCORE_CONSTRAINTS)
    #: 出现且为 null 表示**清除**截止时间；省略表示保持不变
    due_at: datetime | None = None
    allow_late_submission: StrictBool | None = _omittable()
    #: 出现时表示**完整替换**当前评分规则（数量边界与创建一致）
    rubric_items: list[RubricItemRequest] | None = _omittable(
        min_length=MIN_RUBRIC_ITEMS, max_length=MAX_RUBRIC_ITEMS
    )

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, value: object) -> object:
        return _trimmed_title(value)

    @field_validator("total_score", mode="before")
    @classmethod
    def _total_score(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        return _decimal_from_json(value)

    @field_validator("due_at", mode="before")
    @classmethod
    def _due_at(cls, value: object) -> datetime | None:
        if value is None:
            return None
        return _parse_aware_datetime(value)

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: object) -> object:
        if isinstance(data, dict):
            for key, value in data.items():
                if value is None and key != "due_at":
                    raise ValueError(f"字段 {key} 不接受 null")
            if not data:
                raise ValueError("修改请求至少要提供一个字段")
        return data

    @model_validator(mode="after")
    def _check_rubric(self) -> AssignmentUpdateRequest:
        if self.rubric_items is not None:
            self._validate_orders(self.rubric_items)
        return self


# --------------------------------------------------------------------------- #
# 响应（契约 8.8）
# --------------------------------------------------------------------------- #
class RubricItemSchema(BaseModel):
    """评分项响应。"""

    id: uuid.UUID
    title: str
    description: str
    max_score: float
    order: int


class AssignmentSummarySchema(BaseModel):
    """任务摘要（列表用，不含说明与评分项）。"""

    id: uuid.UUID
    course_id: uuid.UUID
    title: str
    total_score: float
    due_at: UtcTimestamp | None
    allow_late_submission: bool
    status: AssignmentStatus
    rubric_version: int
    published_at: UtcTimestamp | None
    closed_at: UtcTimestamp | None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


class AssignmentDetailSchema(AssignmentSummarySchema):
    """任务详情：摘要 + 说明 + 按 ``order`` 升序的当前评分项。"""

    description: str
    rubric_items: list[RubricItemSchema]


# --------------------------------------------------------------------------- #
# 纯校验：总分一致性与评分规则"是否实际变化"
# --------------------------------------------------------------------------- #
def ensure_rubric_total_matches(
    total_score: Decimal, max_scores: Sequence[Decimal]
) -> None:
    """评分项分值之和必须**精确等于**总分（``Decimal`` 比较）。

    :raises RubricScoreMismatchError: 不匹配（`422 RUBRIC_SCORE_MISMATCH`）。
    """
    expected = Decimal(total_score)
    actual = sum((Decimal(score) for score in max_scores), Decimal("0"))
    if actual != expected:
        raise RubricScoreMismatchError(
            details={
                "total_score": str(expected),
                "rubric_items_total": str(actual),
            }
        )


def rubric_fingerprint(
    total_score: Decimal,
    items: Sequence[tuple[str, str, Decimal, int]],
) -> tuple[Decimal, tuple[tuple[str, str, Decimal, int], ...]]:
    """评分规则的比较指纹：总分 + 按 ``order`` 排序的 ``(标题, 说明, 分值, 顺序)``。

    用于判断一次修改是否产生了**实质的评分规则变化**：指纹相同表示提交内容
    与当前版本一致，不需要生成新版本。
    """
    ordered = tuple(
        (title, description, Decimal(max_score), int(order))
        for title, description, max_score, order in sorted(
            items, key=lambda row: row[3]
        )
    )
    return (Decimal(total_score), ordered)


__all__ = [
    "AssignmentCreateRequest",
    "AssignmentDetailSchema",
    "AssignmentSummarySchema",
    "AssignmentUpdateRequest",
    "RubricItemRequest",
    "RubricItemSchema",
    "ensure_rubric_total_matches",
    "rubric_fingerprint",
]
