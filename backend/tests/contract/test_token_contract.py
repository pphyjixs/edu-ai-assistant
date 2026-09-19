"""令牌传输契约测试（``docs/api-contract.md`` 第 2 节）。

只校验 **请求与响应格式**：字段名、类型、状态码、传输位置。

刻意不断言前端令牌存放位置与内部键名——契约 2.2 把它们交给前端自行决定。
因此这里反过来断言"服务端不通过 Cookie 下发令牌"，即不把存储选择强加给前端。
"""

from __future__ import annotations

import re

PASSWORD = "Demo password 2026!"
EMAIL = "contract@example.com"

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/users/me"

#: 契约 2.2：Access Token 有效期 3600 秒
ACCESS_TOKEN_EXPIRES_IN = 3600

#: 契约 2.1：登录用户名只支持邮箱
TOKEN_TYPE = "bearer"

#: JWT 三段式（Header.Payload.Signature）
JWT_PATTERN = re.compile(r"^[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+$")

#: base64url 字符集（Refresh Token 为不透明随机值）
OPAQUE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9\-_]+$")


def _register(client) -> dict:
    response = client.post(
        REGISTER_URL,
        json={
            "email": EMAIL,
            "password": PASSWORD,
            "display_name": "契约测试",
            "role": "TEACHER",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _login(client) -> dict:
    response = client.post(LOGIN_URL, json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# 注册：请求体字段与响应
# --------------------------------------------------------------------------- #
def test_register_request_uses_json_body_with_four_fields(api_client) -> None:
    """契约 2.1：注册接受邮箱、密码、姓名、角色四个字段。"""
    response = api_client.post(
        REGISTER_URL,
        json={
            "email": EMAIL,
            "password": PASSWORD,
            "display_name": "契约测试",
            "role": "TEACHER",
        },
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 201
    assert response.headers["content-type"].startswith("application/json")


def test_register_response_shape(api_client) -> None:
    body = _register(api_client)

    assert set(body) == {"id", "email", "display_name", "role", "created_at"}
    assert isinstance(body["id"], str)
    assert isinstance(body["email"], str)
    assert isinstance(body["display_name"], str)
    assert body["role"] in {"TEACHER", "STUDENT"}
    assert body["created_at"].endswith("Z"), "契约 1：时间为 ISO 8601 UTC"
    # 注册不返回令牌
    assert "access_token" not in body and "refresh_token" not in body


def test_register_does_not_set_cookie(api_client) -> None:
    """服务端不替前端决定令牌存放位置。"""
    response = api_client.post(
        REGISTER_URL,
        json={
            "email": EMAIL,
            "password": PASSWORD,
            "display_name": "契约测试",
            "role": "TEACHER",
        },
    )

    assert "set-cookie" not in {key.lower() for key in response.headers}


# --------------------------------------------------------------------------- #
# 登录：两个令牌都在 JSON 响应体里
# --------------------------------------------------------------------------- #
def test_login_request_is_json_email_and_password(api_client) -> None:
    """契约 2.1：登录使用 JSON，不使用表单。"""
    _register(api_client)

    response = api_client.post(LOGIN_URL, json={"email": EMAIL, "password": PASSWORD})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


def test_login_response_carries_both_tokens_in_body(api_client) -> None:
    _register(api_client)
    body = _login(api_client)

    assert set(body) == {
        "access_token",
        "refresh_token",
        "token_type",
        "expires_in",
        "user",
    }
    assert body["token_type"] == TOKEN_TYPE
    assert body["expires_in"] == ACCESS_TOKEN_EXPIRES_IN
    assert isinstance(body["expires_in"], int)
    assert JWT_PATTERN.match(body["access_token"]), "Access Token 是带签名的三段式令牌"
    assert OPAQUE_TOKEN_PATTERN.match(body["refresh_token"]), "Refresh Token 是不透明随机值"
    assert "." not in body["refresh_token"], "Refresh Token 不带结构，避免被当成 JWT 解析"
    assert set(body["user"]) == {"id", "display_name", "role"}
    assert body["user"]["role"] in {"TEACHER", "STUDENT"}


def test_login_does_not_set_cookie(api_client) -> None:
    _register(api_client)
    response = api_client.post(LOGIN_URL, json={"email": EMAIL, "password": PASSWORD})

    assert "set-cookie" not in {key.lower() for key in response.headers}


# --------------------------------------------------------------------------- #
# 受保护接口：Bearer 头
# --------------------------------------------------------------------------- #
def test_access_token_is_sent_via_authorization_bearer_header(api_client) -> None:
    _register(api_client)
    tokens = _login(api_client)

    with_bearer = api_client.get(
        ME_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    wrong_scheme = api_client.get(
        ME_URL, headers={"Authorization": f"Token {tokens['access_token']}"}
    )
    # 令牌不能通过 query string 传递
    via_query = api_client.get(f"{ME_URL}?access_token={tokens['access_token']}")

    assert with_bearer.status_code == 200
    assert wrong_scheme.status_code == 401
    assert via_query.status_code == 401


def test_users_me_response_shape(api_client) -> None:
    _register(api_client)
    tokens = _login(api_client)

    body = api_client.get(
        ME_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"}
    ).json()

    assert set(body) == {"id", "email", "display_name", "role", "created_at"}
    assert body["email"] == EMAIL
    assert body["created_at"].endswith("Z")


# --------------------------------------------------------------------------- #
# 刷新：JSON Body 提交，不要求 Access Token
# --------------------------------------------------------------------------- #
def test_refresh_takes_refresh_token_in_json_body_without_access_token(
    api_client,
) -> None:
    """契约 2.4：请求体提交 Refresh Token，不要求有效的 Access Token。"""
    _register(api_client)
    tokens = _login(api_client)

    response = api_client.post(
        REFRESH_URL, json={"refresh_token": tokens["refresh_token"]}
    )

    # 未携带 Authorization 头也能刷新成功，即"不要求有效的 Access Token"
    assert response.status_code == 200


def test_refresh_response_only_returns_new_access_token(api_client) -> None:
    """契约 2.4：响应不包含新的 refresh_token，也不含用户信息。"""
    _register(api_client)
    tokens = _login(api_client)

    body = api_client.post(
        REFRESH_URL, json={"refresh_token": tokens["refresh_token"]}
    ).json()

    assert set(body) == {"access_token", "token_type", "expires_in"}
    assert body["token_type"] == TOKEN_TYPE
    assert body["expires_in"] == ACCESS_TOKEN_EXPIRES_IN
    assert JWT_PATTERN.match(body["access_token"])
    assert "refresh_token" not in body


def test_refresh_rejects_access_token_placeholder(api_client) -> None:
    """把 Access Token 当 Refresh Token 用必须失败。"""
    _register(api_client)
    tokens = _login(api_client)

    response = api_client.post(
        REFRESH_URL, json={"refresh_token": tokens["access_token"]}
    )

    assert response.status_code == 401


def test_refresh_requires_refresh_token_field(api_client) -> None:
    missing = api_client.post(REFRESH_URL, json={})
    empty = api_client.post(REFRESH_URL, json={"refresh_token": ""})

    for response in (missing, empty):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------- #
# 注销：Bearer 头 + JSON Body
# --------------------------------------------------------------------------- #
def test_logout_requires_bearer_header_and_json_body(api_client) -> None:
    """契约 2.5：需要 Access Token，并通过 JSON Body 指定当前会话。"""
    _register(api_client)
    tokens = _login(api_client)

    without_token = api_client.post(
        LOGOUT_URL, json={"refresh_token": tokens["refresh_token"]}
    )
    response = api_client.post(
        LOGOUT_URL,
        json={"refresh_token": tokens["refresh_token"]},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )

    assert without_token.status_code == 401
    assert response.status_code == 204
    assert response.content == b"", "契约：204 无响应体"


def test_logout_response_has_no_body_and_no_cookie(api_client) -> None:
    _register(api_client)
    tokens = _login(api_client)

    response = api_client.post(
        LOGOUT_URL,
        json={"refresh_token": tokens["refresh_token"]},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )

    assert response.status_code == 204
    assert "set-cookie" not in {key.lower() for key in response.headers}


# --------------------------------------------------------------------------- #
# 认证失败的统一结构
# --------------------------------------------------------------------------- #
def test_auth_failures_use_the_contract_error_envelope(api_client) -> None:
    _register(api_client)

    samples = [
        api_client.post(LOGIN_URL, json={"email": EMAIL, "password": "wrong"}),
        api_client.post(REFRESH_URL, json={"refresh_token": "unknown"}),
        api_client.get(ME_URL),
        api_client.post(LOGIN_URL, json={"email": "bad"}),
    ]

    for response in samples:
        assert response.status_code in {401, 422}
        error = response.json()["error"]
        assert set(error) == {"code", "message", "details", "request_id"}
        assert isinstance(error["details"], dict)
