"""CORS 配置：只允许明确来源。

按 ``docs/deployment-vercel.md`` 第 1 节，后端只放行：

- ``FRONTEND_ORIGINS`` 中列出的正式前端域名与本地开发地址；
- 命中 ``FRONTEND_ORIGIN_REGEX`` 的预览域名（Vercel 每个 Preview 一个域名）。

不使用 ``allow_origins=["*"]``：本服务需要携带 ``Authorization`` 头，
通配来源与凭据不能共存，且会把接口暴露给任意站点。

实验报告直传（契约 9.1）还要求预检放行浏览器直传对象存储所需的方法与头：
``PUT`` / ``HEAD`` 与 ``x-amz-checksum-sha256``、``If-None-Match``——
预签名地址要求客户端逐字回传这些头，缺一个预检就会失败。
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings

#: 允许的请求方法，按契约只使用 REST 子集（PUT/HEAD 供对象直传与下载）
ALLOWED_METHODS = ["GET", "POST", "PATCH", "PUT", "HEAD", "DELETE", "OPTIONS"]

#: 允许的请求头：认证、JSON、request ID 透传，以及对象直传的签名请求头
ALLOWED_HEADERS = [
    "Authorization",
    "Content-Type",
    "Accept",
    "X-Request-ID",
    "x-amz-checksum-sha256",
    "If-None-Match",
]

#: 需要暴露给浏览器脚本读取的响应头（前端用 request ID 报障）
EXPOSED_HEADERS = ["X-Request-ID"]

#: 预检结果缓存时间（秒）
MAX_AGE_SECONDS = 600


def build_cors_middleware_kwargs(settings: Settings) -> dict[str, Any]:
    """生成 ``CORSMiddleware`` 的构造参数。

    :raises ValueError: 当 ``FRONTEND_ORIGINS`` 含通配符时（由
        :attr:`Settings.cors_origins` 抛出），保证错误配置在启动阶段暴露。
    """
    return {
        "allow_origins": settings.cors_origins,
        "allow_origin_regex": settings.cors_origin_regex,
        "allow_credentials": True,
        "allow_methods": ALLOWED_METHODS,
        "allow_headers": ALLOWED_HEADERS,
        "expose_headers": EXPOSED_HEADERS,
        "max_age": MAX_AGE_SECONDS,
    }
