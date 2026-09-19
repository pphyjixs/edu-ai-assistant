"""Auth 主流程验收测试（真实 PostgreSQL 测试库）。

对应 ``docs/acceptance.md`` 第 2 节与 ``docs/api-contract.md`` 第 2 节：
注册 → 登录 → users/me → 刷新 → 注销 → 刷新失效。
"""

from __future__ import annotations

import hashlib

from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

TEACHER_PASSWORD = "Demo password 2026!"
TEACHER_PAYLOAD = {
    "email": "Teacher@EXAMPLE.com",
    "password": TEACHER_PASSWORD,
    "display_name": "张老师",
    "role": "TEACHER",
}

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/users/me"


def _register(client, **overrides) -> dict:
    payload = {**TEACHER_PAYLOAD, **overrides}
    response = client.post(REGISTER_URL, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _login(client, email: str = "Teacher@example.com", password: str = TEACHER_PASSWORD):
    return client.post(LOGIN_URL, json={"email": email, "password": password})


def _auth(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def test_register_returns_profile_without_secrets(api_client) -> None:
    body = _register(api_client)

    assert body["email"] == TEACHER_PAYLOAD["email"], "原始邮箱原样保存用于展示"
    assert body["role"] == "TEACHER"
    assert body["display_name"] == "张老师"
    assert body["created_at"].endswith("Z"), "契约：时间为 ISO 8601 UTC"
    assert set(body) == {"id", "email", "display_name", "role", "created_at"}
    assert TEACHER_PASSWORD not in str(body)


def test_login_returns_both_tokens_and_user_summary(api_client) -> None:
    _register(api_client)

    response = _login(api_client)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "access_token",
        "refresh_token",
        "token_type",
        "expires_in",
        "user",
    }
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    assert set(body["user"]) == {"id", "display_name", "role"}
    assert TEACHER_PASSWORD not in response.text


def test_full_token_lifecycle(api_client) -> None:
    _register(api_client)
    tokens = _login(api_client).json()
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]

    me = api_client.get(ME_URL, headers=_auth(access_token))
    assert me.status_code == 200
    assert me.json()["email"] == TEACHER_PAYLOAD["email"]

    refreshed = api_client.post(REFRESH_URL, json={"refresh_token": refresh_token})
    assert refreshed.status_code == 200
    refreshed_body = refreshed.json()
    assert set(refreshed_body) == {"access_token", "token_type", "expires_in"}
    new_access_token = refreshed_body["access_token"]
    assert new_access_token != access_token

    # 不轮换：同一个 Refresh Token 可以重复刷新
    assert (
        api_client.post(
            REFRESH_URL, json={"refresh_token": refresh_token}
        ).status_code
        == 200
    )

    logout = api_client.post(
        LOGOUT_URL,
        json={"refresh_token": refresh_token},
        headers=_auth(new_access_token),
    )
    assert logout.status_code == 204
    assert logout.content == b""

    revoked = api_client.post(REFRESH_URL, json={"refresh_token": refresh_token})
    assert revoked.status_code == 401
    assert revoked.json()["error"]["code"] == "AUTH_TOKEN_EXPIRED"


def test_duplicate_registration_is_rejected(api_client) -> None:
    _register(api_client)

    # 域名大小写不同视为同一账号
    duplicate = api_client.post(
        REGISTER_URL, json={**TEACHER_PAYLOAD, "email": "Teacher@example.com"}
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "AUTH_EMAIL_TAKEN"


def test_unique_constraint_is_the_backstop_for_concurrent_registration(
    api_client, pg_sync_engine: Engine
) -> None:
    """并发注册靠数据库唯一约束兜住，而不是应用层的先查后插。

    这里绕过应用直接写库：两个请求同时通过"邮箱未被占用"的检查时，
    只有数据库能拦住第二个 INSERT。
    """
    _register(api_client)

    unique_names = {
        constraint["name"]
        for constraint in inspect(pg_sync_engine).get_unique_constraints("users")
    }
    assert "uq_users_email_normalized" in unique_names, "约束必须真实存在于数据库上"

    with pg_sync_engine.begin() as connection:
        try:
            connection.execute(
                text(
                    "INSERT INTO users (id, email, email_normalized, password_hash,"
                    " display_name, role, is_active, created_at)"
                    " VALUES (gen_random_uuid(), :email, :norm, :hash, :name,"
                    " 'TEACHER', true, now())"
                ),
                {
                    "email": "Teacher@example.com",
                    "norm": "Teacher@example.com",
                    "hash": "$argon2id$placeholder",
                    "name": "重复",
                },
            )
        except IntegrityError as exc:
            assert "uq_users_email_normalized" in str(exc.orig)
        else:  # pragma: no cover - 约束缺失时才会进入
            raise AssertionError("重复的 email_normalized 应被唯一约束拒绝")


def test_database_stores_only_hashes(api_client, pg_sync_engine: Engine) -> None:
    """契约 2.2/2.3：密码只存 Argon2id 哈希，Refresh Token 只存哈希。"""
    _register(api_client)
    refresh_token = _login(api_client).json()["refresh_token"]

    with pg_sync_engine.connect() as connection:
        user_row = connection.execute(
            text("SELECT password_hash FROM users")
        ).one()
        session_row = connection.execute(
            text("SELECT refresh_token_hash, revoked_at, created_at, expires_at FROM auth_sessions")
        ).one()

    assert user_row.password_hash.startswith("$argon2id$")
    assert TEACHER_PASSWORD not in user_row.password_hash
    assert (
        session_row.refresh_token_hash
        == hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()
    )
    assert refresh_token not in str(session_row)
    assert session_row.revoked_at is None
    # 契约 2.2：Refresh Token 自登录签发起有效 7 天
    assert (session_row.expires_at - session_row.created_at).days == 7


def test_registration_validation_rules(api_client) -> None:
    """密码 8–128 字符、允许空格，不强制字符种类组合。"""
    too_short = api_client.post(
        REGISTER_URL, json={**TEACHER_PAYLOAD, "password": "short12"}
    )
    too_long = api_client.post(
        REGISTER_URL, json={**TEACHER_PAYLOAD, "password": "x" * 129}
    )
    allowed = api_client.post(
        REGISTER_URL, json={**TEACHER_PAYLOAD, "password": "  spaces ok  "}
    )

    assert too_short.status_code == 422
    assert too_short.json()["error"]["code"] == "VALIDATION_ERROR"
    assert too_long.status_code == 422
    assert allowed.status_code == 201, "允许空格、不强制大小写数字特殊字符组合"

    # 密码不被截断且区分首尾空格
    assert _login(api_client, password=" spaces ok ").status_code == 401
    assert _login(api_client, password="  spaces ok  ").status_code == 200
