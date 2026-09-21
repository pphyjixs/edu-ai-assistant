"""练习的题型配额与百分制评分（``docs/api-contract.md`` 7.9）。

纯函数，无数据库与网络依赖，便于单元测试与复用：

- :func:`allocate_question_counts`：按题型顺序均衡分配题数，余数依次分配给
  靠前题型（每种题型至少 1 题，由请求校验保证）。
- :func:`normalize_text`：简答文本的 Unicode NFKC、大小写、空白与常见标点
  规范化，保证"同义写法"能命中同一评分要点。
- :func:`grade_attempt`：所有题等权（每题满分 ``100 / 题数``），单选与判断
  完全匹配得满分；简答题每个要点只计一次、命中任一可接受短语即得分，
  命中全部要点才算完全正确。总分按 ``Decimal`` 计算并四舍五入到两位小数。
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.modules.practice.models import PracticeQuestionType

#: 百分制总分
TOTAL_SCORE = Decimal("100")

#: 分数精度（两位小数）
SCORE_QUANTUM = Decimal("0.01")

#: 需要剔除的常见标点与空白（规范化后不参与比对）
_PUNCTUATION = re.compile(
    r"[\s，。、；：？！“”‘’（）【】《》,.;:?!\"'()\[\]{}<>·—\-_~`|/\\]+"
)


def allocate_question_counts(
    question_count: int, question_types: list[PracticeQuestionType]
) -> dict[PracticeQuestionType, int]:
    """按题型顺序均衡分配题数（余数给靠前题型）。"""
    if not question_types:
        raise ValueError("question_types 不能为空")
    if question_count < len(question_types):
        raise ValueError("question_count 不能少于题型数量")

    base, remainder = divmod(question_count, len(question_types))
    return {
        question_type: base + (1 if index < remainder else 0)
        for index, question_type in enumerate(question_types)
    }


def normalize_text(text: str) -> str:
    """简答文本规范化：NFKC → 折叠大小写 → 去空白与常见标点。"""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return _PUNCTUATION.sub("", normalized)


def round_score(value: Decimal) -> Decimal:
    """四舍五入到两位小数。"""
    return value.quantize(SCORE_QUANTUM, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class GradableQuestion:
    """评分所需的题目信息（与 ORM 解耦）。"""

    question_id: uuid.UUID
    order: int
    type: PracticeQuestionType
    #: 单选：选项 ID 字符串；判断：布尔；简答：标准答案文本
    correct_answer: object
    #: 简答题评分要点：[{"point": str, "accepted": [str]}]
    grading_points: list[dict]


@dataclass(frozen=True, slots=True)
class GradedAnswer:
    """单题评分结果。"""

    question_id: uuid.UUID
    order: int
    submitted: object
    is_correct: bool
    score: Decimal
    matched_points: list[str]


def _grade_short_answer(
    question: GradableQuestion, submitted: str, full_score: Decimal
) -> tuple[Decimal, bool, list[str]]:
    """简答评分：每个要点只计一次，命中全部要点才算完全正确。"""
    points = [item for item in question.grading_points if item.get("accepted")]
    normalized_submitted = normalize_text(submitted)

    if not points:
        # 没有评分要点（理论上不会出现）：按标准答案完全匹配处理
        correct = normalize_text(str(question.correct_answer))
        is_correct = bool(normalized_submitted) and normalized_submitted == correct
        return (
            (full_score if is_correct else Decimal("0")),
            is_correct,
            [],
        )

    matched: list[str] = []
    for item in points:
        accepted = [normalize_text(str(phrase)) for phrase in item["accepted"]]
        if any(phrase and phrase in normalized_submitted for phrase in accepted):
            matched.append(str(item.get("point", "")))

    ratio = Decimal(len(matched)) / Decimal(len(points))
    score = round_score(full_score * ratio)
    return score, len(matched) == len(points), matched


def grade_question(
    question: GradableQuestion,
    submitted: object,
    *,
    full_score: Decimal,
) -> GradedAnswer:
    """单题评分。"""
    if question.type is PracticeQuestionType.SHORT_ANSWER:
        score, is_correct, matched = _grade_short_answer(
            question, str(submitted), full_score
        )
        return GradedAnswer(
            question_id=question.question_id,
            order=question.order,
            submitted=submitted,
            is_correct=is_correct,
            score=score,
            matched_points=matched,
        )

    is_correct = submitted == question.correct_answer
    return GradedAnswer(
        question_id=question.question_id,
        order=question.order,
        submitted=submitted,
        is_correct=is_correct,
        score=full_score if is_correct else Decimal("0"),
        matched_points=[],
    )


def grade_attempt(
    questions: list[GradableQuestion], submitted: dict[uuid.UUID, object]
) -> tuple[Decimal, list[GradedAnswer]]:
    """整卷评分：等权、百分制、两位小数、限制在 0–100。

    :raises ValueError: 提交未覆盖全部题目（调用方应先完成请求校验）。
    """
    if not questions:
        raise ValueError("题目不能为空")
    missing = [q.question_id for q in questions if q.question_id not in submitted]
    if missing:
        raise ValueError("提交答案未覆盖全部题目")

    full_score = TOTAL_SCORE / Decimal(len(questions))
    graded = [
        grade_question(question, submitted[question.question_id], full_score=full_score)
        for question in sorted(questions, key=lambda item: item.order)
    ]

    total = round_score(sum((item.score for item in graded), Decimal("0")))
    total = min(max(total, Decimal("0")), TOTAL_SCORE)
    return total, graded


__all__ = [
    "GradableQuestion",
    "GradedAnswer",
    "SCORE_QUANTUM",
    "TOTAL_SCORE",
    "allocate_question_counts",
    "grade_attempt",
    "grade_question",
    "normalize_text",
    "round_score",
]
