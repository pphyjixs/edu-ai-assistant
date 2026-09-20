"""共享测试夹具。

分三层：

1. **单元测试夹具**（``make_settings`` / ``make_client``）：不接触数据库，
   用显式构造的 :class:`Settings` 隔离本地 ``.env``。
2. **PostgreSQL 测试库夹具**（``pg_test_url`` / ``pg_sync_engine``）：独立测试库，
   用 Alembic 迁移建表，会话结束删除（见 ``tests/pg_support.py``）。
3. **应用夹具**（``pg_app`` / ``api_client`` / ``async_api_client``）：把应用的会话
   依赖指向测试库，并在用例之间清空业务表。

集成测试与契约测试都跑在真实 PostgreSQL 上：只有这样才能验证
唯一约束、``TIMESTAMPTZ``、枚举类型与事务行为。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import AppEnv, Settings
from app.db.session import get_session
from app.main import create_app
from tests import pg_support

#: 单元测试用的合法连接串（不指向真实数据库，探测会被替换为 fake）
TEST_DATABASE_URL = "postgresql://tester:not-a-real-password@localhost:5432/edu_ai"

#: 单元/集成测试用的强密钥
TEST_SECRET_KEY = "test-secret-key-with-enough-length-0123456789abcdef"

SettingsFactory = Callable[..., Settings]
AppBuilder = Callable[..., FastAPI]


# --------------------------------------------------------------------------- #
# 单元测试
# --------------------------------------------------------------------------- #
@pytest.fixture
def make_settings() -> SettingsFactory:
    """返回构造 :class:`Settings` 的工厂，默认是一份齐备的开发配置。"""

    def _make(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "app_env": AppEnv.DEVELOPMENT,
            "database_url": TEST_DATABASE_URL,
            "app_secret_key": TEST_SECRET_KEY,
            "frontend_origins": "http://localhost:5173",
            # 解析 Worker 默认关闭：上传/删除/重试等接口测试不依赖后台解析；
            # Worker 行为由专项用例显式开启（material_parse_worker_enabled=True）
            "material_parse_worker_enabled": False,
        }
        values.update(overrides)
        # _env_file=None: 隔离本地 .env，保证测试可重复
        return Settings(_env_file=None, **values)  # type: ignore[arg-type]

    return _make


@pytest.fixture
def make_client(make_settings: SettingsFactory) -> Iterator[Callable[..., TestClient]]:
    """返回构造 :class:`TestClient` 的工厂（不接触数据库）。"""
    stack = ExitStack()

    def _make(**overrides: object) -> TestClient:
        return stack.enter_context(TestClient(create_app(make_settings(**overrides))))

    try:
        yield _make
    finally:
        stack.close()


# --------------------------------------------------------------------------- #
# PostgreSQL 测试库
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def pg_test_url() -> Iterator[str]:
    """独立测试库：按需创建，用 Alembic 迁移建表，会话结束删除。

    - 设置了 ``TEST_DATABASE_URL`` 就直接用它，**不要求**同时配置 ``DATABASE_URL``；
    - 未设置时由 ``DATABASE_URL`` 的库名派生 ``<库名>_test``；
    - 库名必须是 ``_test`` 结尾（见 ``tests/pg_support.assert_test_database_name``），
      与开发库或系统库同名会被拒绝，避免误删开发数据。
    """
    try:
        url = pg_support.resolve_test_database_url()
    except Exception as exc:  # noqa: BLE001 - 未配置或连接串非法
        pytest.skip(
            f"未配置可用的测试数据库（TEST_DATABASE_URL 或 DATABASE_URL）：{exc}"
        )

    name = pg_support.database_name(url)
    if not pg_support.is_postgres_reachable(url):
        pytest.skip("PostgreSQL 实例不可达，无法准备测试库")

    admin = pg_support.admin_engine(url)
    created = False
    try:
        pg_support.create_test_database(
            admin, name, base_name=pg_support.configured_base_name()
        )
        created = True
        # 用迁移建表（不是 metadata.create_all），保证与部署形态一致
        pg_support.upgrade_head(url)
        yield url
    finally:
        try:
            if created:
                pg_support.drop_test_database(admin, name)
        finally:
            admin.dispose()


@pytest.fixture(scope="session")
def pg_sync_engine(pg_test_url: str) -> Iterator[Engine]:
    """同步引擎，供测试直接查库断言。"""
    from sqlalchemy import create_engine

    engine = create_engine(pg_support.to_sync(pg_test_url), poolclass=NullPool)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def pg_session_factory(
    pg_test_url: str,
) -> Iterator[async_sessionmaker[AsyncSession]]:
    """测试库的异步会话工厂。

    使用 ``NullPool``：连接按需创建、用完即关，避免连接被绑定到某个事件循环后
    在另一个用例的循环里复用（TestClient 自带线程与循环，asyncio 用例又各有循环）。
    """
    engine = create_async_engine(pg_test_url, poolclass=NullPool)
    try:
        yield async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
    finally:
        try:
            asyncio.run(engine.dispose())
        except Exception:  # noqa: BLE001 - 跨循环清理失败不影响测试结论
            pass


@pytest.fixture
def db_isolation(pg_sync_engine: Engine) -> Iterator[None]:
    """用例前后清空业务表，保留迁移建好的 schema。"""
    pg_support.truncate_all_tables(pg_sync_engine)
    yield
    pg_support.truncate_all_tables(pg_sync_engine)


# --------------------------------------------------------------------------- #
# 指向测试库的应用
# --------------------------------------------------------------------------- #
@pytest.fixture
def pg_app(
    make_settings: SettingsFactory, pg_test_url: str, pg_session_factory
) -> AppBuilder:
    """返回构造应用的工厂；应用的数据库会话指向测试库。"""

    def _build(**settings_overrides: object) -> FastAPI:
        values: dict[str, object] = {
            # 默认连测试库；需要验证"数据库不可达"等场景时可以覆盖
            "database_url": pg_test_url,
            **settings_overrides,
        }
        app = create_app(make_settings(**values))

        async def _session_override() -> AsyncIterator[AsyncSession]:
            async with pg_session_factory() as session:
                yield session

        app.dependency_overrides[get_session] = _session_override
        return app

    return _build


@pytest.fixture
def api_client(db_isolation: None, pg_app: AppBuilder) -> Iterator[TestClient]:
    """同步 HTTP 客户端，连真实 PostgreSQL 测试库。"""
    with TestClient(pg_app()) as client:
        yield client


@pytest.fixture
async def async_api_client(
    db_isolation: None, pg_app: AppBuilder
) -> AsyncIterator[httpx.AsyncClient]:
    """异步 HTTP 客户端，用于真实并发场景。"""
    transport = httpx.ASGITransport(app=pg_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=30.0
    ) as client:
        yield client
