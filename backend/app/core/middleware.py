"""请求上下文中间件：request ID 透传与结构化访问日志。

职责：

1. 从 ``X-Request-ID`` 头透传上游 request ID，缺失或非法时生成新的；
   把结果写入 ``request.state.request_id`` 与响应头，供前端报障时回传。
2. 记录接口、状态码、耗时与用户 ID，**不记录** 请求体、query string 或响应体。
3. 兜住未被业务异常处理器捕获的异常，返回统一错误结构，避免向前端泄露堆栈。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from app.core.errors import InternalError
from app.core.request_context import (
    REQUEST_ID_HEADER,
    bind_request_id,
    reset_request_id,
)

logger = logging.getLogger("app.access")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """为每个请求绑定 request ID，并输出一条访问日志。"""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = bind_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        started_at = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - 这里是最终兜底，必须捕获全部异常
            duration_ms = _elapsed_ms(started_at)
            # exc_info 会写入堆栈，但日志格式化出口已统一脱敏与截断。
            logger.exception(
                "%s %s -> 500 %.1fms user=%s",
                request.method,
                request.url.path,
                duration_ms,
                _user_id(request),
                extra={"request_id": request_id},
            )
            error = InternalError()
            response = JSONResponse(
                status_code=error.status_code,
                content=error.to_response_body(),
            )
        else:
            logger.info(
                "%s %s -> %s %.1fms user=%s",
                request.method,
                request.url.path,
                response.status_code,
                _elapsed_ms(started_at),
                _user_id(request),
                extra={"request_id": request_id},
            )

        response.headers[REQUEST_ID_HEADER] = request_id
        reset_request_id()
        return response


def _elapsed_ms(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000


def _user_id(request: Request) -> str:
    """读取认证依赖写入的用户 ID；未认证时统一记 ``-``。"""
    return str(getattr(request.state, "user_id", None) or "-")
