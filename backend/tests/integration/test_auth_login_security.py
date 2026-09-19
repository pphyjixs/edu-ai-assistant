"""登录安全与注销行为验收测试（真实 PostgreSQL 测试库）。

对应 ``docs/api-contract.md`` 第 2.5 节与 ``docs/acceptance.md`` 第 2 节：
错误凭证不泄露账号是否存在、失败限流、注销只影响指定会话、
注销不维护 Access Token 黑名单。
"""

from __future__ import annotations

import uuid

from app.core.config import Settings
from app.modules.auth.security import create_access_token

PASSWORD = "Demo password 2026!"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/users/me"

#: 与 settings 默认值一致（LOGIN_RATE_LIMIT_MAX_FAILURES）
MAX_FAILURES = 5


def _register(client, email: str, password: str = PASSWORD) -> None:
    response = client.post(
        REGISTER_URL,
        json={
            "email": email,
            "password": password,
            "display_name": email.split("@")[0],
            "role": "STUDENT",
        },
    )
    assert response.status_code == 201, response.text


def _login(client, email: str, password: str):
    return client.post(LOGIN_URL, json={"email": email, "password": password})


def _auth(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


# --------------------------------------------------------------------------- #
# 错误凭证一致性
# --------------------------------------------------------------------------- #
def test_wrong_password_and_unknown_account_are_indistinguishable(api_client) -> None:
    """错误凭证不泄露账号是否存在：同状态码、同错误码、同文案。"""
    _register(api_client, "student@example.com")

    wrong_password = _login(api_client, "student@example.com", "wrong password")
    unknown_account = _login(api_client, "nobody@example.com", PASSWORD)

    assert wrong_password.status_code == unknown_account.status_code == 401
    # request_id 每次不同，因此只比较对客户端有意义的字段
    assert (
        wrong_password.json()["error"]["code"]
        == unknown_account.json()["error"]["code"]
        == "AUTH_INVALID_CREDENTIALS"
    )
    assert (
        wrong_password.json()["error"]["message"]
        == unknown_account.json()["error"]["message"]
    )


# --------------------------------------------------------------------------- #
# 登录失败限流
# --------------------------------------------------------------------------- #
def test_login_is_rate_limited_by_normalized_email(api_client) -> None:
    _register(api_client, "student@example.com")

    for _ in range(MAX_FAILURES):
        assert (
            _login(api_client, "student@example.com", "wrong password").status_code
            == 401
        )

    blocked = _login(api_client, "student@example.com", PASSWORD)

    assert blocked.status_code == 429
    error = blocked.json()["error"]
    assert error["code"] == "AUTH_TOO_MANY_ATTEMPTS"
    assert error["details"]["retry_after_seconds"] > 0


def test_rate_limit_does_not_leak_account_existence(api_client) -> None:
    """不存在的邮箱同样被限流，否则限流结果会暴露账号是否存在。"""
    for _ in range(MAX_FAILURES):
        assert _login(api_client, "ghost@example.com", PASSWORD).status_code == 401

    blocked = _login(api_client, "ghost@example.com", PASSWORD)

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "AUTH_TOO_MANY_ATTEMPTS"


def test_rate_limit_is_scoped_to_single_email(api_client) -> None:
    _register(api_client, "student@example.com")
    _register(api_client, "classmate@example.com")

    for _ in range(MAX_FAILURES):
        _login(api_client, "student@example.com", "wrong password")

    assert _login(api_client, "student@example.com", PASSWORD).status_code == 429
    assert _login(api_client, "classmate@example.com", PASSWORD).status_code == 200


def test_successful_login_clears_failure_counter(api_client) -> None:
    _register(api_client, "student@example.com")

    for _ in range(MAX_FAILURES - 1):
        _login(api_client, "student@example.com", "wrong password")

    assert _login(api_client, "student@example.com", PASSWORD).status_code == 200
    # 计数已清零，再失败一次不应立刻触发限流
    assert _login(api_client, "student@example.com", "wrong password").status_code == 401
    assert _login(api_client, "student@example.com", PASSWORD).status_code == 200


# --------------------------------------------------------------------------- #
# 注销：会话隔离
# --------------------------------------------------------------------------- #
def test_logout_only_revokes_own_session(api_client) -> None:
    _register(api_client, "alice@example.com", "alice password 1")
    _register(api_client, "bob@example.com", "bob password 1")

    alice = _login(api_client, "alice@example.com", "alice password 1").json()
    bob = _login(api_client, "bob@example.com", "bob password 1").json()

    # 用 alice 的 Access Token 尝试撤销 bob 的会话
    stolen = api_client.post(
        LOGOUT_URL,
        json={"refresh_token": bob["refresh_token"]},
        headers=_auth(alice["access_token"]),
    )

    assert stolen.status_code == 404
    assert stolen.json()["error"]["code"] == "RESOURCE_NOT_FOUND"
    # 两个会话都未受影响
    assert (
        api_client.post(
            REFRESH_URL, json={"refresh_token": bob["refresh_token"]}
        ).status_code
        == 200
    )
    assert (
        api_client.post(
            REFRESH_URL, json={"refresh_token": alice["refresh_token"]}
        ).status_code
        == 200
    )


def test_multiple_sessions_are_revoked_independently(api_client) -> None:
    """契约 2.5：注销只影响请求指定的那一个会话，同账号其他会话照常可用。"""
    _register(api_client, "alice@example.com", "alice password 1")
    first = _login(api_client, "alice@example.com", "alice password 1").json()
    second = _login(api_client, "alice@example.com", "alice password 1").json()
    third = _login(api_client, "alice@example.com", "alice password 1").json()

    assert (
        api_client.post(
            LOGOUT_URL,
            json={"refresh_token": second["refresh_token"]},
            headers=_auth(second["access_token"]),
        ).status_code
        == 204
    )

    assert (
        api_client.post(
            REFRESH_URL, json={"refresh_token": first["refresh_token"]}
        ).status_code
        == 200
    )
    assert (
        api_client.post(
            REFRESH_URL, json={"refresh_token": third["refresh_token"]}
        ).status_code
        == 200
    )
    assert (
        api_client.post(
            REFRESH_URL, json={"refresh_token": second["refresh_token"]}
        ).status_code
        == 401
    )


def test_logout_is_idempotent(api_client) -> None:
    _register(api_client, "alice@example.com", "alice password 1")
    tokens = _login(api_client, "alice@example.com", "alice password 1").json()
    headers = _auth(tokens["access_token"])
    body = {"refresh_token": tokens["refresh_token"]}

    assert api_client.post(LOGOUT_URL, json=body, headers=headers).status_code == 204
    assert api_client.post(LOGOUT_URL, json=body, headers=headers).status_code == 204


def test_logout_rejects_unknown_refresh_token(api_client) -> None:
    _register(api_client, "alice@example.com", "alice password 1")
    tokens = _login(api_client, "alice@example.com", "alice password 1").json()

    response = api_client.post(
        LOGOUT_URL,
        json={"refresh_token": "not-a-real-token"},
        headers=_auth(tokens["access_token"]),
    )

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 注销后旧 Access Token 的到期行为
# --------------------------------------------------------------------------- #
def test_old_access_token_stays_valid_until_its_own_expiry(api_client) -> None:
    """契约 2.5：不维护 Access Token 黑名单，旧 Access Token 可用至自身过期。

    只删除前端令牌或撤销 Refresh Token 都不会让已签发的 Access Token 立即失效；
    若要立即失效必须等它自身过期（或后续引入黑名单，届时需同步改契约）。
    """
    _register(api_client, "alice@example.com", "alice password 1")
    tokens = _login(api_client, "alice@example.com", "alice password 1").json()
    old_access_token = tokens["access_token"]

    assert (
        api_client.post(
            LOGOUT_URL,
            json={"refresh_token": tokens["refresh_token"]},
            headers=_auth(old_access_token),
        ).status_code
        == 204
    )

    # Refresh Token 已失效
    assert (
        api_client.post(
            REFRESH_URL, json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )
    # 但旧 Access Token 仍然有效
    still_valid = api_client.get(ME_URL, headers=_auth(old_access_token))
    assert still_valid.status_code == 200
    assert still_valid.json()["email"] == "alice@example.com"


# --------------------------------------------------------------------------- #
# 受保护端点的令牌校验
# --------------------------------------------------------------------------- #
def test_protected_endpoint_requires_valid_access_token(api_client) -> None:
    _register(api_client, "alice@example.com", "alice password 1")

    missing = api_client.get(ME_URL)
    malformed = api_client.get(ME_URL, headers={"Authorization": "Bearer not-a-jwt"})
    wrong_scheme = api_client.get(ME_URL, headers={"Authorization": "Basic abc"})

    for response in (missing, malformed, wrong_scheme):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTH_TOKEN_EXPIRED"


def test_access_token_from_other_secret_is_rejected(api_client) -> None:
    """签名密钥不同（等价于伪造令牌）时必须拒绝。"""
    _register(api_client, "alice@example.com", "alice password 1")
    tokens = _login(api_client, "alice@example.com", "alice password 1").json()

    forged, _ = create_access_token(
        user_id=uuid.UUID(tokens["user"]["id"]),
        role="STUDENT",
        settings=Settings(
            _env_file=None,
            app_secret_key="attacker-secret-0123456789abcdefghijkl",
        ),
    )

    response = api_client.get(ME_URL, headers=_auth(forged))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_TOKEN_EXPIRED"
