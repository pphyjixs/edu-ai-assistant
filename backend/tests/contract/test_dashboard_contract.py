"""Dashboard 接口契约测试（``docs/api-contract.md`` 第 11 节）。

只校验 OpenAPI 对外承诺的形状：两条路径、状态码、Bearer 安全声明、
无请求体与查询参数、响应组件的字段必填性、数组上限、时间格式、分数约束，
以及导出的 ``contracts/openapi/openapi.json`` 与生成类型语义一致。

本模块不访问数据库。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.main import create_app

OPENAPI_PATH = (
    Path(__file__).resolve().parents[3] / "contracts" / "openapi" / "openapi.json"
)
TYPES_PATH = (
    Path(__file__).resolve().parents[3] / "contracts" / "generated" / "api-types.ts"
)

TEACHER_PATH = "/api/v1/dashboard/teacher"
STUDENT_PATH = "/api/v1/dashboard/student"

TEACHER_EXPECTED_FIELDS = {
    "active_course_count",
    "pending_grading_count",
    "failed_material_count",
    "recent_submissions",
    "failed_materials",
}
STUDENT_EXPECTED_FIELDS = {
    "active_course_count",
    "pending_assignment_count",
    "processing_material_count",
    "failed_material_count",
    "pending_assignments",
    "recent_feedback",
    "material_statuses",
}


@pytest.fixture(scope="module")
def schema() -> dict:
    return create_app().openapi()


@pytest.fixture(scope="module")
def exported() -> dict:
    with OPENAPI_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_both_paths_are_get_with_expected_status_codes(schema: dict) -> None:
    for path in (TEACHER_PATH, STUDENT_PATH):
        assert set(schema["paths"][path]) == {"get"}
        assert set(schema["paths"][path]["get"]["responses"]) >= {"200", "401", "403"}


def test_both_endpoints_require_bearer_token(schema: dict) -> None:
    schemes = schema["components"]["securitySchemes"]
    assert schemes["HTTPBearer"]["scheme"] == "bearer"

    for path in (TEACHER_PATH, STUDENT_PATH):
        operation = schema["paths"][path]["get"]
        assert {"HTTPBearer": []} in operation["security"]


def test_endpoints_have_no_request_body_or_parameters(schema: dict) -> None:
    for path in (TEACHER_PATH, STUDENT_PATH):
        operation = schema["paths"][path]["get"]
        assert "requestBody" not in operation
        assert "parameters" not in operation


def test_teacher_dashboard_component_shape(schema: dict) -> None:
    definition = schema["components"]["schemas"]["TeacherDashboard"]
    assert set(definition["properties"]) == TEACHER_EXPECTED_FIELDS
    assert set(definition["required"]) == TEACHER_EXPECTED_FIELDS


def test_student_dashboard_component_shape(schema: dict) -> None:
    definition = schema["components"]["schemas"]["StudentDashboard"]
    assert set(definition["properties"]) == STUDENT_EXPECTED_FIELDS
    assert set(definition["required"]) == STUDENT_EXPECTED_FIELDS


def test_recent_lists_declare_five_item_cap(schema: dict) -> None:
    teacher = schema["components"]["schemas"]["TeacherDashboard"]["properties"]
    student = schema["components"]["schemas"]["StudentDashboard"]["properties"]

    assert teacher["recent_submissions"]["maxItems"] == 5
    assert teacher["failed_materials"]["maxItems"] == 5
    assert student["pending_assignments"]["maxItems"] == 5
    assert student["recent_feedback"]["maxItems"] == 5
    assert student["material_statuses"]["maxItems"] == 5


def test_time_fields_declare_date_time_format(schema: dict) -> None:
    components = schema["components"]["schemas"]
    submission = components["TeacherRecentSubmission"]["properties"]["submitted_at"]
    assert submission["type"] == "string"
    assert submission["format"] == "date-time"

    feedback = components["StudentRecentFeedback"]["properties"]["published_at"]
    assert feedback["type"] == "string"
    assert feedback["format"] == "date-time"


def test_score_fields_declare_number_with_two_decimals(schema: dict) -> None:
    properties = schema["components"]["schemas"]["StudentRecentFeedback"]["properties"]
    for name in ("final_total_score", "total_score"):
        prop = properties[name]
        assert prop["type"] == "number"
        assert prop["multipleOf"] == 0.01
        assert prop["minimum"] == 0


def test_status_enums_match_contract_section_11(schema: dict) -> None:
    """两个状态字段的 OpenAPI 枚举集合必须与契约 §11 完全一致（不复用完整领域枚举）。"""
    components = schema["components"]["schemas"]
    submission_status = components["TeacherRecentSubmission"]["properties"]["status"]
    material_status = components["StudentMaterialStatus"]["properties"]["status"]

    # 内联枚举：不引用完整的 SubmissionStatus / MaterialStatus 组件
    assert "$ref" not in submission_status
    assert "$ref" not in material_status
    assert submission_status["type"] == "string"
    assert material_status["type"] == "string"

    assert set(submission_status["enum"]) == {
        "SUBMITTED",
        "GRADING",
        "REVIEW_REQUIRED",
        "PUBLISHED",
        "FAILED",
    }
    assert set(material_status["enum"]) == {"PROCESSING", "FAILED"}


def test_exported_file_matches_runtime(schema: dict, exported: dict) -> None:
    assert exported["paths"][TEACHER_PATH] == schema["paths"][TEACHER_PATH]
    assert exported["paths"][STUDENT_PATH] == schema["paths"][STUDENT_PATH]
    assert (
        exported["components"]["schemas"]["TeacherDashboard"]
        == schema["components"]["schemas"]["TeacherDashboard"]
    )
    assert (
        exported["components"]["schemas"]["StudentDashboard"]
        == schema["components"]["schemas"]["StudentDashboard"]
    )


def test_generated_types_cover_dashboard_surface() -> None:
    content = TYPES_PATH.read_text(encoding="utf-8")

    assert "TeacherDashboard:" in content
    assert "StudentDashboard:" in content
    assert "/api/v1/dashboard/teacher" in content
    assert "/api/v1/dashboard/student" in content

    # 两个状态字段的联合类型已按契约 §11 收窄，且不再是被排除的状态
    assert (
        'status: "SUBMITTED" | "GRADING" | "REVIEW_REQUIRED" | "PUBLISHED" | "FAILED";'
        in content
    )
    assert 'status: "PROCESSING" | "FAILED";' in content
