"""统一错误结构测试。

对应 ``docs/acceptance.md`` 第 10 节：Pydantic 校验错误必须转换为统一错误结构，
响应与日志不得包含密码、令牌或异常堆栈。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from app.core.request_context import REQUEST_ID_HEADER
from app.main import create_app

#: 只在测试中注册的探针路由，用于触发校验错误与未预期异常
PROBE_REGISTER_PATH = "/api/v1/__test__/register"
PROBE_BOOM_PATH = "/api/v1/__test__/boom"

SECRET_IN_PAYLOAD = "k9#z"  # 4 字符，故意不满足 8 字符下限
SECRET_IN_EXCEPTION = "super-secret-token-value"


class RegisterPayload(BaseModel):
    """模拟注册请求体，用于验证校验错误的响应形态。"""

    email: str
    password: str = Field(min_length=8, max_length=128)


@pytest.fixture
def client(make_settings) -> Iterator[TestClient]:
    app = create_app(make_settings())
    _register_probe_routes(app)
    with TestClient(app) as test_client:
        yield test_client


def test_validation_error_uses_unified_structure(client: TestClient) -> None:
    response = client.post(
        PROBE_REGISTER_PATH,
        json={"email": "teacher@example.com", "password": SECRET_IN_PAYLOAD},
    )

    assert response.status_code == 422
    error = response.json()["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    assert error["code"] == "VALIDATION_ERROR"

    errors = error["details"]["errors"]
    assert errors and errors[0]["loc"] == ["body", "password"]
    assert errors[0]["type"] == "string_too_short"


def test_validation_error_does_not_echo_user_input(client: TestClient) -> None:
    """Pydantic 默认回显 input 字段，会把密码原文写进响应，必须剔除。"""
    response = client.post(
        PROBE_REGISTER_PATH,
        json={"email": "teacher@example.com", "password": SECRET_IN_PAYLOAD},
    )

    assert SECRET_IN_PAYLOAD not in response.text


def test_unknown_route_returns_contract_error_code(client: TestClient) -> None:
    response = client.get("/api/v1/not-exist")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "RESOURCE_NOT_FOUND"
    assert error["message"] == "资源不存在或不可见"
    assert error["details"] == {}


def test_method_not_allowed_is_mapped(client: TestClient) -> None:
    response = client.delete("/health/live")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert "allow" in {key.lower() for key in response.headers}


def test_unexpected_error_is_masked_and_traceable(client: TestClient) -> None:
    """未预期异常只返回通用文案，堆栈与异常原文不得进入响应。"""
    response = client.get(PROBE_BOOM_PATH)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "INTERNAL_ERROR"
    assert error["message"] == "服务内部错误，请稍后重试"

    for leaked in (SECRET_IN_EXCEPTION, "RuntimeError", "Traceback"):
        assert leaked not in response.text


def test_error_request_id_matches_response_header(client: TestClient) -> None:
    response = client.get(
        PROBE_BOOM_PATH, headers={REQUEST_ID_HEADER: "trace-boom-1"}
    )

    assert response.json()["error"]["request_id"] == "trace-boom-1"
    assert response.headers[REQUEST_ID_HEADER] == "trace-boom-1"


def _register_probe_routes(app: FastAPI) -> None:
    """注册测试用路由；不进入生产路由表。"""

    @app.post(PROBE_REGISTER_PATH)
    async def _register(payload: RegisterPayload) -> dict[str, str]:
        return {"email": payload.email}

    @app.get(PROBE_BOOM_PATH)
    async def _boom() -> dict[str, str]:
        raise RuntimeError(f"unexpected failure token={SECRET_IN_EXCEPTION}")
