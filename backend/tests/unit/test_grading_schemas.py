"""Grading 请求模型、分数语义与 AI 输出校验的单元测试（契约第 9 节）。

覆盖：
- 上传请求的严格类型、显式 ``null``、未知字段；
- 复核请求的完整快照语义（覆盖全部评分项）、分数范围与两位小数、重复项拒绝；
- 文件类型白名单（PDF/DOCX，**拒绝旧版 .doc**）、MIME 一致性、大小与 sha256；
- 模型输出的服务端校验：评分项缺失/重复/越界、证据摘录必须存在于报告原文。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.errors import (
    RubricScoreMismatchError,
    UploadInvalidError,
    ValidationError,
)
from app.modules.grading import grading_ai
from app.modules.grading.grading_ai import RubricItemSnapshot
from app.modules.grading.schemas import (
    GradeReviewUpdateRequest,
    SubmissionUploadInitRequest,
    ensure_review_covers_rubric,
)
from app.modules.grading.service import (
    split_extension,
    validate_upload_request,
)

PDF_MIME = "application/pdf"
DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
SHA256 = "a" * 64


# --------------------------------------------------------------------------- #
# 上传请求
# --------------------------------------------------------------------------- #
def _upload_payload(**overrides: object) -> dict:
    payload = {
        "filename": "report.pdf",
        "content_type": PDF_MIME,
        "size": 1024,
        "sha256": SHA256,
    }
    payload.update(overrides)
    return payload


def test_upload_request_rejects_explicit_null() -> None:
    with pytest.raises(PydanticValidationError):
        SubmissionUploadInitRequest.model_validate(_upload_payload(size=None))


def test_upload_request_rejects_unknown_fields() -> None:
    with pytest.raises(PydanticValidationError):
        SubmissionUploadInitRequest.model_validate(
            _upload_payload(extra_field="x")
        )


def test_upload_request_rejects_boolean_size() -> None:
    """严格整数：布尔不能当 1 用（Python 里 bool 是 int 的子类）。"""
    with pytest.raises(PydanticValidationError):
        SubmissionUploadInitRequest.model_validate(_upload_payload(size=True))


@pytest.mark.parametrize("filename", ["report.pdf", "报告.DOCX"])
def test_validate_upload_accepts_pdf_and_docx(filename: str) -> None:
    content_type = DOCX_MIME if filename.lower().endswith(".docx") else PDF_MIME
    validated = validate_upload_request(
        SubmissionUploadInitRequest.model_validate(
            _upload_payload(filename=filename, content_type=content_type)
        ),
        max_bytes=1024 * 1024,
    )

    assert validated.filename == filename
    assert validated.content_type == content_type
    assert validated.sha256 == SHA256


@pytest.mark.parametrize("filename", ["report.doc", "slides.pptx", "archive.zip", "noext"])
def test_validate_upload_rejects_other_extensions(filename: str) -> None:
    """旧版 .doc 必须被明确拒绝（契约 9.2）。"""
    with pytest.raises(UploadInvalidError) as excinfo:
        validate_upload_request(
            SubmissionUploadInitRequest.model_validate(_upload_payload(filename=filename)),
            max_bytes=1024 * 1024,
        )

    assert excinfo.value.details["reason"] == "FILE_TYPE_NOT_ALLOWED"


def test_validate_upload_rejects_mime_mismatch() -> None:
    with pytest.raises(UploadInvalidError) as excinfo:
        validate_upload_request(
            SubmissionUploadInitRequest.model_validate(
                _upload_payload(content_type="application/octet-stream")
            ),
            max_bytes=1024 * 1024,
        )

    assert excinfo.value.details["reason"] == "CONTENT_TYPE_MISMATCH"


@pytest.mark.parametrize("size", [0, -1, 1025])
def test_validate_upload_rejects_size_out_of_range(size: int) -> None:
    with pytest.raises(UploadInvalidError) as excinfo:
        validate_upload_request(
            SubmissionUploadInitRequest.model_validate(_upload_payload(size=size)),
            max_bytes=1024,
        )

    assert excinfo.value.details["reason"] == "SIZE_OUT_OF_RANGE"


def test_validate_upload_rejects_bad_sha256() -> None:
    with pytest.raises(UploadInvalidError) as excinfo:
        validate_upload_request(
            SubmissionUploadInitRequest.model_validate(_upload_payload(sha256="xyz")),
            max_bytes=1024,
        )

    assert excinfo.value.details["reason"] == "SHA256_INVALID"


def test_validate_upload_rejects_path_separator_in_filename() -> None:
    with pytest.raises(UploadInvalidError) as excinfo:
        validate_upload_request(
            SubmissionUploadInitRequest.model_validate(
                _upload_payload(filename="dir/report.pdf")
            ),
            max_bytes=1024,
        )

    assert excinfo.value.details["reason"] == "FILE_TYPE_NOT_ALLOWED"


def test_sha256_is_normalized_to_lowercase() -> None:
    validated = validate_upload_request(
        SubmissionUploadInitRequest.model_validate(_upload_payload(sha256="A" * 64)),
        max_bytes=1024,
    )

    assert validated.sha256 == "a" * 64


@pytest.mark.parametrize(
    ("filename", "expected"),
    [("a.pdf", ".pdf"), ("a.PDF", ".pdf"), ("a", ""), ("a.tar.gz", ".gz")],
)
def test_split_extension(filename: str, expected: str) -> None:
    assert split_extension(filename) == expected


# --------------------------------------------------------------------------- #
# 复核请求
# --------------------------------------------------------------------------- #
def _review_payload(**overrides: object) -> dict:
    payload = {
        "summary": "整体反馈",
        "items": [{"rubric_item_id": "0" * 8 + "-0000-0000-0000-000000000000",
                   "final_score": 35,
                   "teacher_comment": "还可以补充异常流程"}],
    }
    payload.update(overrides)
    return payload


def test_review_request_accepts_valid_payload() -> None:
    request = GradeReviewUpdateRequest.model_validate(_review_payload())

    assert request.summary == "整体反馈"
    assert request.items[0].final_score == Decimal("35")
    assert request.items[0].teacher_comment == "还可以补充异常流程"


def test_review_request_trims_summary_and_rejects_blank() -> None:
    assert GradeReviewUpdateRequest.model_validate(
        _review_payload(summary="  反馈  ")
    ).summary == "反馈"

    with pytest.raises(PydanticValidationError):
        GradeReviewUpdateRequest.model_validate(_review_payload(summary="   "))


def test_review_request_rejects_empty_items() -> None:
    with pytest.raises(PydanticValidationError):
        GradeReviewUpdateRequest.model_validate(_review_payload(items=[]))


def test_review_request_rejects_duplicate_rubric_items() -> None:
    item = _review_payload()["items"][0]
    with pytest.raises(PydanticValidationError):
        GradeReviewUpdateRequest.model_validate(
            _review_payload(items=[item, dict(item)])
        )


def test_review_request_rejects_explicit_null_and_unknown_fields() -> None:
    with pytest.raises(PydanticValidationError):
        GradeReviewUpdateRequest.model_validate(
            _review_payload(summary=None)
        )
    with pytest.raises(PydanticValidationError):
        GradeReviewUpdateRequest.model_validate(_review_payload(extra=1))


@pytest.mark.parametrize("score", ["35", True, None, float("nan")])
def test_review_request_rejects_non_number_scores(score: object) -> None:
    """分数只接受 JSON number：字符串、布尔、null 与 NaN 一律 422。"""
    item = dict(_review_payload()["items"][0])
    item["final_score"] = score
    with pytest.raises(PydanticValidationError):
        GradeReviewUpdateRequest.model_validate(_review_payload(items=[item]))


def test_review_request_rejects_more_than_two_decimal_places() -> None:
    item = dict(_review_payload()["items"][0])
    item["final_score"] = 1.234
    with pytest.raises(PydanticValidationError):
        GradeReviewUpdateRequest.model_validate(_review_payload(items=[item]))


def test_review_request_rejects_negative_score() -> None:
    item = dict(_review_payload()["items"][0])
    item["final_score"] = -1
    with pytest.raises(PydanticValidationError):
        GradeReviewUpdateRequest.model_validate(_review_payload(items=[item]))


def test_review_request_rejects_comment_over_limit() -> None:
    item = dict(_review_payload()["items"][0])
    item["teacher_comment"] = "x" * 2001
    with pytest.raises(PydanticValidationError):
        GradeReviewUpdateRequest.model_validate(_review_payload(items=[item]))


# --------------------------------------------------------------------------- #
# 完整性校验
# --------------------------------------------------------------------------- #
ITEM_A = "11111111-1111-1111-1111-111111111111"
ITEM_B = "22222222-2222-2222-2222-222222222222"


def _ensure(submitted_ids, scores, rubric_ids=(ITEM_A, ITEM_B), max_scores=None):
    import uuid as _uuid

    limits = max_scores or {_uuid.UUID(rubric_ids[0]): Decimal("40"),
                            _uuid.UUID(rubric_ids[1]): Decimal("60")}
    ensure_review_covers_rubric(
        [_uuid.UUID(item) for item in submitted_ids],
        [_uuid.UUID(item) for item in rubric_ids],
        limits,
        {_uuid.UUID(key): Decimal(value) for key, value in scores.items()},
    )


def test_ensure_review_accepts_full_coverage() -> None:
    _ensure([ITEM_A, ITEM_B], {ITEM_A: "40", ITEM_B: "60"})


def test_ensure_review_rejects_missing_item() -> None:
    with pytest.raises(ValidationError):
        _ensure([ITEM_A], {ITEM_A: "40"})


def test_ensure_review_rejects_unknown_item() -> None:
    with pytest.raises(ValidationError):
        _ensure(
            [ITEM_A, ITEM_B, "33333333-3333-3333-3333-333333333333"],
            {ITEM_A: "40", ITEM_B: "60"},
        )


def test_ensure_review_rejects_duplicate_item() -> None:
    with pytest.raises(ValidationError):
        _ensure([ITEM_A, ITEM_A, ITEM_B], {ITEM_A: "40", ITEM_B: "60"})


def test_ensure_review_rejects_score_over_max() -> None:
    with pytest.raises(ValidationError):
        _ensure([ITEM_A, ITEM_B], {ITEM_A: "41", ITEM_B: "60"})


def test_rubric_score_mismatch_error_code_is_stable() -> None:
    """``RUBRIC_SCORE_MISMATCH`` 由 Assignments 侧的纯校验抛出，错误码不能被本模块改写。"""
    from app.modules.assignments.schemas import ensure_rubric_total_matches

    with pytest.raises(RubricScoreMismatchError):
        ensure_rubric_total_matches(Decimal("100"), [Decimal("40"), Decimal("50")])


# --------------------------------------------------------------------------- #
# AI 输出校验（含证据位置）
# --------------------------------------------------------------------------- #
REPORT_UNITS = [
    (1, "Requirement analysis: login and course management are supported."),
    (2, "Modeling note: the exception flow is not covered yet."),
]

#: 跨单元的证据摘录（同时出现在两个来源单元里，便于构造"位置错误"用例）
SHARED_PHRASE = "the"


def _rubric() -> list[RubricItemSnapshot]:
    import uuid as _uuid

    return [
        RubricItemSnapshot(
            rubric_item_id=_uuid.UUID(ITEM_A),
            order=1,
            title="Requirements",
            description="coverage",
            max_score=Decimal("40"),
        ),
        RubricItemSnapshot(
            rubric_item_id=_uuid.UUID(ITEM_B),
            order=2,
            title="Modeling",
            description="consistency",
            max_score=Decimal("60"),
        ),
    ]


def _report(source_type: str = "PDF_PAGE") -> grading_ai.ReportSource:
    return grading_ai.ReportSource(
        source_type=source_type, units=list(REPORT_UNITS)
    )


def _generated(items: list[dict], summary: str = "整体不错") -> grading_ai.GeneratedGrade:
    return grading_ai.GeneratedGrade.model_validate(
        {"summary": summary, "items": items}
    )


def _item(rubric_item_id: str = ITEM_A, **overrides: object) -> dict:
    """构造一条模型输出；证据摘录默认取**声明位置**上的原文，
    这样"摘录与位置一致"是默认成立的，需要构造错误时再显式覆盖。"""
    payload = {
        "rubric_item_id": rubric_item_id,
        "score": 35,
        "comment": "coverage is fine",
        "evidence_quote": None,
        "location_start": 1,
        "location_end": 1,
        "error_type": "",
        "improvement_suggestion": "补充异常流程",
    }
    payload.update(overrides)
    if payload["evidence_quote"] is None:
        start = payload["location_start"]
        payload["evidence_quote"] = next(
            (
                text
                for location, text in REPORT_UNITS
                if location == start
            ),
            REPORT_UNITS[0][1],
        )
    return payload


def _validate(items: list[dict], report: grading_ai.ReportSource | None = None):
    return grading_ai.validate_generated(
        _generated(items), rubric=_rubric(), report=report or _report()
    )


def test_validate_generated_accepts_valid_output() -> None:
    validated = _validate(
        [
            _item(),
            _item(
                rubric_item_id=ITEM_B,
                score=60,
                evidence_quote=REPORT_UNITS[1][1],
                location_start=2,
                location_end=2,
            ),
        ]
    )

    assert [item.rubric_item_id for item in validated.items] == [
        grading_ai.uuid.UUID(ITEM_A),
        grading_ai.uuid.UUID(ITEM_B),
    ]
    assert validated.summary == "整体不错"
    assert validated.items[0].score == Decimal("35")
    assert validated.items[0].evidence_source_type == "PDF_PAGE"
    assert validated.items[0].evidence_location_start == 1
    assert validated.items[0].evidence_location_end == 1


def test_validate_generated_allows_range_spanning_units() -> None:
    validated = _validate(
        [_item(location_start=1, location_end=2), _item(rubric_item_id=ITEM_B, score=60, location_start=2, location_end=2)]
    )

    assert validated.items[0].evidence_location_end == 2


def test_validate_generated_rejects_missing_item() -> None:
    with pytest.raises(grading_ai.GradeGenerationError):
        _validate([_item()])


def test_validate_generated_rejects_duplicate_item() -> None:
    with pytest.raises(grading_ai.GradeGenerationError):
        _validate([_item(), _item()])


def test_validate_generated_rejects_unknown_item() -> None:
    with pytest.raises(grading_ai.GradeGenerationError):
        _validate(
            [
                _item(),
                _item(rubric_item_id="33333333-3333-3333-3333-333333333333"),
            ]
        )


def test_validate_generated_rejects_score_over_max() -> None:
    with pytest.raises(grading_ai.GradeGenerationError):
        _validate([_item(score=41), _item(rubric_item_id=ITEM_B, score=60)])


def test_validate_generated_rejects_too_many_decimal_places() -> None:
    with pytest.raises(grading_ai.GradeGenerationError):
        _validate(
            [_item(score=35.123), _item(rubric_item_id=ITEM_B, score=60)]
        )


@pytest.mark.parametrize("location", [0, -1])
def test_validate_generated_rejects_non_positive_location(location: int) -> None:
    with pytest.raises(grading_ai.GradeGenerationError):
        _validate(
            [
                _item(location_start=location),
                _item(rubric_item_id=ITEM_B, score=60),
            ]
        )


def test_validate_generated_rejects_reversed_range() -> None:
    with pytest.raises(grading_ai.GradeGenerationError):
        _validate(
            [
                _item(location_start=2, location_end=1),
                _item(rubric_item_id=ITEM_B, score=60),
            ]
        )


def test_validate_generated_rejects_location_outside_report() -> None:
    """位置必须存在于报告实际提取单元中。"""
    with pytest.raises(grading_ai.GradeGenerationError):
        _validate(
            [
                _item(location_start=7, location_end=9),
                _item(rubric_item_id=ITEM_B, score=60),
            ]
        )


def test_validate_generated_rejects_fabricated_interval_end() -> None:
    """起点有效但终点不存在（如 1–999）：整个区间不被接受（契约 9.11）。"""
    with pytest.raises(grading_ai.GradeGenerationError):
        _validate(
            [
                _item(location_start=1, location_end=999),
                _item(rubric_item_id=ITEM_B, score=60),
            ]
        )
    with pytest.raises(grading_ai.GradeGenerationError):
        _validate(
            [
                _item(location_start=999, location_end=999),
                _item(rubric_item_id=ITEM_B, score=60),
            ]
        )


def test_validate_generated_allows_empty_units_inside_interval() -> None:
    """区间中间的空页/空段落不影响合法性：只要求两个端点真实存在。"""
    sparse = grading_ai.ReportSource(
        source_type="PDF_PAGE",
        units=[(1, REPORT_UNITS[0][1]), (3, REPORT_UNITS[1][1])],
    )
    validated = _validate(
        [
            # 端点 1 与 3 都存在，中间的 2 是没有文本的空页
            _item(evidence_quote=REPORT_UNITS[0][1], location_start=1, location_end=3),
            _item(
                rubric_item_id=ITEM_B,
                score=60,
                evidence_quote=REPORT_UNITS[1][1],
                location_start=3,
                location_end=3,
            ),
        ],
        report=sparse,
    )

    assert validated.items[0].evidence_location_end == 3


def test_validate_generated_rejects_quote_outside_declared_range() -> None:
    """证据摘录必须出现在**声明的区间**内：只在报告其他位置出现不算数。"""
    with pytest.raises(grading_ai.GradeGenerationError):
        _validate(
            [
                # 摘录来自第 2 个来源单元，却声明在第 1 页
                _item(
                    evidence_quote=REPORT_UNITS[1][1],
                    location_start=1,
                    location_end=1,
                ),
                _item(rubric_item_id=ITEM_B, score=60),
            ]
        )


def test_validate_generated_accepts_quote_inside_declared_range() -> None:
    validated = _validate(
        [
            _item(evidence_quote=REPORT_UNITS[1][1], location_start=1, location_end=2),
            _item(rubric_item_id=ITEM_B, score=60, location_start=2, location_end=2),
        ]
    )

    assert validated.items[0].evidence_location_start == 1


def test_validate_generated_rejects_fabricated_evidence() -> None:
    """证据摘录不在报告中：整次失败。"""
    with pytest.raises(grading_ai.GradeGenerationError):
        _validate(
            [
                _item(evidence_quote="this sentence is not in the report"),
                _item(rubric_item_id=ITEM_B, score=60),
            ]
        )


def test_validate_generated_accepts_quote_spanning_whitespace() -> None:
    """空白规范化后匹配：跨行摘录仍然可通过。"""
    quoted = "Requirement analysis:   login and course management are supported."
    validated = _validate(
        [
            _item(evidence_quote=quoted),
            _item(rubric_item_id=ITEM_B, score=60),
        ]
    )

    assert validated.items[0].evidence_quote


def test_validate_generated_rejects_empty_comment_or_quote() -> None:
    with pytest.raises(grading_ai.GradeGenerationError):
        _validate([_item(comment="  "), _item(rubric_item_id=ITEM_B, score=60)])
    with pytest.raises(grading_ai.GradeGenerationError):
        _validate([_item(evidence_quote=" "), _item(rubric_item_id=ITEM_B, score=60)])


def test_validate_generated_rejects_boolean_location() -> None:
    """位置必须是严格整数：布尔不是合法位置。"""
    with pytest.raises(PydanticValidationError):
        _generated([_item(location_start=True), _item(rubric_item_id=ITEM_B, score=60)])
    with pytest.raises(PydanticValidationError):
        _generated([_item(location_start="1"), _item(rubric_item_id=ITEM_B, score=60)])


def test_source_type_is_decided_by_the_server() -> None:
    """来源类型由报告 MIME 在服务端确定，模型输出里没有这个字段。"""
    validated = _validate([_item(), _item(rubric_item_id=ITEM_B, score=60)])
    assert validated.items[0].evidence_source_type == "PDF_PAGE"

    docx = _validate(
        [_item(), _item(rubric_item_id=ITEM_B, score=60)], report=_report("DOCX_PARAGRAPH")
    )
    assert docx.items[0].evidence_source_type == "DOCX_PARAGRAPH"


def test_grade_submission_reports_missing_model_config() -> None:
    with pytest.raises(grading_ai.GradeModelNotConfiguredError):
        grading_ai.grade_submission(
            rubric=_rubric(),
            report=_report(),
            base_url="",
            api_key="",
            model="m",
            timeout_seconds=1.0,
        )


def test_grade_submission_maps_timeout_to_generation_error() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise httpx.TimeoutException("timeout", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(grading_ai.GradeGenerationError):
            grading_ai.grade_submission(
                rubric=_rubric(),
                report=_report(),
                base_url="http://model.invalid/v1",
                api_key="k",
                model="m",
                timeout_seconds=1.0,
                client=client,
            )
    finally:
        client.close()


def test_grade_submission_maps_auth_failure_to_generation_error() -> None:
    import httpx

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={})
        )
    )
    try:
        with pytest.raises(grading_ai.GradeGenerationError):
            grading_ai.grade_submission(
                rubric=_rubric(),
                report=_report(),
                base_url="http://model.invalid/v1",
                api_key="k",
                model="m",
                timeout_seconds=1.0,
                client=client,
            )
    finally:
        client.close()


def test_grade_submission_rejects_invalid_json_output() -> None:
    import httpx

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"choices": [{"message": {"content": "not json"}}]},
            )
        )
    )
    try:
        with pytest.raises(grading_ai.GradeGenerationError):
            grading_ai.grade_submission(
                rubric=_rubric(),
                report=_report(),
                base_url="http://model.invalid/v1",
                api_key="k",
                model="m",
                timeout_seconds=1.0,
                client=client,
            )
    finally:
        client.close()


def test_grade_submission_returns_validated_output_with_raw_audit() -> None:
    import json

    import httpx

    payload = {
        "summary": "整体不错",
        "items": [
            _item(),
            _item(
                rubric_item_id=ITEM_B,
                score=55,
                evidence_quote=REPORT_UNITS[1][1],
                location_start=2,
                location_end=2,
            ),
        ],
    }
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": json.dumps(payload, ensure_ascii=False)}}
                    ]
                },
            )
        )
    )
    try:
        validated = grading_ai.grade_submission(
            rubric=_rubric(),
            report=_report(),
            base_url="http://model.invalid/v1",
            api_key="k",
            model="m",
            timeout_seconds=1.0,
            client=client,
        )
    finally:
        client.close()

    assert validated.summary == "整体不错"
    # 原始结构化输出保留下来供审计，且不包含提示词或报告全文
    assert validated.raw_output == payload


def test_prompt_marks_report_units_with_locations() -> None:
    """提示词必须按来源位置标注报告内容，模型才能给出正确的位置区间。"""
    prompt = grading_ai.build_prompt(rubric=_rubric(), report=_report())

    assert "[页 1]" in prompt
    assert "[页 2]" in prompt
    assert "location_start" in prompt

    docx_prompt = grading_ai.build_prompt(
        rubric=_rubric(), report=_report("DOCX_PARAGRAPH")
    )
    assert "[段落 1]" in docx_prompt
