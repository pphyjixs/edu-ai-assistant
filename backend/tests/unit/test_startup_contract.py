"""启动期约束测试。

对应 ``docs/deployment-vercel.md`` 第 4 节与 ``docs/architecture.md`` 第 2 节：

- 应用启动 **不得** 建表、不得执行迁移；
- 启动 **不得** 建立数据库连接（Serverless 冷启动不能依赖外部服务）。

这里用"禁止调用 + 断言未被调用"的方式做回归守卫：一旦以后有人在 lifespan 或
模块导入阶段加了 ``create_all`` / ``alembic upgrade``，测试会立即失败。
"""

from __future__ import annotations

from typing import Any

import alembic.command
import pytest
from fastapi.testclient import TestClient

from app.core.config import AppEnv, Settings
from app.db import session as db_session
from app.db.base import Base
from app.main import create_app


@pytest.fixture
def forbid_schema_changes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """把建表与迁移入口替换为会报错的哨兵，并记录调用。"""
    calls: list[str] = []

    def _forbidden_create_all(*args: Any, **kwargs: Any) -> None:
        calls.append("create_all")
        raise AssertionError("应用启动不得建表（Base.metadata.create_all）")

    def _forbidden_migrate(*args: Any, **kwargs: Any) -> None:
        calls.append("alembic")
        raise AssertionError("应用启动不得执行数据库迁移（alembic）")

    monkeypatch.setattr(Base.metadata, "create_all", _forbidden_create_all)
    monkeypatch.setattr(alembic.command, "upgrade", _forbidden_migrate)
    monkeypatch.setattr(alembic.command, "downgrade", _forbidden_migrate)
    return calls


@pytest.fixture
def fresh_engine_registry() -> None:
    """清空引擎缓存，让"启动是否连库"的断言不受其他用例影响。"""
    db_session._engines.clear()


def test_startup_does_not_create_schema_or_open_connection(
    fresh_engine_registry: None,
    make_client,
    forbid_schema_changes: list[str],
) -> None:
    with make_client() as client:
        # 存活检查不依赖任何外部服务，冷启动即可用
        assert client.get("/health/live").status_code == 200

        assert forbid_schema_changes == [], "启动阶段不得建表或执行迁移"
        assert db_session._engines == {}, (
            "启动阶段不得创建数据库引擎：连接必须推迟到 /health/ready 或业务请求"
        )


def test_importing_app_has_no_database_side_effects(fresh_engine_registry: None) -> None:
    """模块导入（Vercel 的冷启动路径）不得创建引擎。

    重新加载 ``app.main`` 会再次执行模块级的 ``app = create_app()``，
    等价于一次冷启动。
    """
    import importlib

    import app.main as main_module

    importlib.reload(main_module)

    assert db_session._engines == {}


def test_shutdown_releases_engine_pool(fresh_engine_registry: None, make_client) -> None:
    """关闭钩子负责释放连接池，避免 Serverless 实例复用后连接泄漏。"""
    with make_client() as client:
        assert client.get("/health/live").status_code == 200

    assert db_session._engines == {}


def test_process_starts_even_when_database_is_unreachable() -> None:
    """数据库不可达时进程仍能启动，问题由 /health/ready 报告，而不是启动失败。"""
    settings = Settings(
        _env_file=None,
        app_env=AppEnv.DEVELOPMENT,
        app_secret_key="startup-test-secret-key-0123456789abcdef",
        # 指向未监听的端口：如果启动阶段探测数据库，这里会直接超时失败
        database_url="postgresql://tester:secret@127.0.0.1:59999/edu_ai",
    )

    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").status_code == 200
