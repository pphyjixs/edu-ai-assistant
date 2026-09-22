"""Assignments 请求校验、总分一致性与截止规则（契约第 8 节，纯单元测试）。

不接触数据库：覆盖字段边界、严格类型、分数字段精度、评分项顺序、修改请求的
空对象与 ``due_at`` 语义、总分比较（``Decimal``）与"评分规则是否实际变化"，
以及 :func:`can_submit` 的截止/补交规则。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.errors import RubricScoreMismatchError
from app.modules.assignments import service
from app.modules.assignments.models import (
    MAX_SCORE,
    TITLE_MAX_LENGTH,
    Assignment,
    AssignmentStatus,
)
from app.modules.assignments.schemas import (
    AssignmentCreateRequest,
    AssignmentUpdateRequest,
    RubricItemRequest,
    ensure_rubric_total_matches,
    rubric_fingerprint,
)

UTC = timezone.utc


def _create_payload(**overrides) -> dict:
    payload = {
        "title": "  实验一 需求分析  ",
        "description": "任务说明",
        "total_score": 100,
        "due_at": "2026-09-25T15:59:00Z",
        "allow_late_submission": False,
        "rubric_items": [
            {"title": "需求完整性", "description": "是否完整", "max_score": 40, "order": 1},
            {"title": "建模规范", "description": "是否一致", "max_score": 60, "order": 2},
        ],
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# 创建请求
# --------------------------------------------------------------------------- #
def test_create_request_normalizes_fields() -> None:
    request = AssignmentCreateRequest.model_validate(_create_payload())

    assert request.title == "实验一 需求分析"  # 去除首尾空白
    assert request.total_score == Decimal("100")
    assert isinstance(request.total_score, Decimal)
    assert request.allow_late_submission is False
    assert request.due_at == datetime(2026, 9, 25, 15, 59, tzinfo=UTC)
    assert [item.order for item in request.rubric_items] == [1, 2]


def test_create_request_defaults() -> None:
    payload = _create_payload()
    payload.pop("description")
    payload.pop("due_at")
    payload.pop("allow_late_submission")

    request = AssignmentCreateRequest.model_validate(payload)

    assert request.description == ""
    assert request.due_at is None
    assert request.allow_late_submission is False


def test_title_length_is_checked_after_stripping() -> None:
    """标题边界：先去除首尾空白，再检查 1–200 字符（契约 8.2）。

    "首尾各一个空格 + 正文 200 字符"去除空白后恰好是 200 字符，属于**合法**输入，
    不能被长度约束提前拒掉。
    """
    body = "字" * TITLE_MAX_LENGTH
    padded = f" {body} "

    created = AssignmentCreateRequest.model_validate(
        _create_payload(
            title=padded,
            rubric_items=[{"title": padded, "max_score": 100, "order": 1}],
        )
    )
    assert created.title == body
    assert created.rubric_items[0].title == body

    item = RubricItemRequest.model_validate(
        {"title": padded, "max_score": 1, "order": 1}
    )
    assert item.title == body

    updated = AssignmentUpdateRequest.model_validate({"title": padded})
    assert updated.title == body


@pytest.mark.parametrize(
    "title",
    ["字" * (TITLE_MAX_LENGTH + 1), " " + "字" * (TITLE_MAX_LENGTH + 1) + " ", "  "],
)
def test_title_rejected_after_stripping(title: str) -> None:
    """去除首尾空白后为空或超过 200 字符 → 422（创建、修改、评分项一致）。"""
    with pytest.raises(ValidationError):
        AssignmentCreateRequest.model_validate(_create_payload(title=title))
    with pytest.raises(ValidationError):
        AssignmentUpdateRequest.model_validate({"title": title})
    with pytest.raises(ValidationError):
        RubricItemRequest.model_validate({"title": title, "max_score": 1, "order": 1})


def test_float_scores_avoid_binary_artifacts() -> None:
    """浮点分数经 ``str`` 转 Decimal，不会出现 40.1 → 40.0999999 的误差。"""
    request = AssignmentCreateRequest.model_validate(
        _create_payload(
            total_score=40.1,
            rubric_items=[{"title": "A", "max_score": 40.1, "order": 1}],
        )
    )

    assert request.total_score == Decimal("40.1")
    assert request.rubric_items[0].max_score == Decimal("40.1")


def test_due_at_is_normalized_to_utc() -> None:
    request = AssignmentCreateRequest.model_validate(
        _create_payload(due_at="2026-09-25T23:59:00+08:00")
    )

    assert request.due_at == datetime(2026, 9, 25, 15, 59, tzinfo=UTC)


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "   "},
        {"title": "x" * 201},
        {"title": None},
        {"description": None},
        {"description": "字" * 20001},
        {"total_score": None},
        {"total_score": True},
        {"total_score": "100"},
        {"total_score": 100.123},
        # 三位小数：Schema 的 multipleOf: 0.01 与运行时 decimal_places=2 都必须拒绝
        {"total_score": 100.001},
        {"total_score": 0},
        {"total_score": -1},
        {"total_score": float("nan")},
        {"total_score": float("inf")},
        {"allow_late_submission": None},
        {"allow_late_submission": 1},
        {"allow_late_submission": "false"},
        {"due_at": "2026-09-25T15:59:00"},  # 无时区
        {"due_at": "不是时间"},
        {"due_at": 123},
        {"rubric_items": []},
        {"rubric_items": None},
        {
            "rubric_items": [
                {"title": f"T{index}", "max_score": 1, "order": index}
                for index in range(1, 52)
            ]
        },
        {"rubric_items": [{"title": "A", "max_score": 1, "order": 0}]},
        {
            "rubric_items": [
                {"title": "A", "max_score": 40, "order": 1},
                {"title": "B", "max_score": 60, "order": 3},
            ]
        },
        {
            "rubric_items": [
                {"title": "A", "max_score": 40, "order": 1},
                {"title": "B", "max_score": 60, "order": 1},
            ]
        },
        {
            "rubric_items": [
                {"title": "A", "max_score": 50, "order": 1},
                {"title": "B", "max_score": 50, "order": 2},
                {"title": "C", "max_score": 50, "order": 3, "extra": 1},
            ]
        },
        {"unknown_field": 1},
    ],
)
def test_create_request_rejects_invalid_payload(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        AssignmentCreateRequest.model_validate(_create_payload(**overrides))


def test_rubric_item_score_rules() -> None:
    """评分项分值：严格 number、正数、最多两位小数。"""
    for value in (True, "40", None, 0, -1, 40.123, float("nan")):
        with pytest.raises(ValidationError):
            AssignmentCreateRequest.model_validate(
                _create_payload(
                    total_score=40,
                    rubric_items=[{"title": "A", "max_score": value, "order": 1}],
                )
            )

    ok = AssignmentCreateRequest.model_validate(
        _create_payload(
            total_score=40.55,
            rubric_items=[{"title": "A", "max_score": 40.55, "order": 1}],
        )
    )
    assert ok.rubric_items[0].max_score == Decimal("40.55")


# --------------------------------------------------------------------------- #
# 修改请求
# --------------------------------------------------------------------------- #
def test_update_request_rejects_empty_object_or_explicit_null() -> None:
    with pytest.raises(ValidationError):
        AssignmentUpdateRequest.model_validate({})
    for field in ("title", "description", "total_score", "allow_late_submission", "rubric_items"):
        with pytest.raises(ValidationError):
            AssignmentUpdateRequest.model_validate({field: None})
    with pytest.raises(ValidationError):
        AssignmentUpdateRequest.model_validate({"unknown": 1})


def test_update_total_score_enforces_same_limits_as_create() -> None:
    """修改请求的 ``total_score`` 与创建一致：正数、最多两位小数、不超过上限。"""
    for value in (0, -1, 100.001, float(MAX_SCORE) + 1):
        with pytest.raises(ValidationError):
            AssignmentUpdateRequest.model_validate({"total_score": value})

    ok = AssignmentUpdateRequest.model_validate({"total_score": 100.55})
    assert ok.total_score == Decimal("100.55")


def test_update_rubric_items_enforces_count_bounds() -> None:
    """修改时 ``rubric_items`` 一旦出现就必须是 1–50 项（与创建一致）。"""
    for count in (0, 51):
        items = [
            {"title": f"T{index}", "max_score": 1, "order": index}
            for index in range(1, count + 1)
        ]
        with pytest.raises(ValidationError):
            AssignmentUpdateRequest.model_validate({"rubric_items": items})

    ok = AssignmentUpdateRequest.model_validate(
        {"rubric_items": [{"title": "A", "max_score": 1, "order": 1}]}
    )
    assert [item.order for item in ok.rubric_items] == [1]


def test_update_request_due_at_null_clears_and_omission_keeps() -> None:
    cleared = AssignmentUpdateRequest.model_validate({"due_at": None})
    assert cleared.model_fields_set == {"due_at"}
    assert cleared.due_at is None

    kept = AssignmentUpdateRequest.model_validate({"title": "新标题"})
    assert kept.model_fields_set == {"title"}
    assert "due_at" not in kept.model_fields_set


def test_update_request_partial_rubric_still_validates_order_and_type() -> None:
    ok = AssignmentUpdateRequest.model_validate(
        {"total_score": 50, "rubric_items": [{"title": "A", "max_score": 50, "order": 1}]}
    )
    assert ok.total_score == Decimal("50")

    with pytest.raises(ValidationError):
        AssignmentUpdateRequest.model_validate(
            {
                "rubric_items": [
                    {"title": "A", "max_score": 50, "order": 2},
                    {"title": "B", "max_score": 50, "order": 3},
                ]
            }
        )
    with pytest.raises(ValidationError):
        AssignmentUpdateRequest.model_validate({"total_score": "50"})


# --------------------------------------------------------------------------- #
# 总分一致性（Decimal 精确比较）
# --------------------------------------------------------------------------- #
def test_rubric_total_must_match_exactly() -> None:
    ensure_rubric_total_matches(Decimal("100"), [Decimal("40"), Decimal("60")])
    ensure_rubric_total_matches(Decimal("100.00"), [Decimal("40.00"), Decimal("60")])
    # Decimal 精确比较：0.1 + 0.2 恰好等于 0.3
    ensure_rubric_total_matches(
        Decimal("0.30"), [Decimal("0.10"), Decimal("0.20")]
    )

    for total, scores in (
        (Decimal("100"), [Decimal("40"), Decimal("59.99")]),
        (Decimal("100"), [Decimal("40"), Decimal("60.01")]),
        (Decimal("0.30"), [Decimal("0.10"), Decimal("0.19")]),
    ):
        with pytest.raises(RubricScoreMismatchError) as excinfo:
            ensure_rubric_total_matches(total, scores)
        assert excinfo.value.code.value == "RUBRIC_SCORE_MISMATCH"
        assert excinfo.value.status_code == 422
        assert excinfo.value.details["total_score"] == str(total)


def test_rubric_fingerprint_detects_real_changes() -> None:
    base = [(("需求", "说明", Decimal("40"), 1), ("建模", "", Decimal("60"), 2))]
    items = list(base[0])

    same = rubric_fingerprint(Decimal("100"), list(reversed(items)))
    assert same == rubric_fingerprint(Decimal("100"), items), "仅入参顺序不同不算变化"

    assert rubric_fingerprint(Decimal("101"), items) != rubric_fingerprint(
        Decimal("100"), items
    ), "总分变化必须算变化"

    changed_title = [("需求（改）", "说明", Decimal("40"), 1), items[1]]
    assert rubric_fingerprint(Decimal("100"), changed_title) != same

    changed_description = [("需求", "新说明", Decimal("40"), 1), items[1]]
    assert rubric_fingerprint(Decimal("100"), changed_description) != same

    changed_score = [("需求", "说明", Decimal("41"), 1), ("建模", "", Decimal("59"), 2)]
    assert rubric_fingerprint(Decimal("100"), changed_score) != same

    changed_order = [("需求", "说明", Decimal("40"), 2), ("建模", "", Decimal("60"), 1)]
    assert rubric_fingerprint(Decimal("100"), changed_order) != same


# --------------------------------------------------------------------------- #
# 截止时间与补交判断（契约 8.10）
# --------------------------------------------------------------------------- #
def _assignment(**overrides) -> Assignment:
    values: dict = {
        "id": uuid.uuid4(),
        "status": AssignmentStatus.PUBLISHED,
        "due_at": None,
        "allow_late_submission": False,
    }
    values.update(overrides)
    return Assignment(**values)


def test_can_submit_without_due_at() -> None:
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    assert service.can_submit(_assignment(), now=now) is True


def test_can_submit_before_and_at_due_at() -> None:
    due = datetime(2026, 9, 25, 15, 59, tzinfo=UTC)

    assert service.can_submit(
        _assignment(due_at=due), now=due - timedelta(seconds=1)
    ) is True
    # now == due_at 视为已经截止
    assert service.can_submit(_assignment(due_at=due), now=due) is False
    assert service.can_submit(
        _assignment(due_at=due), now=due + timedelta(seconds=1)
    ) is False


def test_can_submit_late_only_when_allowed() -> None:
    due = datetime(2026, 9, 25, 15, 59, tzinfo=UTC)
    late = due + timedelta(days=1)

    assert service.can_submit(
        _assignment(due_at=due, allow_late_submission=True), now=late
    ) is True
    assert service.can_submit(_assignment(due_at=due), now=late) is False


@pytest.mark.parametrize(
    "status",
    [AssignmentStatus.DRAFT, AssignmentStatus.CLOSED, AssignmentStatus.ARCHIVED],
)
def test_can_submit_rejects_non_published_status(status: AssignmentStatus) -> None:
    """手工关闭（以及草稿/归档）优先于 allow_late_submission。"""
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    assert service.can_submit(
        _assignment(status=status, allow_late_submission=True), now=now
    ) is False


def test_current_rubric_version_id_exposes_pointer() -> None:
    version_id = uuid.uuid4()
    assignment = _assignment(current_rubric_version_id=version_id)
    assert service.current_rubric_version_id(assignment) == version_id
