"""Auth 安全基础：邮箱规范化、密码哈希与令牌。

本模块是纯函数/纯逻辑层，不碰数据库，便于单元测试与替换。

安全约定（对应 ``docs/api-contract.md`` 第 2 节）：

- 密码只保存 Argon2id 哈希；任何情况下不得把明文或哈希写进日志与响应。
- Refresh Token 是密码学安全随机值，数据库只保存 sha256 摘要。
  这里用快哈希而非 Argon2 是有意为之：Token 有 192 bit 熵，不存在爆破空间，
  而登录/刷新是热路径，慢哈希会显著抬高成本。
- Access Token 是带到期时间的 HS256 签名令牌，服务端不保存、不维护黑名单，
  因此旧 Access Token 可能在注销后继续有效至自身过期（契约 2.5 节）。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings
from app.core.errors import TokenExpiredError
from app.core.time import to_utc, utc_now

#: Argon2id 哈希器。使用库默认参数：time_cost=3、memory_cost=64MiB、parallelism=4
_password_hasher = PasswordHasher()

#: Access Token 签名算法
ACCESS_TOKEN_ALGORITHM = "HS256"

#: Access Token 类型标记，防止其他用途的 JWT 被当作访问令牌使用
ACCESS_TOKEN_TYPE = "access"

#: Refresh Token 随机字节数：48 字节 → 64 字符 base64url，约 192 bit 熵
REFRESH_TOKEN_BYTES = 48

#: 令牌无效时的统一对外文案，不区分“过期”与“伪造”
INVALID_TOKEN_MESSAGE = "访问令牌无效，请重新登录"


def normalize_email(email: str) -> str:
    """按契约 2.1 生成邮箱比较值。

    只把域名部分小写化；本地部分（``@`` 之前）保留大小写，也不移除句点或
    ``+`` 后缀。因此 ``Teacher@EXAMPLE.com`` 与 ``Teacher@example.com`` 是同一账号，
    而 ``teacher@example.com`` 是另一个账号。

    :raises ValueError: 邮箱缺少 ``@`` 或两侧为空。
    """
    local, separator, domain = email.rpartition("@")
    if not separator or not local or not domain:
        raise ValueError("邮箱格式不正确")
    return f"{local}@{domain.lower()}"


def hash_password(password: str) -> str:
    """生成 Argon2id 哈希。

    不对密码做 ``strip()`` 或截断：契约要求长度与空白原样参与校验。
    """
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """校验密码是否匹配给定哈希。"""
    try:
        _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """进程内只算一次的假哈希，用于补齐不存在账号的校验耗时。"""
    return _password_hasher.hash(secrets.token_urlsafe(32))


def verify_password_constant_time(password_hash: str | None, password: str) -> bool:
    """恒定开销的密码校验，用于登录。

    账号不存在时（``password_hash is None``）也执行一次等价昂贵的 Argon2 校验，
    使「账号不存在」与「密码错误」的响应时间接近，避免通过计时反推账号是否存在；
    两种情况对外都返回同一条错误信息。
    """
    target = password_hash if password_hash is not None else _dummy_password_hash()
    matched = verify_password(target, password)
    return matched and password_hash is not None


def generate_refresh_token() -> str:
    """生成密码学安全的 Refresh Token 明文（只在登录响应中出现一次）。"""
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def hash_refresh_token(token: str) -> str:
    """计算 Refresh Token 的存储摘要（sha256 十六进制）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """校验通过后的 Access Token 声明。"""

    user_id: uuid.UUID
    role: str
    token_id: str
    issued_at: datetime
    expires_at: datetime


def create_access_token(
    *,
    user_id: uuid.UUID,
    role: str,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[str, int]:
    """签发 Access Token。

    :returns: ``(token, expires_in_seconds)``，与契约响应字段一一对应。
    :raises RuntimeError: ``APP_SECRET_KEY`` 未配置——这是部署配置错误，
        宁可显式失败，也不要用空密钥签发可被任意伪造的令牌。
    """
    secret = settings.app_secret_key.strip()
    if not secret:
        raise RuntimeError("APP_SECRET_KEY 未配置，无法签发访问令牌")

    issued_at = to_utc(now or utc_now())
    expires_in = settings.access_token_expire_seconds
    expires_at = issued_at + timedelta(seconds=expires_in)

    payload = {
        "sub": str(user_id),
        "role": role,
        "typ": ACCESS_TOKEN_TYPE,
        # jti 让每次签发的令牌都唯一：iat/exp 只有秒级精度，
        # 同一秒内对同一用户签发的令牌否则会完全相同。
        "jti": uuid.uuid4().hex,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(payload, secret, algorithm=ACCESS_TOKEN_ALGORITHM)
    return token, expires_in


def decode_access_token(token: str, settings: Settings) -> AccessTokenClaims:
    """校验 Access Token 的签名与到期时间，并返回其声明。

    :raises TokenExpiredError: 签名非法、已过期、缺少必需声明或类型不符。
        统一使用 ``AUTH_TOKEN_EXPIRED`` 错误码，客户端据此重新登录。
    """
    secret = settings.app_secret_key.strip()
    if not secret:
        raise RuntimeError("APP_SECRET_KEY 未配置，无法校验访问令牌")

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ACCESS_TOKEN_ALGORITHM],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError() from exc
    except jwt.InvalidTokenError as exc:
        raise TokenExpiredError(INVALID_TOKEN_MESSAGE) from exc

    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        raise TokenExpiredError(INVALID_TOKEN_MESSAGE)

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise TokenExpiredError(INVALID_TOKEN_MESSAGE) from exc

    return AccessTokenClaims(
        user_id=user_id,
        role=str(payload.get("role", "")),
        token_id=str(payload.get("jti", "")),
        issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=utc_now().tzinfo),
        expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=utc_now().tzinfo),
    )
