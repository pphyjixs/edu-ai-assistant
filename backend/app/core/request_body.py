"""没有请求字段的接口：请求体校验与 OpenAPI 声明（契约 6.2 / 7.5 / 10.1）。

这类接口（创建会话、发布练习、重试任务）只接受**省略请求体**或空对象
``{}``；显式 JSON ``null``、非对象、非法 UTF-8 与任何未声明字段都返回
``422 VALIDATION_ERROR``。

手工校验的原因：Pydantic 的"可选对象"会把显式 ``null`` 当成省略，
无法区分两者；因此请求体在路由层读取原始字节后判定，并在 OpenAPI 中用
:data:`EMPTY_OBJECT_REQUEST_BODY` 声明为**可选对象**（非 nullable）。
"""

from __future__ import annotations

import json

from fastapi import Request

from app.core.errors import ValidationError

#: OpenAPI 声明：可选对象请求体（非 null 类型）
EMPTY_OBJECT_REQUEST_BODY: dict = {
    "requestBody": {
        "required": False,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "description": "没有请求字段：可省略请求体或传空对象 {}",
                }
            }
        },
    }
}


def _errors(message: str, field: str = "body") -> dict:
    return {"errors": [{"field": field, "message": message}]}


async def validate_empty_object_body(request: Request) -> None:
    """校验"没有请求字段"的请求体。

    :raises ValidationError: 非法 JSON / 非 UTF-8 / 显式 ``null`` /
        非对象 / 含任何未声明字段（统一 422）。
    """
    raw = await request.body()
    if not raw.strip():
        return

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValidationError(
            "请求体不是合法 JSON",
            details=_errors("不是合法 JSON 或不是 UTF-8 编码"),
        ) from exc

    if payload is None:
        raise ValidationError(
            "请求体不能为 null：请省略请求体或传空对象 {}",
            details=_errors("不接受 null"),
        )
    if not isinstance(payload, dict):
        raise ValidationError(
            "请求体必须是 JSON 对象",
            details=_errors("必须是 JSON 对象"),
        )
    if payload:
        raise ValidationError(
            "该接口没有请求字段",
            details={
                "errors": [
                    {"field": key, "message": "未声明字段"} for key in sorted(payload)
                ]
            },
        )


__all__ = ["EMPTY_OBJECT_REQUEST_BODY", "validate_empty_object_body"]
