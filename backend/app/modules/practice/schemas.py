"""Practice 模块的请求/响应模型与模型输出校验（``docs/api-contract.md`` 第 7 节）。

请求体一律拒绝未声明字段与显式 ``null``（``extra="forbid"`` + before 校验器），
业务边界（数量、题型组合、答案覆盖）在 Pydantic 层给出确定结果并统一
转成 ``422 VALIDATION_ERROR``。

**严格类型**（契约 7.2 / 7.6）：``question_count`` 只接受 JSON 整数
（``true`` / ``"3"`` / ``3.0`` 都是 422）；作答值只接受 JSON 字符串或
JSON 布尔值（``0`` / ``1`` 不会被当作布尔值）。模型输出中的正确项下标与
判断题答案同样严格，避免错误输出被静默转换。

模型输出（:class:`GeneratedPractice`）只描述**结构**；业务配额、选项唯一性、
来源片段与摘录的核对在 :mod:`app.modules.practice.generation_ai` 中完成。
"""

from __future__ import annotations

import uuid

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.core.time import UtcTimestamp
from app.modules.practice.models import (
    MAX_MATERIALS,
    MAX_QUESTIONS,
    MIN_MATERIALS,
    MIN_QUESTIONS,
    SHORT_ANSWER_MAX_LENGTH,
    PracticeDifficulty,
    PracticeQuestionType,
    PracticeStatus,
)


class _StrictRequest(BaseModel):
    """拒绝未声明字段与显式 ``null`` 的请求基类。"""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: object) -> object:
        if isinstance(data, dict):
            for key, value in data.items():
                if value is None:
                    raise ValueError(f"字段 {key} 不接受 null")
        return data


class PracticeGenerateRequest(_StrictRequest):
    """生成练习请求（契约 7.2）。"""

    material_ids: list[uuid.UUID]
    #: 只接受 JSON 整数：``true``、``"3"``、``3.0`` 都返回 422
    question_count: StrictInt
    question_types: list[PracticeQuestionType]
    difficulty: PracticeDifficulty

    @field_validator("material_ids")
    @classmethod
    def _validate_materials(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        if not (MIN_MATERIALS <= len(value) <= MAX_MATERIALS):
            raise ValueError(
                f"material_ids 必须包含 {MIN_MATERIALS}–{MAX_MATERIALS} 个资料"
            )
        if len(set(value)) != len(value):
            raise ValueError("material_ids 不能重复")
        return value

    @field_validator("question_types")
    @classmethod
    def _validate_types(
        cls, value: list[PracticeQuestionType]
    ) -> list[PracticeQuestionType]:
        if not value:
            raise ValueError("question_types 不能为空")
        if len(set(value)) != len(value):
            raise ValueError("question_types 不能重复")
        return value

    @model_validator(mode="after")
    def _validate_count(self) -> PracticeGenerateRequest:
        if not (MIN_QUESTIONS <= self.question_count <= MAX_QUESTIONS):
            raise ValueError(f"question_count 必须在 {MIN_QUESTIONS}–{MAX_QUESTIONS} 之间")
        if self.question_count < len(self.question_types):
            raise ValueError("question_count 不能少于题型数量（每种题型至少 1 题）")
        return self


class PracticeAttemptAnswerRequest(BaseModel):
    """单题提交（契约 7.6）：单选为选项 ID、判断为布尔、简答为文本。

    类型严格：只接受 JSON 字符串或 JSON 布尔值，``0`` / ``1`` 不会被转成布尔值。
    """

    model_config = ConfigDict(extra="forbid")

    question_id: uuid.UUID
    answer: StrictStr | StrictBool

    @field_validator("answer")
    @classmethod
    def _validate_answer(cls, value: StrictStr | StrictBool) -> StrictStr | StrictBool:
        if isinstance(value, bool):
            return value
        if len(value) > SHORT_ANSWER_MAX_LENGTH:
            raise ValueError(f"简答答案不能超过 {SHORT_ANSWER_MAX_LENGTH} 个字符")
        return value


class PracticeAttemptSubmitRequest(_StrictRequest):
    """提交答案请求（契约 7.6）。"""

    answers: list[PracticeAttemptAnswerRequest]

    @field_validator("answers")
    @classmethod
    def _validate_answers(
        cls, value: list[PracticeAttemptAnswerRequest]
    ) -> list[PracticeAttemptAnswerRequest]:
        if not value:
            raise ValueError("answers 不能为空")
        ids = [item.question_id for item in value]
        if len(set(ids)) != len(ids):
            raise ValueError("answers 中同一题目不能重复")
        return value


# --------------------------------------------------------------------------- #
# 响应（契约 7.8）
# --------------------------------------------------------------------------- #
class PracticeOptionSchema(BaseModel):
    """单选选项：ID 由服务端生成。"""

    id: str
    text: str


class PracticeGradingPointSchema(BaseModel):
    """简答题评分要点（教师可见）。"""

    point: str
    accepted: list[str]


class PracticeQuestionSchema(BaseModel):
    """题目。教师专有字段在学生视角下为 ``null`` / 空数组。"""

    id: uuid.UUID
    order: int
    type: PracticeQuestionType
    prompt: str
    options: list[PracticeOptionSchema]
    knowledge_point: str | None
    correct_answer: str | bool | None = None
    grading_points: list[PracticeGradingPointSchema] = Field(default_factory=list)
    explanation: str | None = None


class PracticeSetSummarySchema(BaseModel):
    """练习集摘要（列表用，不含题目）。"""

    id: uuid.UUID
    course_id: uuid.UUID
    title: str
    status: PracticeStatus
    difficulty: PracticeDifficulty
    question_count: int
    question_types: list[PracticeQuestionType]
    created_at: UtcTimestamp
    updated_at: UtcTimestamp
    published_at: UtcTimestamp | None


class PracticeSetSchema(PracticeSetSummarySchema):
    """练习详情：含按 ``order`` 升序的题目。"""

    questions: list[PracticeQuestionSchema]


class PracticeAttemptAnswerSchema(BaseModel):
    """答题结果中的单题明细（契约 7.7）。"""

    question_id: uuid.UUID
    question_order: int
    type: PracticeQuestionType
    prompt: str
    submitted_answer: str | bool
    is_correct: bool
    score: float
    correct_answer: str | bool
    explanation: str
    knowledge_point: str | None


class PracticeAttemptResultSchema(BaseModel):
    """答题结果（契约 7.7）：百分制总分保留两位小数。"""

    id: uuid.UUID
    practice_set_id: uuid.UUID
    student_id: uuid.UUID
    total_score: float
    submitted_at: UtcTimestamp
    answers: list[PracticeAttemptAnswerSchema]


# --------------------------------------------------------------------------- #
# 模型输出（内部）：只描述结构，业务配额与来源核对在 generation_ai 中完成
# --------------------------------------------------------------------------- #
class GeneratedOption(BaseModel):
    """模型给出的选项文本（选项 ID 由服务端生成）。"""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500)


class GeneratedGradingPoint(BaseModel):
    """模型给出的评分要点：说明 + 一组可接受短语。"""

    model_config = ConfigDict(extra="forbid")

    point: str = Field(min_length=1, max_length=500)
    accepted: list[str] = Field(min_length=1, max_length=8)


class GeneratedQuestion(BaseModel):
    """模型给出的单题（三种题型共用，按题型取用相应字段）。"""

    model_config = ConfigDict(extra="forbid")

    type: PracticeQuestionType
    prompt: str = Field(min_length=1, max_length=2000)
    options: list[GeneratedOption] = Field(default_factory=list, max_length=6)
    #: 单选题：正确选项下标（严格整数，不接受字符串下标）
    correct_option_index: StrictInt | None = None
    #: 判断题：正确取值（严格布尔，不接受 0/1 或 "true"）
    correct_boolean: StrictBool | None = None
    #: 简答题：标准答案文本
    correct_text: str | None = Field(default=None, max_length=2000)
    explanation: str = Field(min_length=1, max_length=2000)
    knowledge_point: str | None = Field(default=None, max_length=255)
    grading_points: list[GeneratedGradingPoint] = Field(
        default_factory=list, max_length=6
    )
    #: 来源片段 ID（必须是本次上下文中的片段）
    source_chunk_id: str = Field(min_length=1, max_length=64)
    #: 原文摘录（必须能在该片段原文中找到）
    source_quote: str = Field(min_length=1, max_length=1000)


class GeneratedPractice(BaseModel):
    """模型输出：标题 + 精确数量的题目。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    questions: list[GeneratedQuestion] = Field(min_length=1, max_length=MAX_QUESTIONS)
