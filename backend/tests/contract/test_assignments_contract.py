"""实验任务接口的契约测试（不访问数据库）。

校验 OpenAPI 里对外承诺的形状：六个接口的路径与成功状态码、Bearer 安全声明、
请求体与响应组件、状态枚举、分页结构、可选空对象请求体与稳定错误码
（``docs/api-contract.md`` 第 8 节）。导出物与运行时代码的一致性由
``tests/contract/test_openapi_contract.py`` 统一守卫。

**另外把请求 Schema 的语义与运行时校验对齐**（不只是字段集合）：可空性、
JSON number、``minProperties`` 都由同一个模型对象驱动——先用 Schema 声明的
类型集合预测结果，再用 Pydantic 模型实际校验，两者必须一致。只断言"导出物
等于代码生成结果"无法发现"生成出的 Schema 与运行时规则相悖"这类缺陷。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.main import create_app
from app.modules.assignments.models import MAX_RUBRIC_ITEMS, MIN_RUBRIC_ITEMS
from app.modules.assignments.schemas import (
    AssignmentCreateRequest,
    AssignmentUpdateRequest,
    RubricItemRequest,
)

CREATE_PATH = "/api/v1/courses/{course_id}/assignments"
DETAIL_PATH = "/api/v1/assignments/{assignment_id}"
PUBLISH_PATH = "/api/v1/assignments/{assignment_id}/publish"
CLOSE_PATH = "/api/v1/assignments/{assignment_id}/close"

#: 分数的最小单位：契约 8.2 / 8.5 冻结为"最多两位小数"（0.01 的整数倍）
SCORE_STEP = 0.01


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


def _allows_null(field_schema: dict) -> bool:
    if field_schema.get("type") == "null":
        return True
    types = field_schema.get("type")
    if isinstance(types, list) and "null" in types:
        return True
    return any(
        branch.get("type") == "null" for branch in field_schema.get("anyOf", [])
    )


def _accepted_types(field_schema: dict) -> set[str]:
    """Schema 接受哪些 JSON 类型（展开 ``anyOf``，只看 ``type`` 关键字）。

    ``type`` 为数组时按集合理解；嵌套 ``anyOf`` 递归展开。用于把"Schema 声明"
    与"运行时行为"对照，避免只比较字段名而漏掉类型层面的不一致。
    """
    declared = field_schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return set(declared)
    types: set[str] = set()
    for branch in field_schema.get("anyOf", []):
        types |= _accepted_types(branch)
    return types


def _valid_create_payload() -> dict:
    return {
        "title": "实验一 需求分析",
        "description": "任务说明",
        "total_score": 100,
        "allow_late_submission": False,
        "rubric_items": [{"title": "需求完整性", "max_score": 100, "order": 1}],
    }


def _declares_type(field_schema: object, target: str) -> bool:
    """递归检查 Schema 的任意层级是否声明了某种 JSON 类型。"""
    if isinstance(field_schema, dict):
        if field_schema.get("type") == target:
            return True
        return any(_declares_type(value, target) for value in field_schema.values())
    if isinstance(field_schema, list):
        return any(_declares_type(value, target) for value in field_schema)
    return False


def _runtime_accepts(model: type, payload: dict) -> bool:
    try:
        model.model_validate(payload)
    except ValidationError:
        return False
    return True


def _is_multiple_of(value: float, step: float) -> bool:
    """按**十进制**判定 ``multipleOf``（对应运行时的"最多两位小数"）。"""
    return Decimal(str(value)) % Decimal(str(step)) == 0


# --------------------------------------------------------------------------- #
# 路径与状态码
# --------------------------------------------------------------------------- #
def test_six_assignment_endpoints_are_documented(schema: dict) -> None:
    """契约 8.2–8.7：六个接口与各自成功状态码。"""
    paths = schema["paths"]
    expected = {
        (CREATE_PATH, "post"): "201",
        (CREATE_PATH, "get"): "200",
        (DETAIL_PATH, "get"): "200",
        (DETAIL_PATH, "patch"): "200",
        (PUBLISH_PATH, "post"): "200",
        (CLOSE_PATH, "post"): "200",
    }
    for (path, method), status_code in expected.items():
        assert path in paths, f"缺少路径 {path}"
        assert method in paths[path], f"{path} 缺少 {method.upper()}"
        assert status_code in paths[path][method]["responses"]


def test_assignment_endpoints_require_bearer(schema: dict) -> None:
    for path, method in (
        (CREATE_PATH, "post"),
        (CREATE_PATH, "get"),
        (DETAIL_PATH, "get"),
        (DETAIL_PATH, "patch"),
        (PUBLISH_PATH, "post"),
        (CLOSE_PATH, "post"),
    ):
        operation = schema["paths"][path][method]
        assert operation.get("security"), f"{method.upper()} {path} 未声明 Bearer"
        assert "401" in operation["responses"]


@pytest.mark.parametrize(
    ("path", "method"),
    [
        (CREATE_PATH, "post"),
        (DETAIL_PATH, "patch"),
        (PUBLISH_PATH, "post"),
        (CLOSE_PATH, "post"),
    ],
)
def test_write_endpoints_declare_business_errors(
    schema: dict, path: str, method: str
) -> None:
    """写接口声明 403 / 404 / 409 / 422（契约 8.11）。"""
    operation = schema["paths"][path][method]
    assert {"403", "404", "409", "422"} <= operation["responses"].keys()


def test_read_endpoints_declare_404(schema: dict) -> None:
    assert "404" in schema["paths"][CREATE_PATH]["get"]["responses"]
    assert "404" in schema["paths"][DETAIL_PATH]["get"]["responses"]


# --------------------------------------------------------------------------- #
# 请求体
# --------------------------------------------------------------------------- #
def test_create_request_body_shape(schema: dict) -> None:
    component = _resolve(schema, _body_schema(schema, CREATE_PATH, "post"))

    assert set(component["properties"]) == {
        "title",
        "description",
        "total_score",
        "due_at",
        "allow_late_submission",
        "rubric_items",
    }
    assert set(component.get("required", [])) == {"title", "total_score", "rubric_items"}
    assert component.get("additionalProperties") is False
    assert _allows_null(component["properties"]["due_at"]), "due_at 必须允许 null"

    items = component["properties"]["rubric_items"]
    assert items.get("minItems") == MIN_RUBRIC_ITEMS
    assert items.get("maxItems") == MAX_RUBRIC_ITEMS

    item = _resolve(schema, items["items"])
    assert set(item["properties"]) == {"title", "description", "max_score", "order"}
    assert set(item.get("required", [])) == {"title", "max_score", "order"}


def test_update_request_body_is_all_optional(schema: dict) -> None:
    component = _resolve(schema, _body_schema(schema, DETAIL_PATH, "patch"))

    assert set(component["properties"]) == {
        "title",
        "description",
        "total_score",
        "due_at",
        "allow_late_submission",
        "rubric_items",
    }
    assert component.get("required", []) == []
    assert component.get("additionalProperties") is False
    # 空对象 {} 运行时返回 422，Schema 必须同样拒绝
    assert component.get("minProperties") == 1


def test_update_fields_are_not_nullable_except_due_at(schema: dict) -> None:
    """修改接口除 ``due_at`` 外都不接受显式 ``null``（契约 8.5）。"""
    component = _resolve(schema, _body_schema(schema, DETAIL_PATH, "patch"))

    for name, field_schema in component["properties"].items():
        if name == "due_at":
            assert _allows_null(field_schema), "due_at: null 表示清除截止时间"
            continue
        assert not _allows_null(field_schema), f"{name} 不应声明为可空"
        # 默认值也不能是 null：运行时把显式 null 判为 422
        assert "default" not in field_schema, f"{name} 不应声明 default: null"


def test_create_fields_are_not_nullable_except_due_at(schema: dict) -> None:
    """创建接口同样只有 ``due_at`` 允许显式 ``null``（契约 8.2）。"""
    component = _resolve(schema, _body_schema(schema, CREATE_PATH, "post"))

    for name, field_schema in component["properties"].items():
        assert _allows_null(field_schema) is (name == "due_at"), name


def _score_field_schemas(schema: dict) -> list[tuple[str, dict]]:
    """三个分数字段的 Schema：创建总分、修改总分、评分项分值。"""
    create = _resolve(schema, _body_schema(schema, CREATE_PATH, "post"))
    update = _resolve(schema, _body_schema(schema, DETAIL_PATH, "patch"))
    item = _resolve(schema, create["properties"]["rubric_items"]["items"])
    return [
        ("create.total_score", create["properties"]["total_score"]),
        ("update.total_score", update["properties"]["total_score"]),
        ("rubric_item.max_score", item["properties"]["max_score"]),
    ]


def test_score_fields_accept_only_json_number(schema: dict) -> None:
    """``total_score`` / ``max_score`` 只声明 JSON number（拒绝字符串与布尔）。"""
    for name, field_schema in _score_field_schemas(schema):
        assert _accepted_types(field_schema) == {"number"}, name
        assert field_schema["exclusiveMinimum"] == 0, name
        assert field_schema["maximum"] > 0, name
        # Pydantic 默认的 number|string 分支必须被覆盖掉
        assert not _declares_type(field_schema, "string"), name


def test_score_fields_declare_multiple_of_one_cent(schema: dict) -> None:
    """三个分数字段都声明 ``multipleOf: 0.01``（对应 ``decimal_places=2``）。"""
    for name, field_schema in _score_field_schemas(schema):
        assert field_schema.get("multipleOf") == SCORE_STEP, name


def test_title_schema_does_not_declare_length_limits(schema: dict) -> None:
    """标题只声明 ``string``：长度规则是"先 trim 再判 1–200"，Schema 表达不了。

    若声明 ``maxLength: 200``，客户端会拒掉运行时可接受的
    ``"<空格> + 200 字符 + <空格>"``（去除空白后恰好 200），因此这里显式回归。
    """
    create = _resolve(schema, _body_schema(schema, CREATE_PATH, "post"))
    update = _resolve(schema, _body_schema(schema, DETAIL_PATH, "patch"))
    item = _resolve(schema, create["properties"]["rubric_items"]["items"])

    for name, field_schema in (
        ("create.title", create["properties"]["title"]),
        ("update.title", update["properties"]["title"]),
        ("rubric_item.title", item["properties"]["title"]),
    ):
        assert _accepted_types(field_schema) == {"string"}, name
        assert "maxLength" not in field_schema, name
        assert "pattern" not in field_schema, name
        assert "去除首尾空白" in field_schema.get("description", ""), name


# --------------------------------------------------------------------------- #
# Schema 声明 ↔ 运行时行为（语义一致性，而不是字段集合）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("model", "baseline", "nullable"),
    [
        (AssignmentCreateRequest, _valid_create_payload(), {"due_at"}),
        (AssignmentUpdateRequest, {"title": "实验一"}, {"due_at"}),
    ],
)
def test_declared_nullability_matches_runtime(
    model: type, baseline: dict, nullable: set[str]
) -> None:
    """Schema 说"可空"的字段运行时必须接受 ``null``，反之必须拒绝。"""
    for name, field_schema in model.model_json_schema()["properties"].items():
        payload: dict[str, Any] = dict(baseline)
        payload[name] = None
        expected = name in nullable
        assert _allows_null(field_schema) is expected, f"{model.__name__}.{name} 声明"
        assert _runtime_accepts(model, payload) is expected, f"{model.__name__}.{name} 运行时"


def test_declared_score_types_match_runtime() -> None:
    """Schema 只允许 number：字符串与布尔必须被运行时拒绝。"""
    create = AssignmentCreateRequest.model_json_schema()["properties"]["total_score"]
    assert _accepted_types(create) == {"number"}

    valid = _valid_create_payload()
    assert _runtime_accepts(AssignmentCreateRequest, valid)
    for forbidden in ("100", True, float("nan")):
        assert not _runtime_accepts(
            AssignmentCreateRequest, {**valid, "total_score": forbidden}
        ), forbidden
    # 评分项分值同样只接受 number
    item = RubricItemRequest.model_json_schema()["properties"]["max_score"]
    assert _accepted_types(item) == {"number"}
    assert not _runtime_accepts(
        RubricItemRequest, {"title": "A", "max_score": "100", "order": 1}
    )


def test_declared_min_properties_matches_runtime() -> None:
    """``minProperties: 1`` 对应运行时的"空对象 422"。"""
    update = AssignmentUpdateRequest.model_json_schema()
    assert update["minProperties"] == 1
    assert not _runtime_accepts(AssignmentUpdateRequest, {})
    assert _runtime_accepts(AssignmentUpdateRequest, {"due_at": None})


def test_declared_score_precision_matches_runtime() -> None:
    """``multipleOf: 0.01`` 与运行时 ``decimal_places=2`` 一致（三位小数必须双方拒绝）。

    ``multipleOf`` 按**十进制**判定：浮点实现的校验器用二进制除法
    （``40.55 / 0.01 == 4054.999…``）会把合法值判为非法，那是工具的缺陷而不是契约，
    因此这里只比较声明值与十进制语义，另用真实模型确认运行时行为。
    """
    for name, field_schema in _score_field_schemas(dict(create_app().openapi())):
        assert field_schema.get("multipleOf") == SCORE_STEP, name

    valid = _valid_create_payload()
    for accepted in (40.55, 33.33, 100, 0.01):
        assert _is_multiple_of(accepted, SCORE_STEP), accepted
        assert _runtime_accepts(
            AssignmentCreateRequest,
            {
                **valid,
                "total_score": accepted,
                "rubric_items": [{"title": "A", "max_score": accepted, "order": 1}],
            },
        ), accepted

    for rejected in (100.001, 1.001, 0.005):
        assert not _is_multiple_of(rejected, SCORE_STEP), rejected
        cases = (
            (
                AssignmentCreateRequest,
                {
                    **valid,
                    "total_score": rejected,
                    "rubric_items": [
                        {"title": "A", "max_score": rejected, "order": 1}
                    ],
                },
            ),
            (AssignmentUpdateRequest, {"total_score": rejected}),
            (RubricItemRequest, {"title": "A", "max_score": rejected, "order": 1}),
        )
        for model, payload in cases:
            assert not _runtime_accepts(model, payload), (model.__name__, rejected)


def test_publish_and_close_declare_optional_object(schema: dict) -> None:
    for path in (PUBLISH_PATH, CLOSE_PATH):
        request_body = schema["paths"][path]["post"]["requestBody"]
        assert request_body.get("required") in (None, False)
        body = request_body["content"]["application/json"]["schema"]
        assert body.get("type") == "object"
        assert "anyOf" not in body, "省略合法，但显式 null 不合法"


# --------------------------------------------------------------------------- #
# 响应组件
# --------------------------------------------------------------------------- #
def test_assignment_summary_fields(schema: dict) -> None:
    summary = _component(schema, "AssignmentSummarySchema")
    assert set(summary["properties"]) == {
        "id",
        "course_id",
        "title",
        "total_score",
        "due_at",
        "allow_late_submission",
        "status",
        "rubric_version",
        "published_at",
        "closed_at",
        "created_at",
        "updated_at",
    }


def test_assignment_detail_adds_description_and_rubric(schema: dict) -> None:
    detail = _component(schema, "AssignmentDetailSchema")
    assert set(detail["properties"]) == set(
        _component(schema, "AssignmentSummarySchema")["properties"]
    ) | {"description", "rubric_items"}


def test_rubric_item_component_fields(schema: dict) -> None:
    item = _component(schema, "RubricItemSchema")
    assert set(item["properties"]) == {
        "id",
        "title",
        "description",
        "max_score",
        "order",
    }


def test_status_enum_values(schema: dict) -> None:
    enum = set(_component(schema, "AssignmentStatus")["enum"])
    assert enum == {"DRAFT", "PUBLISHED", "CLOSED", "ARCHIVED"}


def test_list_uses_page_of_summaries(schema: dict) -> None:
    operation = schema["paths"][CREATE_PATH]["get"]
    ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    component = _component(schema, ref.rsplit("/", 1)[-1])

    assert set(component["properties"]) == {"items", "page", "page_size", "total"}
    item_ref = component["properties"]["items"]["items"]["$ref"]
    assert item_ref.endswith("/AssignmentSummarySchema")


# --------------------------------------------------------------------------- #
# 错误码
# --------------------------------------------------------------------------- #
def test_error_code_enum_includes_assignment_codes(schema: dict) -> None:
    enum = set(_component(schema, "ErrorCode")["enum"])
    assert {"RUBRIC_SCORE_MISMATCH", "ASSIGNMENT_NOT_OPEN"} <= enum
    assert "COURSE_ARCHIVED" in enum and "ROLE_FORBIDDEN" in enum
