"""提交与批改接口的契约测试（``docs/api-contract.md`` 第 9 节）。

两块内容：

1. **OpenAPI 结构**：八个接口的路径、状态码与请求体组件必须与契约一致；
   导出的 ``contracts/openapi/openapi.json`` 也必须包含这些路径。
2. **Schema 语义**：分数只接受 JSON ``number`` 且 ``multipleOf: 0.01``；
   学生视角字段可空（服务端在发布后对学生隐藏 AI 原始建议）；分页与错误码结构。

本模块不访问数据库。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.error_codes import ErrorCode
from app.main import create_app

#: 导出文件位置（与 backend/scripts/export_openapi.py 一致）
OPENAPI_PATH = (
    Path(__file__).resolve().parents[3] / "contracts" / "openapi" / "openapi.json"
)

#: 第 9 节的八个接口 → 方法 → 声明的状态码（与 docs/api-contract.md 9.12 对应）
EXPECTED_OPERATIONS: dict[str, dict[str, set[str]]] = {
    "/api/v1/assignments/{assignment_id}/submissions/uploads": {
        "post": {"201", "401", "403", "404", "409", "422", "500"}
    },
    "/api/v1/assignments/{assignment_id}/submissions/uploads/{upload_id}/complete": {
        "post": {"201", "401", "403", "404", "409", "422", "500"}
    },
    "/api/v1/assignments/{assignment_id}/submissions": {
        "get": {"200", "401", "403", "404", "422", "500"}
    },
    "/api/v1/submissions/{submission_id}": {
        "get": {"200", "401", "403", "404", "422", "500", "503"}
    },
    "/api/v1/submissions/{submission_id}/grade": {
        "post": {"202", "401", "403", "404", "409", "422", "500"}
    },
    "/api/v1/submissions/{submission_id}/grade-review": {
        "get": {"200", "401", "403", "404", "409", "422", "500", "502"}
    },
    "/api/v1/grade-reviews/{review_id}": {
        "patch": {"200", "401", "403", "404", "409", "422", "500"}
    },
    "/api/v1/grade-reviews/{review_id}/publish": {
        "post": {"200", "401", "403", "404", "409", "422", "500"}
    },
}

#: 第 9 节新增的稳定错误码
NEW_ERROR_CODES = {
    ErrorCode.SUBMISSION_ALREADY_EXISTS,
    ErrorCode.SUBMISSION_NOT_READY,
    ErrorCode.GRADE_ALREADY_PUBLISHED,
}


@pytest.fixture(scope="module")
def schema() -> dict:
    return create_app().openapi()


def test_all_planned_paths_are_documented(schema: dict) -> None:
    for path in EXPECTED_OPERATIONS:
        assert path in schema["paths"], f"{path} 未出现在 OpenAPI 中"


def test_documented_status_codes_match_contract(schema: dict) -> None:
    for path, operations in EXPECTED_OPERATIONS.items():
        for method, expected_codes in operations.items():
            actual = set(schema["paths"][path][method]["responses"])
            assert actual == expected_codes, (
                f"{method.upper()} {path} 状态码不一致: {actual}"
            )


def test_new_error_codes_are_exposed_for_frontend(schema: dict) -> None:
    """三个新错误码必须出现在 ``ErrorCode`` 枚举里，前端才能做穷尽检查。"""
    enum_values = set(schema["components"]["schemas"]["ErrorCode"]["enum"])

    for code in NEW_ERROR_CODES:
        assert code.value in enum_values


def test_upload_init_request_body_is_declared(schema: dict) -> None:
    operation = schema["paths"][
        "/api/v1/assignments/{assignment_id}/submissions/uploads"
    ]["post"]
    ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]

    assert ref.endswith("/SubmissionUploadInitRequest")
    assert operation["requestBody"]["required"] is True
    assert "SubmissionUploadInitRequest" in schema["components"]["schemas"]


def test_upload_init_request_fields_are_strict_and_not_nullable(schema: dict) -> None:
    """上传请求字段都不是可空类型：显式 ``null`` 由运行时的校验器拒绝。"""
    definition = schema["components"]["schemas"]["SubmissionUploadInitRequest"]
    properties = definition["properties"]

    assert set(properties) == {"filename", "content_type", "size", "sha256"}
    assert definition.get("additionalProperties") is False
    assert set(definition["required"]) == set(properties)
    for name, field in properties.items():
        assert field.get("type") != "null", name
        assert not _is_nullable(field), f"{name} 不应是可空类型"
        assert "default" not in field or field["default"] is not None


def test_grade_review_request_body_is_declared(schema: dict) -> None:
    operation = schema["paths"]["/api/v1/grade-reviews/{review_id}"]["patch"]
    ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]

    assert ref.endswith("/GradeReviewUpdateRequest")
    assert "GradeReviewUpdateRequest" in schema["components"]["schemas"]


def test_grade_review_request_declares_full_snapshot(schema: dict) -> None:
    """复核请求是完整快照：summary 与 items 必填，items 声明数量边界。"""
    definition = schema["components"]["schemas"]["GradeReviewUpdateRequest"]

    assert set(definition["required"]) == {"summary", "items"}
    assert definition["properties"]["summary"]["minLength"] == 1
    items = definition["properties"]["items"]
    assert items["minItems"] == 1
    assert items["maxItems"] == 50


def test_score_fields_are_json_numbers_with_two_decimals(schema: dict) -> None:
    """分数只接受 JSON number，并以 0.01 为最小单位（对应 decimal_places=2）。"""
    field = schema["components"]["schemas"]["GradeItemReviewRequest"]["properties"][
        "final_score"
    ]

    assert field["type"] == "number"
    assert field["multipleOf"] == 0.01
    assert field["minimum"] == 0
    assert "exclusiveMinimum" not in field


def test_grade_item_request_requires_rubric_item_id(schema: dict) -> None:
    definition = schema["components"]["schemas"]["GradeItemReviewRequest"]

    assert set(definition["required"]) == {"rubric_item_id", "final_score"}
    assert definition["properties"]["rubric_item_id"]["format"] == "uuid"
    assert definition["properties"]["teacher_comment"]["maxLength"] == 2000


def test_student_visible_ai_fields_are_nullable(schema: dict) -> None:
    """学生发布后视角要隐藏 AI 原始建议，因此这些字段必须可为 ``null``。"""
    item = schema["components"]["schemas"]["GradeItemDetailSchema"]["properties"]
    review = schema["components"]["schemas"]["GradeReviewDetailSchema"]["properties"]

    assert _is_nullable(item["ai_score"])
    assert _is_nullable(item["ai_comment"])
    assert _is_nullable(review["suggested_total_score"])
    assert _is_nullable(review["ai_summary"])
    # 终稿字段对学生也必须存在，因此不是可空类型
    assert not _is_nullable(item["final_score"])
    assert not _is_nullable(review["final_total_score"])


def test_submission_detail_exposes_download_fields(schema: dict) -> None:
    detail = schema["components"]["schemas"]["SubmissionDetailSchema"]["properties"]

    assert "download_url" in detail
    assert "download_expires_at" in detail
    assert "sha256" in detail
    assert _is_nullable(detail["download_url"])
    # 未完成提交时 rubric_version 为 null
    assert _is_nullable(detail["rubric_version"])


def test_grade_item_detail_declares_evidence_fields(schema: dict) -> None:
    """批改项必须声明可核验的证据定位（契约 9.7/9.10）。

    证据摘录 + 来源类型（PDF_PAGE / DOCX_PARAGRAPH）+ 位置区间（从 1 开始），
    以及错误类型与改进建议，对教师始终返回，因此**不可为 null**。
    """
    item = schema["components"]["schemas"]["GradeItemDetailSchema"]["properties"]

    assert "evidence_quote" in item
    assert "evidence_source_type" in item
    assert set(item["evidence_source_type"]["enum"]) == {"PDF_PAGE", "DOCX_PARAGRAPH"}
    # 位置从 1 开始：导出为 integer + minimum 1
    for name in ("evidence_location_start", "evidence_location_end"):
        assert item[name]["type"] == "integer"
        assert item[name]["minimum"] == 1
    assert "error_type" in item
    assert "improvement_suggestion" in item

    for name in (
        "evidence_quote",
        "evidence_source_type",
        "evidence_location_start",
        "evidence_location_end",
        "error_type",
        "improvement_suggestion",
    ):
        assert not _is_nullable(item[name]), f"{name} 必须始终返回，不可为 null"


def test_submission_list_is_paginated(schema: dict) -> None:
    """提交列表复用统一分页结构 ``{items, page, page_size, total}``。"""
    operation = schema["paths"]["/api/v1/assignments/{assignment_id}/submissions"]["get"]
    ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    page_schema = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]

    assert set(page_schema["properties"]) == {"items", "page", "page_size", "total"}


def test_error_responses_reference_unified_schema(schema: dict) -> None:
    """错误响应必须是统一错误结构（前端按 ``code`` 分支）。

    ``422`` 例外：其中一部分来自 FastAPI 对路径/查询参数的自动校验
    （``HTTPValidationError``），这是全项目一致的既有形态；运行时的响应体仍由
    统一异常处理器转换为 ``ErrorResponse``（由集成测试逐项断言）。
    """
    for path, operations in EXPECTED_OPERATIONS.items():
        for method, codes in operations.items():
            for code in codes:
                if code in {"200", "201", "202"}:
                    continue
                response = schema["paths"][path][method]["responses"][code]
                content = response.get("content")
                if not content:
                    continue
                ref = content["application/json"]["schema"]["$ref"]
                if code == "422":
                    assert ref.endswith(("/ErrorResponse", "/HTTPValidationError"))
                    continue
                assert ref.endswith("/ErrorResponse"), f"{method} {path} {code}"


def test_exported_file_contains_grading_paths() -> None:
    """导出的契约文件必须包含第 9 节的路径（前端据此生成类型）。"""
    assert OPENAPI_PATH.exists(), (
        f"缺少导出文件 {OPENAPI_PATH}；请执行 scripts/export_openapi.py"
    )
    exported = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))

    for path in EXPECTED_OPERATIONS:
        assert path in exported["paths"], f"导出文件缺少 {path}"


def _is_nullable(field: dict) -> bool:
    """判断 JSON Schema 字段是否允许 ``null``（含 ``anyOf`` 分支写法）。"""
    if field.get("type") == "null":
        return True
    return any(
        branch.get("type") == "null" for branch in field.get("anyOf", [])
    )
