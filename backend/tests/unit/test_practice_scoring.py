"""练习评分与题型配额的离线单元测试（不访问数据库与网络）。

对应契约 7.2（配额分配）与 7.9（百分制评分）：

- 题型配额按顺序均衡分配、余数给靠前题型；
- 单选/判断完全匹配得满分，否则 0；
- 简答题按要点比例给分、每个要点只计一次、命中全部要点才算完全正确；
- 总分按 ``Decimal`` 计算并四舍五入到两位小数，限制在 0–100。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.modules.practice import scoring
from app.modules.practice.models import PracticeQuestionType

SINGLE = PracticeQuestionType.SINGLE_CHOICE
TRUE_FALSE = PracticeQuestionType.TRUE_FALSE
SHORT = PracticeQuestionType.SHORT_ANSWER


def _question(
    question_type: PracticeQuestionType,
    *,
    correct: object,
    order: int = 1,
    points: list[dict] | None = None,
) -> scoring.GradableQuestion:
    return scoring.GradableQuestion(
        question_id=uuid.uuid4(),
        order=order,
        type=question_type,
        correct_answer=correct,
        grading_points=points or [],
    )


# --------------------------------------------------------------------------- #
# 题型配额
# --------------------------------------------------------------------------- #
def test_allocate_counts_splits_evenly_with_remainder_first() -> None:
    assert scoring.allocate_question_counts(5, [SINGLE, TRUE_FALSE]) == {
        SINGLE: 3,
        TRUE_FALSE: 2,
    }
    assert scoring.allocate_question_counts(6, [SINGLE, TRUE_FALSE, SHORT]) == {
        SINGLE: 2,
        TRUE_FALSE: 2,
        SHORT: 2,
    }
    assert scoring.allocate_question_counts(1, [SHORT]) == {SHORT: 1}


def test_allocate_counts_rejects_fewer_questions_than_types() -> None:
    with pytest.raises(ValueError):
        scoring.allocate_question_counts(2, [SINGLE, TRUE_FALSE, SHORT])


# --------------------------------------------------------------------------- #
# 文本规范化
# --------------------------------------------------------------------------- #
def test_normalize_text_folds_case_space_and_punctuation() -> None:
    assert scoring.normalize_text("  Software  ENGINEERING。 ") == "softwareengineering"
    assert scoring.normalize_text("需求分析、是起点！") == scoring.normalize_text(
        "需求分析是起点"
    )
    # 全角数字经 NFKC 归一为半角
    assert scoring.normalize_text("第１章") == scoring.normalize_text("第1章")


# --------------------------------------------------------------------------- #
# 单选与判断
# --------------------------------------------------------------------------- #
def test_single_choice_scores_full_or_zero() -> None:
    option_id = str(uuid.uuid4())
    question = _question(SINGLE, correct=option_id)

    hit = scoring.grade_question(question, option_id, full_score=Decimal("25"))
    miss = scoring.grade_question(question, str(uuid.uuid4()), full_score=Decimal("25"))

    assert hit.is_correct is True and hit.score == Decimal("25")
    assert miss.is_correct is False and miss.score == Decimal("0")


def test_true_false_requires_exact_boolean() -> None:
    question = _question(TRUE_FALSE, correct=True)

    assert scoring.grade_question(
        question, True, full_score=Decimal("20")
    ).is_correct
    assert not scoring.grade_question(
        question, False, full_score=Decimal("20")
    ).is_correct


# --------------------------------------------------------------------------- #
# 简答题
# --------------------------------------------------------------------------- #
def test_short_answer_partial_credit_counts_each_point_once() -> None:
    question = _question(
        SHORT,
        correct="过程、方法与工具",
        points=[
            {"point": "过程", "accepted": ["过程", "流程"]},
            {"point": "方法", "accepted": ["方法"]},
            {"point": "工具", "accepted": ["工具"]},
            {"point": "协同", "accepted": ["协同", "配合"]},
        ],
    )

    graded = scoring.grade_question(
        question,
        "软件工程包含过程、方法与工具；其中过程和流程是同一含义。",
        full_score=Decimal("25"),
    )

    # 命中「过程」「方法」「工具」三个要点；重复出现的"流程"不再额外计分
    assert graded.score == Decimal("18.75")
    assert graded.is_correct is False
    assert graded.matched_points == ["过程", "方法", "工具"]


def test_short_answer_full_match_is_correct() -> None:
    question = _question(
        SHORT,
        correct="需求分析",
        points=[
            {"point": "需求", "accepted": ["需求"]},
            {"point": "分析", "accepted": ["分析"]},
        ],
    )

    graded = scoring.grade_question(question, "需求 分析。", full_score=Decimal("50"))

    assert graded.is_correct is True
    assert graded.score == Decimal("50")


def test_short_answer_empty_points_falls_back_to_exact_match() -> None:
    question = _question(SHORT, correct="标准答案")

    hit = scoring.grade_question(question, "标准答案", full_score=Decimal("10"))
    miss = scoring.grade_question(question, "别的答案", full_score=Decimal("10"))

    assert hit.is_correct is True
    assert miss.score == Decimal("0")


# --------------------------------------------------------------------------- #
# 整卷评分
# --------------------------------------------------------------------------- #
def test_grade_attempt_is_percentage_with_two_decimals() -> None:
    """3 题等权：满分 100，总分保留两位小数。"""
    option_id = str(uuid.uuid4())
    questions = [
        _question(SINGLE, correct=option_id, order=1),
        _question(TRUE_FALSE, correct=True, order=2),
        _question(
            SHORT,
            correct="要点",
            order=3,
            points=[
                {"point": "A", "accepted": ["甲"]},
                {"point": "B", "accepted": ["乙"]},
                {"point": "C", "accepted": ["丙"]},
            ],
        ),
    ]
    submitted = {
        questions[0].question_id: option_id,
        questions[1].question_id: True,
        questions[2].question_id: "甲 乙",  # 命中 2/3 要点 → 22.22
    }

    total, graded = scoring.grade_attempt(questions, submitted)

    assert graded[2].score == Decimal("22.22")
    assert total == Decimal("88.89")
    assert total <= Decimal("100")


def test_grade_attempt_all_correct_is_exactly_100() -> None:
    questions = [
        _question(TRUE_FALSE, correct=True, order=1),
        _question(TRUE_FALSE, correct=False, order=2),
        _question(TRUE_FALSE, correct=True, order=3),
    ]
    submitted = {
        questions[0].question_id: True,
        questions[1].question_id: False,
        questions[2].question_id: True,
    }

    total, graded = scoring.grade_attempt(questions, submitted)

    assert total == Decimal("100")
    assert all(item.is_correct for item in graded)


def test_grade_attempt_requires_full_coverage() -> None:
    question = _question(TRUE_FALSE, correct=True)
    with pytest.raises(ValueError):
        scoring.grade_attempt([question], {})


# --------------------------------------------------------------------------- #
# 分值分配：明细之和必须严格等于总分
# --------------------------------------------------------------------------- #
def test_three_correct_short_answers_allocate_remainder_to_first() -> None:
    """三道全对简答题：33.34 + 33.33 + 33.33 = 100.00。"""
    questions = [
        _question(
            SHORT,
            correct="要点",
            order=order,
            points=[
                {"point": "A", "accepted": ["甲"]},
                {"point": "B", "accepted": ["乙"]},
            ],
        )
        for order in (1, 2, 3)
    ]
    submitted = {question.question_id: "甲 乙" for question in questions}

    total, graded = scoring.grade_attempt(questions, submitted)

    scores = [item.score for item in graded]
    assert scores == [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")]
    assert total == Decimal("100.00")
    assert sum(scores, Decimal("0")) == total
    assert all(item.is_correct for item in graded)


@pytest.mark.parametrize("count", [3, 4, 6, 7])
def test_score_details_always_sum_to_total(count: int) -> None:
    """任意题数下明细之和都等于总分（含余数分配）。"""
    questions = [
        _question(TRUE_FALSE, correct=True, order=order)
        for order in range(1, count + 1)
    ]
    # 前一半答对，其余答错 → 总分不是整数分界的常见情形
    submitted = {
        question.question_id: index < count // 2
        for index, question in enumerate(questions)
    }

    total, graded = scoring.grade_attempt(questions, submitted)

    assert sum((item.score for item in graded), Decimal("0")) == total
    assert total == scoring.round_score(
        Decimal("100") * Decimal(count // 2) / Decimal(count)
    )


def test_mixed_questions_partial_credit_sums_to_total() -> None:
    """单选 + 判断 + 简答混合、部分得分：明细之和仍严格等于总分。"""
    option_id = str(uuid.uuid4())
    questions = [
        _question(SINGLE, correct=option_id, order=1),
        _question(TRUE_FALSE, correct=False, order=2),
        _question(
            SHORT,
            correct="要点",
            order=3,
            points=[
                {"point": "A", "accepted": ["甲"]},
                {"point": "B", "accepted": ["乙"]},
                {"point": "C", "accepted": ["丙"]},
            ],
        ),
        _question(SHORT, correct="要点", order=4, points=[
            {"point": "A", "accepted": ["甲"]},
        ]),
    ]
    submitted = {
        questions[0].question_id: option_id,   # 对
        questions[1].question_id: True,        # 错
        questions[2].question_id: "甲 乙",     # 2/3 要点
        questions[3].question_id: "甲",        # 对
    }

    total, graded = scoring.grade_attempt(questions, submitted)

    assert sum((item.score for item in graded), Decimal("0")) == total
    assert [item.is_correct for item in graded] == [True, False, False, True]
    # 每题得分都是两位小数（存入数据库的形态）
    assert all(item.score == scoring.round_score(item.score) for item in graded)
    assert total == scoring.round_score(total)


def test_allocate_scores_favours_larger_remainder_then_order() -> None:
    """余数按小数部分从大到小分配；相同余数按题目顺序优先。"""
    raw = [Decimal("33.3333"), Decimal("33.3333"), Decimal("33.3333")]
    awarded = scoring.allocate_scores(raw, [1, 2, 3])

    assert sum(awarded, Decimal("0")) == Decimal("100.00")
    assert awarded == [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")]

    # 余数不同：小数部分更大的题目先补分
    raw = [Decimal("10.001"), Decimal("10.009"), Decimal("79.99")]
    awarded = scoring.allocate_scores(raw, [1, 2, 3])
    assert sum(awarded, Decimal("0")) == Decimal("100.00")
    assert awarded == [Decimal("10.00"), Decimal("10.01"), Decimal("79.99")]
