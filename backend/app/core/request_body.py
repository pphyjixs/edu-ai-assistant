"""写接口的请求体解析与 OpenAPI 声明（契约 1 / 6.2 / 7.2 / 7.5 / 7.6 / 10.1）。

两类场景都需要**手工解析**，原因是 FastAPI 会在解析依赖（认证、资源可见性、
角色、归档与状态检查、行锁）**之前**读取并解析 JSON 请求体，无法满足契约 7.1
固定的错误优先级：

- 没有请求字段的接口（创建会话、发布练习、重试任务）：只接受省略或空对象
  ``{}``；显式 ``null``、非对象、非法 UTF-8 与未声明字段都返回 422；
- 有请求字段的接口（生成练习、提交答案）：路由只拿原始 :class:`~fastapi.Request`，
  由服务在**加锁之后**调用 :func:`parse_required_object_body` 完成结构与字段校验。

改用原始 ``Request`` 后 FastAPI 不会再收集这些模型，因此路由需同时用
``openapi_extra`` 声明 ``requestBody``，并由 :func:`app.main.install_explicit_schemas`
把这些组件补进 OpenAPI，保证导出文档与契约一致。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TypeVar

from fastapi import Request
from pydantic import BaseModel, ValidationError as PydanticValidationError

from app.core.errors import ValidationError

#: 可注入的请求体解析器：在资源检查与加锁之后调用
ModelT = TypeVar("ModelT", bound=BaseModel)

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


def _field_errors(exc: PydanticValidationError) -> dict:
    """把 Pydantic 的字段级错误转成契约 1 的 ``details.errors`` 结构。"""
    items = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ())) or "body"
        items.append({"field": location, "message": error.get("msg", "取值不合法")})
    return {"errors": items}


def _decode_object(raw: bytes) -> dict | None:
    """解析请求体原始字节：非法 JSON / 非 UTF-8 / 非对象都转成 422。"""
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValidationError(
            "请求体不是合法 JSON",
            details=_errors("不是合法 JSON 或不是 UTF-8 编码"),
        ) from exc
    if payload is None:
        raise ValidationError(
            "请求体不能为 null：请省略请求体或传空对象 {}", details=_errors("不接受 null")
        )
    if not isinstance(payload, dict):
        raise ValidationError(
            "请求体必须是 JSON 对象", details=_errors("必须是 JSON 对象")
        )
    return payload


async def parse_required_object_body(
    request: Request, model_cls: type[ModelT]
) -> ModelT:
    """解析并校验**必有**的 JSON 对象请求体。

    约定：调用方（服务）已经完成认证、资源可见性、角色、归档与状态检查并持有
    所需行锁——这正是契约 7.1 的错误优先级要求。

    :raises ValidationError: 缺少请求体、非法 JSON / UTF-8、非对象，
        或字段未通过严格类型与边界校验（统一 422）。
    """
    raw = await request.body()
    if not raw.strip():
        raise ValidationError("请求体不能为空", details=_errors("缺少请求体"))
    payload = _decode_object(raw)
    if payload is None:  # pragma: no cover - 上面已拦截空请求体
        raise ValidationError("请求体不能为空", details=_errors("缺少请求体"))
    try:
        return model_cls.model_validate(payload)
    except PydanticValidationError as exc:
        raise ValidationError("请求参数不合法", details=_field_errors(exc)) from exc


def body_loader(
    request: Request, model_cls: type[ModelT]
) -> Callable[[], object]:
    """返回一个惰性解析器，便于调用方在正确的时刻触发请求体校验。"""

    async def load() -> ModelT:
        return await parse_required_object_body(request, model_cls)

    return load


async def validate_empty_object_body(request: Request) -> None:
    """校验"没有请求字段"的请求体。

    :raises ValidationError: 非法 JSON / 非 UTF-8 / 显式 ``null`` /
        非对象 / 含任何未声明字段（统一 422）。
    """
    payload = _decode_object(await request.body())
    if payload:
        raise ValidationError(
            "该接口没有请求字段",
            details={
                "errors": [
                    {"field": key, "message": "未声明字段"} for key in sorted(payload)
                ]
            },
        )


__all__ = [
    "EMPTY_OBJECT_REQUEST_BODY",
    "body_loader",
    "parse_required_object_body",
    "validate_empty_object_body",
]
