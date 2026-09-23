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
import faulthandler
import math
import os
import sys
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path

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
# 测试超时：自动取消 + 事件记录（pytest-timeout）
# --------------------------------------------------------------------------- #
# 超时时长配置（优先级从高到低）：
#   1. 单用例标记   @pytest.mark.timeout(30)   —— pytest-timeout 处理
#   2. 命令行       --timeout 60               —— pytest-timeout 处理
#   3. 环境变量     TEST_TIMEOUT_SECONDS=60    —— 本项目钩子解析（见下方）
#   4. 环境变量     PYTEST_TIMEOUT=60          —— pytest-timeout 处理
#   5. pytest.ini   timeout = 120              —— pytest-timeout 处理
#
# 超时行为按平台自动选择：
#   - POSIX（CI）：signal 方法。**当前测试失败**、留下完整报告，pytest 继续
#     运行并生成正常结果；超时事件由下方 ``pytest_runtest_makereport`` 钩子
#     补写入事件日志。
#   - Windows：thread 方法。pytest-timeout 默认 ``os._exit(1)`` 直接终止
#     **整个 pytest 进程**、不留任何上下文，因此这里接管计时器：先写入超时
#     事件与**全部线程栈**（死锁/卡住分析所需的关键证据），再退出。
#     异常退出意味着 **fixture 的清理不保证执行**（测试库删除、临时目录
#     清理等都可能被跳过），运行后需按 pg_support 的残留规则人工确认。
#
# 事件日志：默认 ``backend/test-reports/timeout-events.log``（已 gitignore）；
# 可用 ``TEST_TIMEOUT_EVENT_LOG`` 环境变量指向独立文件（子进程隔离测试用）。
_TIMEOUT_REPORT_DIR = Path(__file__).resolve().parent.parent / "test-reports"
_TIMEOUT_EVENT_LOG = Path(
    os.environ.get("TEST_TIMEOUT_EVENT_LOG")
    or (_TIMEOUT_REPORT_DIR / "timeout-events.log")
)
#: pytest-timeout signal 方法失败消息的固定前缀（PYTEST_FAILURE_MESSAGE）
_TIMEOUT_FAIL_SIGNATURE = "Timeout (>"
#: 项目级超时环境变量名
_TIMEOUT_ENV_VAR = "TEST_TIMEOUT_SECONDS"


def _parse_test_timeout_seconds(raw: str | None) -> float | None:
    """解析 ``TEST_TIMEOUT_SECONDS``（步骤二的独立解析函数）。

    :returns: 合法配置对应的正浮点数；未配置（环境变量不存在）返回 ``None``。
    :raises pytest.UsageError: 配置了但值非法——空字符串、非数字、
        ``NaN``、正负无穷、零或负数。错误信息包含变量名与原始值，
        pytest 会以**配置错误**退出而不是内部异常。
    """
    if raw is None:
        return None
    if not raw.strip():
        raise pytest.UsageError(f"{_TIMEOUT_ENV_VAR}={raw!r} 不能是空字符串")
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise pytest.UsageError(
            f"{_TIMEOUT_ENV_VAR}={raw!r} 不是合法的秒数（应为正数，例如 120）"
        ) from exc
    if not math.isfinite(value):
        raise pytest.UsageError(
            f"{_TIMEOUT_ENV_VAR}={raw!r} 必须是有限数（拒绝 NaN 与无穷）"
        )
    if value <= 0:
        raise pytest.UsageError(
            f"{_TIMEOUT_ENV_VAR}={raw!r} 必须是正数（超时时长不能为零或负数）"
        )
    return value


def _write_timeout_event(
    header: dict[str, object],
    *,
    include_thread_stacks: bool,
    detail: str = "",
) -> None:
    """把一次超时事件追加写入事件日志；记录失败不影响测试进程退出。"""
    try:
        _TIMEOUT_EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _TIMEOUT_EVENT_LOG.open("a", encoding="utf-8") as fh:
            fh.write("\n" + "=" * 72 + "\n")
            fh.write(f"TEST TIMEOUT  {datetime.now(timezone.utc).isoformat()}\n")
            for key, value in header.items():
                fh.write(f"{key:<12}: {value}\n")
            if detail:
                fh.write("-" * 72 + "\n")
                fh.write(detail)
            if include_thread_stacks:
                fh.write("-" * 72 + "\n")
                fh.write("thread stacks at timeout:\n")
                fh.flush()
                # 所有线程的栈快照：卡在哪个锁/哪个调用一目了然
                faulthandler.dump_traceback(file=fh)
    except Exception:  # noqa: BLE001 - 事件记录失败不能阻塞退出流程
        pass


def _timeout_header(item: pytest.Item, timeout: object) -> dict[str, object]:
    return {
        "nodeid": item.nodeid,
        "timeout": f"{timeout}s",
        "pid": os.getpid(),
        "platform": sys.platform,
    }


def pytest_configure(config: pytest.Config) -> None:
    """支持 ``TEST_TIMEOUT_SECONDS`` 环境变量覆盖默认超时。

    固定优先级：marker > CLI ``--timeout`` > ``TEST_TIMEOUT_SECONDS`` >
    ``PYTEST_TIMEOUT`` > ``pytest.ini``。后四者的顺序由写入时机保证：
    conftest 的 ``pytest_configure`` 先于 pytest-timeout 插件执行，此处把
    **解析后的浮点数**写入 ``config.option.timeout``，插件会把它当作命令行
    值读取（优先于自己的 ``PYTEST_TIMEOUT`` 环境变量与 ini 默认值）。
    CLI 显式传参仍然最高——那种情况下这里不做任何解析或校验。
    """
    if getattr(config.option, "timeout", None) is not None:
        return  # CLI 显式设置：以 CLI 为准，不读取也不校验环境变量
    parsed = _parse_test_timeout_seconds(os.environ.get(_TIMEOUT_ENV_VAR))
    if parsed is not None:
        config.option.timeout = parsed


def pytest_timeout_set_timer(item: pytest.Item, settings: object) -> bool | None:
    """接管 thread 方法的计时器（Windows 默认路径）。

    插件默认实现超时后直接 ``os._exit(1)``，线程栈只打到终端、随进程一起丢
    失。这里替换为：先写事件日志（含全部线程栈），再以同样方式退出。
    signal 方法（POSIX/CI）返回 None，保留插件默认行为：测试失败并留下报告。
    """
    if getattr(settings, "method", None) != "thread":
        return None
    timeout = getattr(settings, "timeout", None)
    if not timeout:
        return None

    def _on_timeout() -> None:
        _write_timeout_event(
            _timeout_header(item, timeout), include_thread_stacks=True
        )
        os._exit(1)

    def _cancel() -> None:
        timer.cancel()
        timer.join()

    timer = threading.Timer(timeout, _on_timeout)
    timer.name = f"conftest-timeout {item.nodeid}"
    item.cancel_timeout = _cancel
    timer.start()
    return True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Iterator[None]:
    """signal 方法（POSIX/CI）超时时测试会失败：把事件补写进日志。"""
    outcome = yield
    try:
        report = outcome.get_result()
        if report.when != "call" or not report.failed:
            return
        longrepr = report.longreprtext
        if _TIMEOUT_FAIL_SIGNATURE not in longrepr:
            return
        _write_timeout_event(
            {
                **_timeout_header(item, "report"),
                "phase": report.when,
                "duration": f"{report.duration:.1f}s",
            },
            include_thread_stacks=False,
            detail=f"{longrepr}\nstdout:\n{report.capstdout}\nstderr:\n{report.capstderr}\n",
        )
    except Exception:  # noqa: BLE001 - 事件记录失败不影响测试报告
        pass


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
