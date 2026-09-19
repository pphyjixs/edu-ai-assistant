"""FastAPI 应用入口。

对外暴露模块级 ``app`` 对象，满足 ``docs/deployment-vercel.md`` 第 4 节
对 Vercel Python Function 的要求：``from app.main import app``。

启动过程刻意保持“轻”:不建表、不执行迁移、不下载文件、不加载模型；
数据库连接只为服务健康检查与首屏请求按需创建。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.v1 import api_router
from app.core.config import Settings, get_settings
from app.core.cors import build_cors_middleware_kwargs
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import setup_logging
from app.core.middleware import RequestContextMiddleware
from app.core.schemas import ErrorResponse
from app.db.session import dispose_engines

logger = logging.getLogger("app.main")

API_DESCRIPTION = (
    "AI 教学助手后端服务。接口契约见 docs/api-contract.md；"
    "所有响应错误统一为 {error: {code, message, details, request_id}} 结构。"
)

#: 所有端点都可能返回的服务端错误。挂在这里是为了让导出的 OpenAPI（进而前端
#: 生成的类型）包含统一错误结构，具体业务错误码再按端点各自补充。
DEFAULT_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    500: {"model": ErrorResponse, "description": "未预期的服务端错误"},
}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：只做日志与资源释放，不做初始化副作用。"""
    settings: Settings = app.state.settings
    logger.info(
        "应用启动 env=%s version=%s",
        settings.app_env.value,
        settings.app_version,
    )

    problems = settings.config_problems()
    if problems:
        # 不在这里抛错：让实例先起来，由 /health/ready 明确报告缺失项。
        logger.warning("配置不完整，/health/ready 将返回 503：%s", "; ".join(problems))

    try:
        yield
    finally:
        await dispose_engines()
        logger.info("应用关闭，数据库引擎已释放")


def create_app(settings: Settings | None = None) -> FastAPI:
    """构建应用实例。

    :param settings: 可注入的配置，便于测试替换；默认读取环境变量。
    :raises ValueError: ``FRONTEND_ORIGINS`` 使用通配符时立即失败，
        避免把带凭据的接口暴露给任意来源。
    """
    resolved = settings or get_settings()
    setup_logging(resolved.log_level)

    app = FastAPI(
        title=resolved.app_name,
        version=resolved.app_version,
        description=API_DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        responses=DEFAULT_ERROR_RESPONSES,
    )
    app.state.settings = resolved

    # 中间件顺序：后 add 的更靠外层。CORS 先加、请求上下文后加，
    # 这样连 CORS 预检等早期响应也会带上 X-Request-ID 与访问日志。
    app.add_middleware(CORSMiddleware, **build_cors_middleware_kwargs(resolved))
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    # 健康检查不在 /api/v1 之下，供平台探针直接访问。
    app.include_router(health_router)
    app.include_router(api_router, prefix=resolved.api_v1_prefix)

    return app


#: 模块级 ASGI 应用（Vercel / uvicorn 入口）
app = create_app()
