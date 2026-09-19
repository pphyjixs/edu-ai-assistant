"""Auth 验收矩阵（真实 PostgreSQL 测试库）。

覆盖 ``docs/acceptance.md`` 第 2 节中依赖数据库语义的条目：

- 邮箱比较规则（只规范化域名，保留本地部分大小写与句点/``+`` 后缀）；
- 密码长度边界（8–128）与空格、大小写敏感性；
- 过期 Access Token；
- 过期与已撤销的 Refresh Token；
- 401 / 403 / 404 / 422 / 429 的统一错误结构。
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.core.time import utc_now
from app.modules.auth.permissions import TeacherDep
from app.modules.auth.security import create_access_token

PASSWORD = "Demo password 2026!"
REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
REFRESH_URL = "/api/v1/auth/refresh"
LOGOUT_URL = "/api/v1/auth/logout"
ME_URL = "/api/v1/users/me"
TEACHER_ONLY_URL = "/__test__/teacher-only"

ENVELOPE_KEYS = {"code", "message", "details", "request_id"}


def _register(client, email: str, password: str = PASSWORD, role: str = "STUDENT"):
    return client.post(
        REGISTER_URL,
        json={
            "email": email,
            "password": password,
            "display_name": email.split("@")[0],
            "role": role,
        },
    )


def _login(client, email: str, password: str = PASSWORD):
    return client.post(LOGIN_URL, json={"email": email, "password": password})


def _auth(access_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def _rows(engine: Engine, sql: str) -> list[Any]:
    with engine.connect() as connection:
        return list(connection.execute(text(sql)).all())


def _attach_teacher_only_route(app) -> None:
    """挂一条只允许教师的探针路由，用于验证共享的 ``TeacherDep``。

    业务模块尚未接入 ``require_roles``，因此这里验证的是 Auth 交付的依赖本身。
    """

    @app.get(TEACHER_ONLY_URL)
    async def _teacher_only(user: TeacherDep) -> dict[str, str]:
        return {"role": user.role.value}


# --------------------------------------------------------------------------- #
# 邮箱：域名大小写 vs 本地部分大小写
# --------------------------------------------------------------------------- #
def test_domain_case_is_ignored_but_local_part_case_is_not(
    api_client, pg_sync_engine: Engine
) -> None:
    """契约 2.1：域名大小写不同视为同一账号，本地部分大小写不同视为不同账号。"""
    assert _register(api_client, "Teacher@EXAMPLE.com").status_code == 201

    # 同一账号：域名大小写任意
    assert _login(api_client, "Teacher@example.com").status_code == 200
    assert _login(api_client, "Teacher@EXAMPLE.com").status_code == 200

    # 重复注册被唯一约束拦下
    duplicate = _register(api_client, "Teacher@example.com")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "AUTH_EMAIL_TAKEN"

    # 不同账号：本地部分大小写不同
    assert _register(api_client, "teacher@example.com").status_code == 201
    assert _login(api_client, "teacher@example.com").status_code == 200
    assert _login(api_client, "teacher@EXAMPLE.com").status_code == 200, (
        "域名大小写不影响解析，仍是本地小写的那个账号"
    )

    rows = dict(_rows(pg_sync_engine, "SELECT email, email_normalized FROM users"))
    # 原始邮箱原样保存用于展示
    assert "Teacher@EXAMPLE.com" in rows
    # 比较值只小写域名
    assert rows["Teacher@EXAMPLE.com"] == "Teacher@example.com"
    assert rows["teacher@example.com"] == "teacher@example.com"


def test_dots_and_plus_suffix_are_preserved(api_client, pg_sync_engine: Engine) -> None:
    """契约 2.1：不移除本地部分中的句点或 ``+`` 后缀。"""
    assert _register(api_client, "First.Last+tag@Example.COM").status_code == 201

    normalized = _rows(pg_sync_engine, "SELECT email_normalized FROM users")[0][0]
    assert normalized == "First.Last+tag@example.com"

    # 去掉 ``+`` 后缀的是另一个账号，不会被当成同一人
    assert _register(api_client, "First.Last@example.com").status_code == 201


# --------------------------------------------------------------------------- #
# 密码边界
# --------------------------------------------------------------------------- #
def test_password_length_boundaries(api_client) -> None:
    """契约 2.1：8–128 个字符，少于 8 或多于 128 被拒绝，不截断。"""
    assert _register(api_client, "lower@example.com", "a" * 8).status_code == 201
    assert _register(api_client, "upper@example.com", "b" * 128).status_code == 201

    too_short = _register(api_client, "short@example.com", "c" * 7)
    too_long = _register(api_client, "long@example.com", "d" * 129)

    assert too_short.status_code == 422
    assert too_short.json()["error"]["code"] == "VALIDATION_ERROR"
    assert too_long.status_code == 422
    # 超长密码被拒绝而不是截断成 128 位
    assert _login(api_client, "long@example.com", "d" * 128).status_code == 401


def test_password_is_case_sensitive_and_space_significant(api_client) -> None:
    password = "  Mixed Case Pass  "
    assert _register(api_client, "space@example.com", password).status_code == 201

    assert _login(api_client, "space@example.com", password).status_code == 200
    # 首尾空格不自动去除
    assert _login(api_client, "space@example.com", password.strip()).status_code == 401
    # 区分大小写
    assert _login(api_client, "space@example.com", password.lower()).status_code == 401


def test_password_without_character_class_mix_is_accepted(api_client) -> None:
    """不强制大小写字母、数字或特殊字符的组合。"""
    assert _register(api_client, "simple@example.com", "aaaaaaaa").status_code == 201
    assert _login(api_client, "simple@example.com", "aaaaaaaa").status_code == 200


# --------------------------------------------------------------------------- #
# 过期 Access Token
# --------------------------------------------------------------------------- #
def test_expired_access_token_is_rejected(db_isolation, pg_app, make_settings) -> None:
    """用与应用相同的密钥签发一个已过期令牌，必须被拒绝。"""
    secret = "acceptance-secret-key-0123456789abcdefghij"
    app = pg_app(app_secret_key=secret)

    with TestClient(app) as client:
        assert _register(client, "expired@example.com").status_code == 201
        user_id = uuid.UUID(_login(client, "expired@example.com").json()["user"]["id"])

        expired_token, _ = create_access_token(
            user_id=user_id,
            role="STUDENT",
            settings=make_settings(app_secret_key=secret),
            now=utc_now() - timedelta(hours=2),
        )
        response = client.get(ME_URL, headers=_auth(expired_token))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_TOKEN_EXPIRED"


def test_access_token_expires_after_configured_ttl(db_isolation, pg_app) -> None:
    """把 TTL 配成 2 秒，验证真实登录签发的令牌在到期后确实失效。

    TTL 取 2 而不是 1：``exp`` 只有秒级精度（``exp = int(iat) + ttl``），
    TTL=1 时令牌的实际有效期可能不足 1 秒，首个请求就可能已经过期，
    会让用例随机失败。TTL=2 时实际有效期在 (1s, 2s] 之间，足够先验证"未过期可用"。
    """
    ttl_seconds = 2
    app = pg_app(access_token_expire_seconds=ttl_seconds)

    with TestClient(app) as client:
        assert _register(client, "ttl@example.com").status_code == 201
        tokens = _login(client, "ttl@example.com").json()
        assert tokens["expires_in"] == ttl_seconds, "签发的 expires_in 应等于配置的 TTL"

        alive = client.get(ME_URL, headers=_auth(tokens["access_token"]))
        time.sleep(ttl_seconds + 0.2)
        expired = client.get(ME_URL, headers=_auth(tokens["access_token"]))

    assert alive.status_code == 200
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "AUTH_TOKEN_EXPIRED"


# --------------------------------------------------------------------------- #
# 过期 / 已撤销 / 不存在的 Refresh Token
# --------------------------------------------------------------------------- #
def test_expired_refresh_token_cannot_refresh(
    api_client, pg_sync_engine: Engine
) -> None:
    """把会话到期时间改成过去，刷新必须失败。"""
    assert _register(api_client, "stale@example.com").status_code == 201
    refresh_token = _login(api_client, "stale@example.com").json()["refresh_token"]

    with pg_sync_engine.begin() as connection:
        connection.execute(
            text("UPDATE auth_sessions SET expires_at = now() - interval '1 day'")
        )

    response = api_client.post(REFRESH_URL, json={"refresh_token": refresh_token})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_TOKEN_EXPIRED"


def test_unexpired_session_can_still_refresh(api_client) -> None:
    """对照组：未过期的会话必须能刷新，证明上面的失败来自到期时间本身。"""
    assert _register(api_client, "fresh@example.com").status_code == 201
    refresh_token = _login(api_client, "fresh@example.com").json()["refresh_token"]

    assert (
        api_client.post(
            REFRESH_URL, json={"refresh_token": refresh_token}
        ).status_code
        == 200
    )


def test_revoked_refresh_token_cannot_refresh(api_client) -> None:
    assert _register(api_client, "revoked@example.com").status_code == 201
    tokens = _login(api_client, "revoked@example.com").json()

    assert (
        api_client.post(
            LOGOUT_URL,
            json={"refresh_token": tokens["refresh_token"]},
            headers=_auth(tokens["access_token"]),
        ).status_code
        == 204
    )

    response = api_client.post(
        REFRESH_URL, json={"refresh_token": tokens["refresh_token"]}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_TOKEN_EXPIRED"


def test_unknown_refresh_token_cannot_refresh(api_client) -> None:
    response = api_client.post(REFRESH_URL, json={"refresh_token": "unknown-token"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_TOKEN_EXPIRED"


# --------------------------------------------------------------------------- #
# 401 / 403 / 404 / 422 / 429 统一错误结构
# --------------------------------------------------------------------------- #
def test_student_gets_403_from_teacher_only_dependency(db_isolation, pg_app) -> None:
    """平台角色守卫：学生调用教师接口返回 403 ROLE_FORBIDDEN。"""
    app = pg_app()
    _attach_teacher_only_route(app)

    with TestClient(app) as client:
        assert _register(client, "student@example.com").status_code == 201
        assert (
            _register(client, "teacher@example.com", role="TEACHER").status_code == 201
        )
        student = _login(client, "student@example.com").json()
        teacher = _login(client, "teacher@example.com").json()

        forbidden = client.get(
            TEACHER_ONLY_URL, headers=_auth(student["access_token"])
        )
        allowed = client.get(TEACHER_ONLY_URL, headers=_auth(teacher["access_token"]))
        anonymous = client.get(TEACHER_ONLY_URL)

    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "ROLE_FORBIDDEN"
    assert allowed.status_code == 200
    assert allowed.json() == {"role": "TEACHER"}
    # 未登录是 401 而不是 403：先认证，再鉴权
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "AUTH_TOKEN_EXPIRED"


def test_all_error_responses_share_one_envelope(db_isolation, pg_app) -> None:
    """401 / 403 / 404 / 422 / 429 必须是同一种结构，前端只需写一份错误处理。"""
    app = pg_app()
    _attach_teacher_only_route(app)

    with TestClient(app) as client:
        assert _register(client, "student@example.com").status_code == 201
        student = _login(client, "student@example.com").json()

        samples = {
            401: client.get(ME_URL),
            403: client.get(TEACHER_ONLY_URL, headers=_auth(student["access_token"])),
            404: client.get("/api/v1/not-exist"),
            422: client.post(
                LOGIN_URL, json={"email": "not-an-email", "password": PASSWORD}
            ),
        }

        # 429：把失败次数打满
        for _ in range(5):
            client.post(LOGIN_URL, json={"email": "student@example.com", "password": "nope"})
        samples[429] = client.post(
            LOGIN_URL, json={"email": "student@example.com", "password": PASSWORD}
        )

    for expected_status, response in samples.items():
        assert response.status_code == expected_status, response.text
        error = response.json()["error"]
        assert set(error) == ENVELOPE_KEYS, f"{expected_status} 的响应结构不一致"
        assert isinstance(error["code"], str) and error["code"]
        assert isinstance(error["message"], str) and error["message"]
        assert isinstance(error["details"], dict)
        assert error["request_id"]

    validation = samples[422].json()["error"]
    assert validation["code"] == "VALIDATION_ERROR"
    for item in validation["details"]["errors"]:
        assert set(item) == {"loc", "type", "message"}
    # 校验错误不回显用户提交的原始值
    assert "not-an-email" not in samples[422].text


def test_error_request_id_matches_response_header(api_client) -> None:
    response = api_client.get(ME_URL, headers={"X-Request-ID": "acceptance-trace-1"})

    assert response.status_code == 401
    assert response.json()["error"]["request_id"] == "acceptance-trace-1"
    assert response.headers["x-request-id"] == "acceptance-trace-1"


def test_login_error_response_contains_no_secrets(api_client) -> None:
    """登录失败响应不得出现密码原文，也不得出现任何令牌字段。"""
    assert _register(api_client, "student@example.com").status_code == 201

    response = _login(api_client, "student@example.com", "wrong password value")

    assert response.status_code == 401
    assert "access_token" not in response.text
    assert "refresh_token" not in response.text
    assert "wrong password value" not in response.text
