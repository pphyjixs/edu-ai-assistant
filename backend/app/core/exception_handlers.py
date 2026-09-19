"""异常处理器注册：把所有错误收敛到统一响应结构。

覆盖四类来源：

1. 业务抛出的 :class:`~app.core.errors.ApiError`（含契约全部稳定错误码）。
2. Pydantic 请求校验错误 —— 转换为统一结构，且 **不回显用户输入**
   （``RequestValidationError`` 默认带 ``input`` 字段，会把密码原文写进响应）。
3. Starlette/FastAPI 抛出的 :class:`HTTPException`（路由不存在、方法不允许等）。
4. 任何未预期异常 —— 只返回通用文案，堆栈仅进服务端日志。
"""

from __future__ import annotations

import logging
from http import HTTPStatus

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from app.core.error_codes import ErrorCode, default_message
from app.core.errors import ApiError, InternalError, ValidationError, build_error_body
from app.core.request_context import get_request_id_or_placeholder

logger = logging.getLogger("app.error")

#: 校验错误最多回传的字段条数，避免超大请求刷屏
MAX_VALIDATION_ERRORS = 20

#: 框架 HTTP 状态码到契约错误码的映射
_HTTP_STATUS_TO_CODE: dict[int, ErrorCode] = {
    401: ErrorCode.AUTH_TOKEN_EXPIRED,
    403: ErrorCode.ROLE_FORBIDDEN,
    404: ErrorCode.RESOURCE_NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    422: ErrorCode.VALIDATION_ERROR,
}


def register_exception_handlers(app: FastAPI) -> None:
    """把全部异常处理器挂到应用上。"""
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected_error)


async def _handle_api_error(_request, exc: ApiError) -> JSONResponse:
    """业务异常：直接使用异常自带的状态码与错误码。"""
    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_response_body(),
        headers=headers,
    )


async def _handle_validation_error(
    _request, exc: RequestValidationError
) -> JSONResponse:
    """请求校验失败：只保留字段定位与说明，丢弃可能含敏感值的原始输入。"""
    error = ValidationError(
        details={"errors": _sanitize_validation_errors(exc)},
    )
    return JSONResponse(status_code=error.status_code, content=error.to_response_body())


async def _handle_http_exception(
    _request, exc: StarletteHTTPException
) -> JSONResponse:
    """框架级 HTTP 错误：映射为契约错误码，文案使用统一中文提示。"""
    code = _resolve_http_error_code(exc.status_code)
    body = build_error_body(code, _safe_http_message(exc, code))

    headers = dict(exc.headers or {})
    if exc.status_code == 401:
        headers.setdefault("WWW-Authenticate", "Bearer")
    return JSONResponse(
        status_code=exc.status_code,
        content=body,
        headers=headers or None,
    )


async def _handle_unexpected_error(_request, exc: Exception) -> JSONResponse:
    """最终兜底：日志保留堆栈（出口已脱敏），响应只给通用文案。"""
    logger.exception("未处理异常 type=%s request_id=%s", type(exc).__name__, get_request_id_or_placeholder())
    error = InternalError()
    return JSONResponse(status_code=error.status_code, content=error.to_response_body())


def _resolve_http_error_code(status_code: int) -> ErrorCode:
    """把 HTTP 状态码映射为稳定错误码。"""
    if status_code in _HTTP_STATUS_TO_CODE:
        return _HTTP_STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return ErrorCode.INTERNAL_ERROR
    return ErrorCode.VALIDATION_ERROR


def _safe_http_message(exc: StarletteHTTPException, code: ErrorCode) -> str:
    """仅透传非空白的自定义 detail，其余情况使用契约默认文案。

    框架默认 detail 是英文短语（如 ``Not Found``），不满足前端统一展示要求，
    因此优先使用默认中文文案。
    """
    detail = exc.detail
    if isinstance(detail, str) and detail and detail != _default_starlette_detail(exc.status_code):
        return detail
    return default_message(code)


def _default_starlette_detail(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return ""


def _sanitize_validation_errors(exc: RequestValidationError) -> list[dict[str, object]]:
    """提取可安全回传的校验错误条目。

    刻意丢弃 ``input``/``ctx``/``url`` 字段：Pydantic 会把原始输入一并返回，
    注册请求中的密码会被原样写进响应体与访问日志。
    """
    sanitized: list[dict[str, object]] = []
    for item in exc.errors()[:MAX_VALIDATION_ERRORS]:
        sanitized.append(
            {
                "loc": [str(part) for part in item.get("loc", ())],
                "type": str(item.get("type", "")),
                "message": str(item.get("msg", "")),
            }
        )
    return sanitized
