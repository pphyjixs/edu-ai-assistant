"""课程练习接口的契约测试（不访问数据库）。

只校验 OpenAPI 里对外承诺的形状：六个接口的路径与成功状态码、Bearer 安全
声明、请求体与响应组件、字段可见性（学生/教师差异字段）以及稳定错误码枚举
（``docs/api-contract.md`` 第 7 与 10 节）。
"""

from __future__ import annotations

import pytest

from app.main import create_app

GENERATE_PATH = "/api/v1/courses/{course_id}/practice-sets/generate"
SETS_PATH = "/api/v1/courses/{course_id}/practice-sets"
SET_PATH = "/api/v1/practice-sets/{set_id}"
PUBLISH_PATH = "/api/v1/practice-sets/{set_id}/publish"
ATTEMPTS_PATH = "/api/v1/practice-sets/{set_id}/attempts"
RESULT_PATH = "/api/v1/practice-attempts/{attempt_id}"
RETRY_PATH = "/api/v1/jobs/{job_id}/retry"


@pytest.fixture(scope="module")
def schema() -> dict:
    return create_app().openapi()


def _component(schema: dict, name: str) -> dict:
    components = schema.get("components", {}).get("schemas", {})
    assert name in components, f"缺少响应组件 {name}"
    return components[name]


def _body_schema(schema: dict, path: str, method: str) -> dict:
    operation = schema["paths"][path][method]
    return operation["requestBody"]["content"]["application/json"]["schema"]


def _resolve(schema: dict, ref_or_schema: dict) -> dict:
    if "$ref" in ref_or_schema:
        return _component(schema, ref_or_schema["$ref"].rsplit("/", 1)[-1])
    return ref_or_schema


# --------------------------------------------------------------------------- #
# 路径与状态码
# --------------------------------------------------------------------------- #
def test_six_practice_endpoints_are_documented(schema: dict) -> None:
    """契约 7.2–7.7：六个练习接口与各自成功状态码。"""
    paths = schema["paths"]
    expected = {
        GENERATE_PATH: ("post", "202"),
        SETS_PATH: ("get", "200"),
        SET_PATH: ("get", "200"),
        PUBLISH_PATH: ("post", "200"),
        ATTEMPTS_PATH: ("post", "201"),
        RESULT_PATH: ("get", "200"),
    }
    for path, (method, status_code) in expected.items():
        assert path in paths, f"缺少路径 {path}"
        assert method in paths[path], f"{path} 缺少 {method.upper()}"
        assert status_code in paths[path][method]["responses"]


def test_retry_endpoint_is_documented(schema: dict) -> None:
    """契约 10.1：任务重试接口返回 202 并声明冲突错误。"""
    operation = schema["paths"][RETRY_PATH]["post"]
    assert {"202", "403", "404", "409", "422"} <= operation["responses"].keys()


def test_practice_endpoints_require_bearer(schema: dict) -> None:
    """所有练习接口与重试接口都要求 Bearer 认证。"""
    for path, method in (
        (GENERATE_PATH, "post"),
        (SETS_PATH, "get"),
        (SET_PATH, "get"),
        (PUBLISH_PATH, "post"),
        (ATTEMPTS_PATH, "post"),
        (RESULT_PATH, "get"),
        (RETRY_PATH, "post"),
    ):
        operation = schema["paths"][path][method]
        assert operation.get("security"), f"{method.upper()} {path} 未声明 Bearer"
        assert "401" in operation["responses"]


def test_generate_declares_business_errors(schema: dict) -> None:
    """生成接口声明 403 / 404 / 409 / 422（契约 7.2 与 7.11）。"""
    operation = schema["paths"][GENERATE_PATH]["post"]
    assert {"403", "404", "409", "422"} <= operation["responses"].keys()


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
def test_generate_request_body_shape(schema: dict) -> None:
    """生成请求只含四个字段，并拒绝未声明字段。"""
    component = _resolve(schema, _body_schema(schema, GENERATE_PATH, "post"))

    assert set(component["properties"]) == {
        "material_ids",
        "question_count",
        "question_types",
        "difficulty",
    }
    assert set(component.get("required", [])) == {
        "material_ids",
        "question_count",
        "question_types",
        "difficulty",
    }
    assert component.get("additionalProperties") is False


def test_empty_body_endpoints_declare_optional_object(schema: dict) -> None:
    """发布与重试没有请求字段：声明为可省略的对象（非 nullable）。"""
    for path in (PUBLISH_PATH, RETRY_PATH):
        request_body = schema["paths"][path]["post"]["requestBody"]
        assert request_body.get("required") in (None, False)
        body = request_body["content"]["application/json"]["schema"]
        assert body.get("type") == "object"
        assert "anyOf" not in body, "省略合法，但显式 null 不合法"
        assert body.get("nullable") is not True


def test_submit_request_requires_answers(schema: dict) -> None:
    """提交答案请求只有 ``answers``，每项含 ``question_id`` 与 ``answer``。"""
    component = _resolve(schema, _body_schema(schema, ATTEMPTS_PATH, "post"))

    assert set(component["properties"]) == {"answers"}
    assert component.get("additionalProperties") is False

    item = _resolve(schema, component["properties"]["answers"]["items"])
    assert set(item["properties"]) == {"question_id", "answer"}


# --------------------------------------------------------------------------- #
# 响应组件
# --------------------------------------------------------------------------- #
def test_practice_set_summary_and_detail_fields(schema: dict) -> None:
    """摘要字段集与契约 7.8 一致；详情在摘要基础上只多 ``questions``。"""
    summary = _component(schema, "PracticeSetSummarySchema")
    assert set(summary["properties"]) == {
        "id",
        "course_id",
        "title",
        "status",
        "difficulty",
        "question_count",
        "question_types",
        "created_at",
        "updated_at",
        "published_at",
    }

    detail = _component(schema, "PracticeSetSchema")
    assert set(detail["properties"]) == set(summary["properties"]) | {"questions"}


def test_practice_question_fields_include_teacher_only_answers(schema: dict) -> None:
    """题目公共字段 + 教师专有字段（学生响应中为 null / 空数组）。"""
    question = _component(schema, "PracticeQuestionSchema")
    assert set(question["properties"]) >= {
        "id",
        "order",
        "type",
        "prompt",
        "options",
        "knowledge_point",
        "correct_answer",
        "grading_points",
        "explanation",
    }

    option = _component(schema, "PracticeOptionSchema")
    assert set(option["properties"]) == {"id", "text"}

    grading_point = _component(schema, "PracticeGradingPointSchema")
    assert set(grading_point["properties"]) == {"point", "accepted"}

    # 题型枚举与契约 7.2 一致
    question_type = _component(schema, "PracticeQuestionType")
    assert set(question_type["enum"]) == {
        "SINGLE_CHOICE",
        "TRUE_FALSE",
        "SHORT_ANSWER",
    }


def test_attempt_result_fields(schema: dict) -> None:
    """答题结果字段集（契约 7.7）。"""
    result = _component(schema, "PracticeAttemptResultSchema")
    assert set(result["properties"]) == {
        "id",
        "practice_set_id",
        "student_id",
        "total_score",
        "submitted_at",
        "answers",
    }

    answer = _component(schema, "PracticeAttemptAnswerSchema")
    assert set(answer["properties"]) == {
        "question_id",
        "question_order",
        "type",
        "prompt",
        "submitted_answer",
        "is_correct",
        "score",
        "correct_answer",
        "explanation",
        "knowledge_point",
    }


def test_published_list_uses_page_of_summaries(schema: dict) -> None:
    """已发布列表复用统一分页，元素为摘要（不含题目）。"""
    operation = schema["paths"][SETS_PATH]["get"]
    ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    component = _component(schema, ref.rsplit("/", 1)[-1])

    assert set(component["properties"]) == {"items", "page", "page_size", "total"}
    item_ref = component["properties"]["items"]["items"]["$ref"]
    assert item_ref.endswith("/PracticeSetSummarySchema")


# --------------------------------------------------------------------------- #
# 任务与错误码
# --------------------------------------------------------------------------- #
def test_job_status_documents_practice_task(schema: dict) -> None:
    """任务响应能表达练习生成任务（契约 7.2 / 10.1）。"""
    job = _component(schema, "JobStatus")
    assert {"id", "type", "status", "progress", "resource_type", "resource_id"} <= set(
        job["properties"]
    )
    assert "PRACTICE_GENERATE" in _component(schema, "JobType")["enum"]
    assert "PRACTICE_SET" in _component(schema, "JobResourceType")["enum"]


def test_error_code_enum_includes_practice_codes(schema: dict) -> None:
    """契约 7.11 / 10.1 新增的三个稳定错误码必须出现在枚举里。"""
    enum = set(_component(schema, "ErrorCode")["enum"])
    assert {"PRACTICE_NOT_READY", "PRACTICE_ALREADY_ATTEMPTED", "JOB_NOT_RETRYABLE"} <= enum
