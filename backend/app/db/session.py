"""数据库引擎与会话。

范围（本阶段只做连接探测）：提供懒加载的异步引擎、``SELECT 1`` 探测和关闭钩子。
不创建 ORM Base、不声明模型、不执行迁移，符合
``docs/deployment-vercel.md`` 中「启动过程不得自动建表或执行迁移」的约束。

引擎在首次使用（``/health/ready`` 探测或业务请求）时才创建，避免冷启动时
把连接错误抛到模块导入阶段。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Annotated, NamedTuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.core.deps import SettingsDep
from app.core.logging import redact, truncate

logger = logging.getLogger("app.db")

#: 异步 PostgreSQL 驱动；DATABASE_URL 会被规范化到该协议
ASYNC_SCHEME = "postgresql+asyncpg"

#: 被接受的输入协议（Vercel/Neon/Supabase 常见写法）
_ACCEPTED_SCHEMES = {"postgres", "postgresql", "postgresql+asyncpg", "postgresql+psycopg"}

#: 这些 sslmode 需要为 asyncpg 显式开启 TLS
_TLS_SSL_MODES = {"require", "verify-ca", "verify-full"}

#: asyncpg 不认识的 query 参数，需从连接串中剔除
_UNSUPPORTED_PARAMS = {"sslmode", "channel_binding"}

#: 探测数据库使用的语句
PING_STATEMENT = "SELECT 1"


class DatabaseConfigError(RuntimeError):
    """DATABASE_URL 缺失或格式不受支持。消息可安全展示。"""


class DatabaseUnavailableError(RuntimeError):
    """数据库连接或探测失败。消息已脱敏，不含凭据。"""


class NormalizedDatabaseUrl(NamedTuple):
    """规范化后的连接串与其派生信息。"""

    url: str
    host: str
    requires_tls: bool


def normalize_database_url(raw: str) -> NormalizedDatabaseUrl:
    """把各种 PostgreSQL 连接串统一为 SQLAlchemy 异步驱动可用的形式。

    处理三类常见问题：

    - ``postgres://`` / ``postgresql://`` → ``postgresql+asyncpg://``；
    - ``?sslmode=require`` 等 asyncpg 不支持的参数会被剔除，并转换为
      显式 ``ssl=True``；
    - 去除多余空白，拒绝非 PostgreSQL 协议。
    """
    value = (raw or "").strip()
    if not value:
        raise DatabaseConfigError("DATABASE_URL 未配置")

    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme not in _ACCEPTED_SCHEMES:
        raise DatabaseConfigError(
            "DATABASE_URL 必须是 PostgreSQL 连接串（postgres:// 或 postgresql://）"
        )

    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in _UNSUPPORTED_PARAMS]
    ssl_mode = dict(parse_qsl(parts.query)).get("sslmode", "").lower()
    normalized = urlunsplit(
        (
            ASYNC_SCHEME,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )
    return NormalizedDatabaseUrl(
        url=normalized,
        host=parts.hostname or "unknown",
        requires_tls=ssl_mode in _TLS_SSL_MODES,
    )


def check_database_config(settings: Settings) -> str | None:
    """仅校验配置本身（不发起连接）。

    返回面向运维的问题描述；配置合法时返回 ``None``。
    """
    try:
        normalize_database_url(settings.database_url)
    except DatabaseConfigError as exc:
        return str(exc)
    return None


_engines: dict[str, AsyncEngine] = {}
_session_factories: dict[str, async_sessionmaker[AsyncSession]] = {}


def get_engine(settings: Settings) -> AsyncEngine:
    """返回（并按需创建）配置对应的异步引擎。

    引擎按连接串缓存，避免在 Serverless 冷启动与测试中反复创建连接池。
    """
    config = normalize_database_url(settings.database_url)
    cache_key = f"{config.url}|echo={settings.db_echo}|tls={config.requires_tls}"
    engine = _engines.get(cache_key)
    if engine is None:
        engine = _build_engine(settings, config)
        _engines[cache_key] = engine
    return engine


def _build_engine(
    settings: Settings, config: NormalizedDatabaseUrl
) -> AsyncEngine:
    connect_args: dict[str, object] = {
        "timeout": settings.db_connect_timeout_seconds,
    }
    if config.requires_tls:
        connect_args["ssl"] = True

    return create_async_engine(
        config.url,
        echo=settings.db_echo,
        # 连接池按 Serverless 环境取小值，并依赖 pre_ping 剔除失效连接
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args=connect_args,
    )


async def ping_database(
    settings: Settings, *, timeout_seconds: float | None = None
) -> None:
    """执行一次轻量连通性探测。

    只运行 ``SELECT 1``，不读业务表、不建表。

    :raises DatabaseConfigError: 连接串缺失或格式非法。
    :raises DatabaseUnavailableError: 连接失败或超时，消息已脱敏。
    """
    limit = timeout_seconds or settings.db_ready_timeout_seconds
    try:
        engine = get_engine(settings)
    except DatabaseConfigError:
        raise

    try:
        # asyncio.timeout 保证探测不会拖垮平台探针
        async with asyncio.timeout(limit):
            async with engine.connect() as connection:
                await connection.execute(text(PING_STATEMENT))
    except TimeoutError as exc:
        raise DatabaseUnavailableError(
            f"数据库连接超时（超过 {limit:g} 秒）"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 需要把驱动异常统一转成可展示信息
        raise DatabaseUnavailableError(_safe_database_error(exc)) from exc


async def dispose_engines() -> None:
    """关闭并清理全部引擎与会话工厂，用于应用关闭钩子。"""
    engines = list(_engines.values())
    _engines.clear()
    _session_factories.clear()
    for engine in engines:
        await engine.dispose()


def get_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    """返回（并按需创建）会话工厂，与引擎共用同一份缓存键。"""
    config = normalize_database_url(settings.database_url)
    cache_key = f"{config.url}|echo={settings.db_echo}|tls={config.requires_tls}"
    factory = _session_factories.get(cache_key)
    if factory is None:
        factory = async_sessionmaker(
            get_engine(settings),
            class_=AsyncSession,
            # 提交后仍可读取属性：service 在 commit 之后要把 ORM 对象转成响应模型
            expire_on_commit=False,
            # 关闭自动 flush，让写操作只在显式 flush/commit 时发生，便于定位事务边界
            autoflush=False,
        )
        _session_factories[cache_key] = factory
    return factory


async def get_session(settings: SettingsDep) -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：每个请求一个会话。

    这里 **不** 自动提交也不吞异常：事务边界由各模块的 service 决定
    （见 ``docs/architecture.md`` 第 4 节），异常直接向上冒泡交给异常处理器。
    """
    factory = get_session_factory(settings)
    async with factory() as session:
        yield session


#: 注入数据库会话；测试中可通过
#: ``app.dependency_overrides[get_session]`` 替换为测试库会话。
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _safe_database_error(exc: Exception) -> str:
    """把驱动异常转换为可安全展示的说明。

    驱动异常原文可能包含完整连接串（含用户名与密码），这里统一脱敏后再截断。
    """
    return truncate(f"{type(exc).__name__}: {redact(str(exc))}", 300)
