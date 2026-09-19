"""健康检查端点。

按 ``docs/deployment-vercel.md`` 第 5 节，两个端点位于 ``/api/v1`` 之外，
供平台探针直接访问：

- ``GET /health/live``：进程可响应即 200，**不访问任何外部服务**，用于存活探针。
- ``GET /health/ready``：检查必需配置与数据库连通性，任一项不可用返回 503。

两者都不调用大模型、不访问对象存储、不执行迁移或建表。
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.deps import SettingsDep
from app.core.errors import ServiceUnavailableError
from app.core.schemas import ErrorResponse
from app.db.session import (
    DatabaseConfigError,
    DatabaseUnavailableError,
    check_database_config,
    ping_database,
)

router = APIRouter(tags=["health"])

#: 健康检查结果不缓存，避免探针读到过期状态
_NO_STORE_HEADERS = {"Cache-Control": "no-store"}

CHECK_OK = "ok"
CHECK_UNAVAILABLE = "unavailable"


@router.get(
    "/health/live",
    summary="进程存活检查",
    description="进程可响应时返回 200，不访问数据库、对象存储或大模型。",
)
async def liveness() -> JSONResponse:
    """存活探针：只证明事件循环仍在处理请求。"""
    return JSONResponse(
        status_code=200,
        content={"status": CHECK_OK, "check": "live"},
        headers=_NO_STORE_HEADERS,
    )


@router.get(
    "/health/ready",
    summary="依赖就绪检查",
    description=(
        "检查必需环境变量与数据库连通性。全部就绪返回 200，"
        "任一不可用返回 503 且响应体为统一错误结构。"
    ),
    responses={
        200: {"description": "配置齐备且数据库可达"},
        503: {
            "model": ErrorResponse,
            "description": "配置缺失或数据库不可达（SERVICE_UNAVAILABLE）",
        },
    },
)
async def readiness(settings: SettingsDep) -> JSONResponse:
    """就绪探针：配置齐备且数据库可执行 ``SELECT 1`` 才算就绪。"""
    config_problems = settings.config_problems()
    checks: dict[str, str] = {
        "config": CHECK_UNAVAILABLE if config_problems else CHECK_OK
    }

    database_config_problem = check_database_config(settings)
    database_problem: str | None = None

    if database_config_problem:
        # 连接串本身非法时不做无意义的连接尝试
        checks["database"] = CHECK_UNAVAILABLE
    else:
        try:
            await ping_database(settings)
            checks["database"] = CHECK_OK
        except (DatabaseConfigError, DatabaseUnavailableError) as exc:
            checks["database"] = CHECK_UNAVAILABLE
            database_problem = str(exc)

    if all(result == CHECK_OK for result in checks.values()):
        return JSONResponse(
            status_code=200,
            content={"status": CHECK_OK, "checks": checks},
            headers=_NO_STORE_HEADERS,
        )

    details: dict[str, object] = {"checks": checks}
    if config_problems:
        details["config_problems"] = config_problems
    reason = database_config_problem or database_problem
    if reason:
        details["database_problem"] = reason

    error = ServiceUnavailableError(details=details)
    return JSONResponse(
        status_code=error.status_code,
        content=error.to_response_body(),
        headers=_NO_STORE_HEADERS,
    )
