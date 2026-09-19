"""请求上下文：request ID 的生成、校验与全链路透传。

request ID 保存在 :mod:`contextvars` 中，因此错误响应、异常日志和业务日志
都能在不显式传参的情况下带上同一个标识（见 docs/deployment-vercel.md 第 7 节）。
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

#: 透传与回写使用的请求头名称
REQUEST_ID_HEADER = "X-Request-ID"

#: 上游可能来自任意网关，只接受安全字符集与有限长度，避免日志注入
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def generate_request_id() -> str:
    """生成新的 request ID（UUID4 十六进制形式）。"""
    return uuid.uuid4().hex


def normalize_request_id(candidate: str | None) -> str:
    """校验并透传上游 request ID，非法或缺失时生成新的。

    只做格式白名单校验：request ID 会被写入日志与响应头，必须排除换行、
    空格等可能造成日志伪造的字符。
    """
    if candidate:
        trimmed = candidate.strip()
        if _SAFE_REQUEST_ID.match(trimmed):
            return trimmed
    return generate_request_id()


def bind_request_id(candidate: str | None) -> str:
    """校验候选值、写入上下文，并返回最终使用的 request ID。"""
    request_id = normalize_request_id(candidate)
    _request_id_var.set(request_id)
    return request_id


def get_request_id() -> str | None:
    """读取当前上下文的 request ID；不在请求链路中时返回 ``None``。

    错误响应中该字段必须存在，因此调用方应使用 :func:`get_request_id_or_placeholder`。
    """
    return _request_id_var.get()


def get_request_id_or_placeholder(placeholder: str = "unknown") -> str:
    """读取 request ID，缺失时返回占位值，保证响应结构稳定。"""
    return _request_id_var.get() or placeholder


def reset_request_id() -> None:
    """清空当前上下文（主要用于测试与非请求场景）。"""
    _request_id_var.set(None)
