"""OpenAPI 契约测试。

两块内容：

1. **导出物一致性**：``contracts/openapi/openapi.json`` 必须与当前代码生成的结果
   完全一致。接口改了却忘记重新导出时，这个用例会失败——因为前端是按导出文件
   生成类型的，导出文件过期就等于前后端契约悄悄分叉。
2. **契约覆盖**：``docs/api-contract.md`` 中已实现的路径、状态码与统一错误结构
   必须出现在 OpenAPI 里。

本模块不访问数据库。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI

from app.core.error_codes import ErrorCode
from app.main import create_app

#: 导出文件位置（与 backend/scripts/export_openapi.py 一致）
OPENAPI_PATH = (
    Path(__file__).resolve().parents[3] / "contracts" / "openapi" / "openapi.json"
)

#: 契约中已实现的路径 → 允许的状态码
EXPECTED_OPERATIONS: dict[str, tuple[str, set[str]]] = {
    "/api/v1/auth/register": ("post", {"201", "409", "422", "500"}),
    "/api/v1/auth/login": ("post", {"200", "401", "422", "429", "500"}),
    "/api/v1/auth/refresh": ("post", {"200", "401", "422", "500"}),
    "/api/v1/auth/logout": ("post", {"204", "401", "404", "422", "500"}),
    "/api/v1/users/me": ("get", {"200", "401", "500"}),
}

#: 统一错误结构必须出现在组件里
REQUIRED_COMPONENTS = {"ErrorResponse", "ErrorBody", "ErrorCode"}


@pytest.fixture(scope="module")
def schema() -> dict:
    return create_app().openapi()


def test_all_planned_paths_are_documented(schema: dict) -> None:
    for path in EXPECTED_OPERATIONS:
        assert path in schema["paths"], f"{path} 未出现在 OpenAPI 中"


def test_health_paths_are_outside_api_prefix(schema: dict) -> None:
    assert "/health/live" in schema["paths"]
    assert "/health/ready" in schema["paths"]
    assert "/api/v1/health/live" not in schema["paths"]


def test_documented_status_codes_match_contract(schema: dict) -> None:
    for path, (method, expected_codes) in EXPECTED_OPERATIONS.items():
        actual = set(schema["paths"][path][method]["responses"])
        assert actual == expected_codes, f"{method.upper()} {path} 状态码不一致: {actual}"


def test_error_components_are_present(schema: dict) -> None:
    components = schema.get("components", {}).get("schemas", {})

    for name in REQUIRED_COMPONENTS:
        assert name in components, f"缺少错误结构组件 {name}"


def test_error_code_enum_is_exposed_for_frontend(schema: dict) -> None:
    """错误码必须作为枚举暴露，前端才能生成可穷尽检查的联合类型。"""
    enum_values = set(
        schema["components"]["schemas"]["ErrorCode"]["enum"]
    )

    assert enum_values == {code.value for code in ErrorCode}


def test_health_check_responses_reference_error_schema(schema: dict) -> None:
    ready_503 = schema["paths"]["/health/ready"]["get"]["responses"]["503"]
    schema_ref = ready_503["content"]["application/json"]["schema"]["$ref"]

    assert schema_ref.endswith("/ErrorResponse")


def test_auth_routes_require_bearer_security_scheme(schema: dict) -> None:
    security_schemes = schema["components"]["securitySchemes"]

    assert "HTTPBearer" in security_schemes
    assert security_schemes["HTTPBearer"]["scheme"] == "bearer"
    # /users/me 与 /auth/logout 需要 Bearer；/auth/refresh 明确不需要
    assert schema["paths"]["/api/v1/users/me"]["get"].get("security")
    assert schema["paths"]["/api/v1/auth/logout"]["post"].get("security")
    assert not schema["paths"]["/api/v1/auth/refresh"]["post"].get("security")


def test_exported_file_matches_current_code(schema: dict) -> None:
    """必须与 ``scripts/export_openapi.py`` 的产物一致。

    修复方式：在 ``backend`` 目录下执行
    ``..\\.venv\\Scripts\\python.exe scripts\\export_openapi.py`` 并提交产物。
    """
    assert OPENAPI_PATH.exists(), (
        f"缺少导出文件 {OPENAPI_PATH}；请执行 scripts/export_openapi.py"
    )

    exported = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))

    assert exported == schema, (
        "contracts/openapi/openapi.json 已过期："
        "接口改动后请重新执行 scripts/export_openapi.py 并提交产物"
    )


def test_openapi_generation_does_not_need_database() -> None:
    """契约测试可在没有数据库的机器上跑：生成 OpenAPI 不访问外部服务。"""

    app: FastAPI = create_app()

    assert app.openapi()["info"]["title"]
