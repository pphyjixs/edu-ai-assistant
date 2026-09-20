"""课件上传接口的契约测试（不访问数据库）。

只校验 OpenAPI 里对外承诺的形状：路径、状态码、Bearer 安全声明、
响应组件与枚举取值。前端据此生成 TypeScript 类型（``contracts/generated/api-types.ts``），
因此字段缺失或改名必须在这里被拦住。
"""

from __future__ import annotations

import pytest

from app.main import create_app

INIT_PATH = "/api/v1/courses/{course_id}/materials/uploads"
COMPLETE_PATH = "/api/v1/courses/{course_id}/materials/uploads/{upload_id}/complete"
MATERIAL_PATH = "/api/v1/materials/{material_id}"
JOB_PATH = "/api/v1/jobs/{job_id}"


@pytest.fixture(scope="module")
def schema() -> dict:
    return create_app().openapi()


def _component(schema: dict, name: str) -> dict:
    components = schema.get("components", {}).get("schemas", {})
    assert name in components, f"缺少响应组件 {name}"
    return components[name]


def test_both_write_endpoints_are_documented(schema: dict) -> None:
    assert INIT_PATH in schema["paths"]
    assert COMPLETE_PATH in schema["paths"]

    assert schema["paths"][INIT_PATH]["post"]["responses"].keys() >= {"201"}
    assert schema["paths"][COMPLETE_PATH]["post"]["responses"].keys() >= {"202"}


def test_write_endpoints_require_bearer_token(schema: dict) -> None:
    """两个接口都必须挂在 Bearer 认证下（匿名请求 401）。"""
    security_schemes = schema["components"]["securitySchemes"]
    assert "HTTPBearer" in security_schemes
    assert security_schemes["HTTPBearer"]["scheme"] == "bearer"

    for path in (INIT_PATH, COMPLETE_PATH):
        operation = schema["paths"][path]["post"]
        assert operation.get("security"), f"{path} 缺少安全声明"
        assert {"401"} <= operation["responses"].keys()


def test_init_response_exposes_presigned_fields(schema: dict) -> None:
    """初始化响应必须给出直传所需的四个关键字段与两个期限。"""
    ref = schema["paths"][INIT_PATH]["post"]["responses"]["201"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert ref.endswith("/MaterialUploadInitResponse")

    properties = _component(schema, "MaterialUploadInitResponse")["properties"]
    for field in (
        "upload_id",
        "upload_url",
        "method",
        "headers",
        "expires_at",
        "confirm_deadline_at",
    ):
        assert field in properties, f"初始化响应缺少字段 {field}"


def test_complete_response_keeps_material_and_job_shape(schema: dict) -> None:
    """完成响应固定为 material + job，且两者都不含敏感字段。"""
    properties = _component(schema, "MaterialUploadCompleteResponse")["properties"]
    assert set(properties) == {"material", "job"}

    material = _component(schema, "MaterialDetail")["properties"]
    assert set(material) == {
        "id",
        "course_id",
        "filename",
        "content_type",
        "size",
        "status",
        "uploaded_by",
        "error_message",
        "created_at",
        "updated_at",
    }
    # 对象键、签名地址等内部信息不得出现在资料响应里
    assert "storage_key" not in material
    assert "object_key" not in material

    job = _component(schema, "JobStatus")["properties"]
    assert set(job) == {
        "id",
        "type",
        "status",
        "progress",
        "resource_type",
        "resource_id",
        "error",
        "created_at",
        "started_at",
        "finished_at",
    }


def test_status_enums_expose_processing_and_pending(schema: dict) -> None:
    """第一版没有 Worker：前端必须能表达 PROCESSING 与 PENDING。"""
    material_status = _component(schema, "MaterialStatus")
    assert set(material_status["enum"]) == {
        "UPLOADING",
        "UPLOADED",
        "PROCESSING",
        "READY",
        "FAILED",
    }

    job_status = _component(schema, "JobStatusValue")
    assert set(job_status["enum"]) == {
        "PENDING",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
    }


def test_error_code_enum_includes_upload_invalid(schema: dict) -> None:
    error_code = _component(schema, "ErrorCode")
    assert "UPLOAD_INVALID" in error_code["enum"]
    assert "SERVICE_UNAVAILABLE" in error_code["enum"]


def test_component_names_are_stable_for_codegen(schema: dict) -> None:
    """组件名是前端生成类型的契约：改名等于破坏前端。

    ``JobStatus`` 既是 ORM 枚举（取值域）又是响应模型，pydantic 会为后者加后缀，
    这里固定住两者的实际名字，改名时用例会失败而不是静默生成两套类型。
    """
    components = set(schema.get("components", {}).get("schemas", {}))
    assert {"MaterialDetail", "MaterialUploadInitResponse"} <= components
    assert "JobStatus" in components  # 响应模型
    assert "JobStatusValue" in components  # ORM 枚举


def test_status_query_endpoints_are_documented(schema: dict) -> None:
    """契约 4.7：资料详情与任务状态两个查询接口必须出现在 OpenAPI。"""
    assert MATERIAL_PATH in schema["paths"]
    assert JOB_PATH in schema["paths"]

    assert schema["paths"][MATERIAL_PATH]["get"]["responses"].keys() >= {"200"}
    assert schema["paths"][JOB_PATH]["get"]["responses"].keys() >= {"200"}


def test_status_query_endpoints_require_bearer(schema: dict) -> None:
    """两个查询接口都必须挂 Bearer 认证，且非成员/不存在统一 404。"""
    for path in (MATERIAL_PATH, JOB_PATH):
        operation = schema["paths"][path]["get"]
        assert operation.get("security"), f"{path} 缺少安全声明"
        assert {"401"} <= operation["responses"].keys()
        assert {"404"} <= operation["responses"].keys()


def test_material_detail_is_reused_by_status_query(schema: dict) -> None:
    """资料详情查询复用 ``MaterialDetail``，任务查询复用 ``JobStatus``。"""
    material_ref = schema["paths"][MATERIAL_PATH]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert material_ref.endswith("/MaterialDetail")

    job_ref = schema["paths"][JOB_PATH]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    assert job_ref.endswith("/JobStatus")
