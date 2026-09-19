"""Auth 安全层单元测试。

覆盖 ``docs/api-contract.md`` 第 2.1 / 2.2 / 2.4 节的规则：
邮箱比较值只小写域名、密码只存 Argon2id 哈希、Refresh Token 只存哈希、
Access Token 带到期时间且不可伪造。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import jwt
import pytest

from app.core.errors import TokenExpiredError
from app.core.time import utc_now
from app.modules.auth import security

SAMPLE_USER_ID = uuid.UUID("2f1c9f4e-1c1a-4f0e-9f0e-6a2b3c4d5e6f")
SAMPLE_PASSWORD = "Demo password 2026!"


# ------------------------------- 邮箱规范化 -------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 只把域名小写
        ("Teacher@EXAMPLE.com", "Teacher@example.com"),
        ("teacher@example.com", "teacher@example.com"),
        # 本地部分大小写、句点与 + 后缀都原样保留
        ("First.Last+tag@Example.COM", "First.Last+tag@example.com"),
    ],
)
def test_normalize_email_only_lowercases_domain(raw: str, expected: str) -> None:
    assert security.normalize_email(raw) == expected


def test_normalize_email_treats_same_domain_case_as_same_account() -> None:
    """契约：域名大小写不同视为同一账号，本地部分大小写不同视为不同账号。"""
    assert security.normalize_email("Teacher@EXAMPLE.com") == security.normalize_email(
        "Teacher@example.com"
    )
    assert security.normalize_email("Teacher@example.com") != security.normalize_email(
        "teacher@example.com"
    )


@pytest.mark.parametrize("raw", ["no-at-sign", "@example.com", "user@"])
def test_normalize_email_rejects_malformed_input(raw: str) -> None:
    with pytest.raises(ValueError):
        security.normalize_email(raw)


# -------------------------------- 密码哈希 --------------------------------


def test_password_is_hashed_with_argon2id() -> None:
    digest = security.hash_password(SAMPLE_PASSWORD)

    assert digest.startswith("$argon2id$")
    assert SAMPLE_PASSWORD not in digest


def test_password_hash_is_salted_and_verifiable() -> None:
    first = security.hash_password(SAMPLE_PASSWORD)
    second = security.hash_password(SAMPLE_PASSWORD)

    assert first != second, "同一密码两次哈希应不同（加盐）"
    assert security.verify_password(first, SAMPLE_PASSWORD)
    assert not security.verify_password(first, SAMPLE_PASSWORD + "x")


def test_password_whitespace_is_significant() -> None:
    """契约：不自动去除首尾空格，空格参与校验。"""
    digest = security.hash_password("  spaced pass  ")

    assert security.verify_password(digest, "  spaced pass  ")
    assert not security.verify_password(digest, "spaced pass")


def test_verify_password_returns_false_for_broken_hash() -> None:
    assert not security.verify_password("not-a-hash", SAMPLE_PASSWORD)


def test_constant_time_verify_handles_unknown_account() -> None:
    """账号不存在时返回 False，但仍执行一次真实校验（不抛异常）。"""
    assert not security.verify_password_constant_time(None, SAMPLE_PASSWORD)

    digest = security.hash_password(SAMPLE_PASSWORD)
    assert security.verify_password_constant_time(digest, SAMPLE_PASSWORD)


# ----------------------------- Refresh Token ------------------------------


def test_refresh_token_is_random_and_url_safe() -> None:
    first = security.generate_refresh_token()
    second = security.generate_refresh_token()

    assert first != second
    assert len(first) >= 60, "48 字节随机值编码后应远长于 32 字符"
    assert all(char.isalnum() or char in "-_" for char in first)


def test_refresh_token_hash_is_stable_sha256_hex() -> None:
    token = security.generate_refresh_token()
    digest = security.hash_refresh_token(token)

    assert digest == security.hash_refresh_token(token)
    assert len(digest) == 64
    assert all(char in "0123456789abcdef" for char in digest)
    assert token not in digest


# ------------------------------- Access Token -----------------------------


def test_access_token_round_trip(make_settings) -> None:
    settings = make_settings()
    token, expires_in = security.create_access_token(
        user_id=SAMPLE_USER_ID, role="TEACHER", settings=settings
    )

    claims = security.decode_access_token(token, settings)

    assert claims.user_id == SAMPLE_USER_ID
    assert claims.role == "TEACHER"
    assert expires_in == settings.access_token_expire_seconds
    assert claims.expires_at > utc_now()


def test_expired_access_token_is_rejected(make_settings) -> None:
    settings = make_settings()
    expired_at = utc_now() - timedelta(hours=2)
    token, _ = security.create_access_token(
        user_id=SAMPLE_USER_ID, role="STUDENT", settings=settings, now=expired_at
    )

    with pytest.raises(TokenExpiredError):
        security.decode_access_token(token, settings)


def test_tampered_access_token_is_rejected(make_settings) -> None:
    settings = make_settings()
    token, _ = security.create_access_token(
        user_id=SAMPLE_USER_ID, role="STUDENT", settings=settings
    )
    header, payload, signature = token.split(".")
    tampered = ".".join([header, payload, signature[:-4] + "AAAA"])

    with pytest.raises(TokenExpiredError):
        security.decode_access_token(tampered, settings)


def test_access_token_signed_with_other_secret_is_rejected(make_settings) -> None:
    issuer_settings = make_settings()
    verifier_settings = make_settings(app_secret_key="a-different-secret-key-0123456789abcdef")
    token, _ = security.create_access_token(
        user_id=SAMPLE_USER_ID, role="STUDENT", settings=issuer_settings
    )

    with pytest.raises(TokenExpiredError):
        security.decode_access_token(token, verifier_settings)


def test_token_with_wrong_type_is_rejected(make_settings) -> None:
    """其他用途的 JWT（例如未来的刷新令牌）不能当作访问令牌使用。"""
    settings = make_settings()
    now = utc_now()
    foreign = jwt.encode(
        {
            "sub": str(SAMPLE_USER_ID),
            "role": "STUDENT",
            "typ": "refresh",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        settings.app_secret_key,
        algorithm=security.ACCESS_TOKEN_ALGORITHM,
    )

    with pytest.raises(TokenExpiredError):
        security.decode_access_token(foreign, settings)


def test_signing_without_secret_fails_loudly(make_settings) -> None:
    """缺少 APP_SECRET_KEY 时宁可显式失败，也不能用空密钥签发。"""
    settings = make_settings(app_secret_key="")

    with pytest.raises(RuntimeError):
        security.create_access_token(
            user_id=SAMPLE_USER_ID, role="STUDENT", settings=settings
        )
