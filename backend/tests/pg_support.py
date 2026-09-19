"""PostgreSQL 测试库工具。

集成测试与契约测试统一使用 **专用测试库**，并严格用 **Alembic 迁移** 建表，
不用 ``Base.metadata.create_all``：只有这样，测试跑的形状才和真实部署一致
（枚举类型、``TIMESTAMPTZ``、约束命名都来自迁移文件本身）。

测试库规则（``docs/acceptance.md`` 第 12 节）：

1. **只设置 ``TEST_DATABASE_URL`` 也能跑**：不需要 ``DATABASE_URL``。
   未设置 ``TEST_DATABASE_URL`` 时，由 ``DATABASE_URL`` 的库名派生 ``<库名>_test``。
2. **只允许 ``_test`` 结尾的专用库**：与开发库或系统库同名的名称一律拒绝，
   避免误删开发数据。
3. **创建前检查冲突**：目标库若已存在且有其他连接在用，直接报错而不是重建。
4. **结束后清理**：会话结束删除测试库。

本模块文件名不匹配 ``test_*.py``，因此不会被 pytest 收集。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

#: 后端根目录
BACKEND_DIR = Path(__file__).resolve().parents[1]

#: 集成测试库后缀（强制要求）
TEST_SUFFIX = "_test"

#: 迁移专项校验库后缀（迁移用例会清空整个 schema，必须与集成测试库分开）
MIGRATION_SUFFIX = "_migration_check"

#: 执行 CREATE/DROP DATABASE 时连接的维护库
MAINTENANCE_DATABASE = "postgres"

#: 绝不允许当作测试库使用的库名
PROTECTED_DATABASES = frozenset({"postgres", "template0", "template1"})

#: 库名白名单：CREATE DATABASE 不能用绑定参数，只能拼标识符，必须校验
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,52}$")

_ASYNC_DRIVER = "postgresql+asyncpg"
_SYNC_DRIVER = "postgresql+psycopg2"


class DatabaseNameConflict(RuntimeError):
    """测试库名不合法或存在冲突，宁可报错也不动手改别人的库。"""


def ensure_backend_on_path() -> None:
    """让测试代码能 ``import app.*``（pytest.ini 的 pythonpath 之外再兜一层）。"""
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))


ensure_backend_on_path()


# --------------------------------------------------------------------------- #
# 连接串处理
# --------------------------------------------------------------------------- #
def to_sync(url: str) -> str:
    """换成 psycopg2 同步驱动。"""
    return make_url(url).set(drivername=_SYNC_DRIVER).render_as_string(
        hide_password=False
    )


def to_async(url: str) -> str:
    """换成 asyncpg 异步驱动。"""
    return make_url(url).set(drivername=_ASYNC_DRIVER).render_as_string(
        hide_password=False
    )


def with_database(url: str, database: str) -> str:
    """替换连接串中的数据库名。"""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, ""))


def database_name(url: str) -> str:
    return make_url(url).database or ""


def derive_database_name(url: str, suffix: str) -> str:
    """由现有库名派生测试库名，例如 ``edu_ai_dev`` → ``edu_ai_dev_test``。"""
    return f"{database_name(url)}{suffix}"


def configured_base_name() -> str | None:
    """开发库名，仅用于冲突检查；未配置 ``DATABASE_URL`` 时返回 ``None``。"""
    try:
        from app.core.config import get_settings
        from app.db.session import normalize_database_url

        return database_name(normalize_database_url(get_settings().database_url).url) or None
    except Exception:  # noqa: BLE001 - 未配置或连接串非法都视为"没有源库"
        return None


def derive_migration_database_name(test_url: str) -> str:
    """由测试库名派生迁移专项库名，例如 ``x_test`` → ``x_migration_check``。"""
    name = database_name(test_url)
    if name.endswith(TEST_SUFFIX):
        return name[: -len(TEST_SUFFIX)] + MIGRATION_SUFFIX
    return name + MIGRATION_SUFFIX


def resolve_test_database_url() -> str:
    """确定测试库连接串（异步形式）。

    - 设置了 ``TEST_DATABASE_URL``：直接用它（因此**不要求**配置 ``DATABASE_URL``）；
    - 否则：从 ``DATABASE_URL`` 派生 ``<库名>_test``。

    :raises RuntimeError: 两者都没有配置，或 ``DATABASE_URL`` 不是 PostgreSQL 连接串。
    """
    explicit = os.environ.get("TEST_DATABASE_URL", "").strip()
    if explicit:
        return to_async(explicit)

    from app.core.config import get_settings
    from app.db.session import normalize_database_url

    return with_database(
        normalize_database_url(get_settings().database_url).url,
        derive_database_name(
            normalize_database_url(get_settings().database_url).url, TEST_SUFFIX
        ),
    )


# --------------------------------------------------------------------------- #
# 库名守卫
# --------------------------------------------------------------------------- #
def assert_test_database_name(
    name: str, *, suffix: str = TEST_SUFFIX, base_name: str | None = None
) -> str:
    """校验测试库名，返回该名字（便于链式调用）。

    :raises DatabaseNameConflict: 名字不合法、不是专用测试库、与系统库或开发库同名。
    """
    if not SAFE_IDENTIFIER.match(name):
        raise DatabaseNameConflict(
            f"测试库名不合法: {name!r}（只允许字母、数字与下划线，且以字母或下划线开头）"
        )
    if not name.endswith(suffix):
        raise DatabaseNameConflict(
            f"测试库名必须以 {suffix!r} 结尾，拒绝操作 {name!r}"
        )
    if name in PROTECTED_DATABASES:
        raise DatabaseNameConflict(f"拒绝把系统库 {name!r} 当作测试库")
    if base_name and name == base_name:
        raise DatabaseNameConflict(f"测试库名不能与源库 {name!r} 相同")
    return name


# --------------------------------------------------------------------------- #
# 建库 / 删库
# --------------------------------------------------------------------------- #
def admin_engine(target_url: str) -> Engine:
    """连到目标实例维护库的 AUTOCOMMIT 引擎，用于建库 / 删库。"""
    return create_engine(
        with_database(to_sync(target_url), MAINTENANCE_DATABASE),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )


def database_exists(engine: Engine, name: str) -> bool:
    with engine.connect() as connection:
        return bool(
            connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": name},
            ).scalar()
        )


def active_connections(engine: Engine, name: str) -> int:
    """返回除当前连接外、仍连着该库的后端数量。"""
    with engine.connect() as connection:
        return int(
            connection.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity"
                    " WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            ).scalar_one()
        )


def _terminate_connections(engine: Engine, name: str) -> None:
    with engine.connect() as connection:
        connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": name},
        )


def create_test_database(
    engine: Engine, name: str, *, suffix: str = TEST_SUFFIX, base_name: str | None = None
) -> None:
    """创建专用测试库。

    已存在时先做冲突检查：有其他连接在用就直接报错，否则视为上次残留并重建。
    """
    assert_test_database_name(name, suffix=suffix, base_name=base_name)

    if database_exists(engine, name):
        in_use = active_connections(engine, name)
        if in_use:
            raise DatabaseNameConflict(
                f"测试库 {name!r} 仍有 {in_use} 个连接在用，拒绝重建；"
                "请先关闭正在跑测试的程序，或换一个 TEST_DATABASE_URL"
            )
        drop_database(engine, name)

    with engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))


def drop_test_database(
    engine: Engine, name: str, *, suffix: str = TEST_SUFFIX
) -> None:
    """删除专用测试库（先断开残留连接）。"""
    assert_test_database_name(name, suffix=suffix)
    _terminate_connections(engine, name)
    with engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


def drop_database(engine: Engine, name: str) -> None:
    """低层删除：不做后缀检查，仅供 ``create_test_database`` 重建残留时使用。"""
    _terminate_connections(engine, name)
    with engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


def is_postgres_reachable(url: str) -> bool:
    """轻量探测：能否连上目标实例的维护库。"""
    try:
        engine = admin_engine(url)
    except Exception:  # noqa: BLE001 - 连接串本身非法
        return False
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 - 实例不可达
        return False
    finally:
        engine.dispose()


@contextmanager
def alembic_database_url(url: str) -> Iterator[None]:
    """临时把 ``DATABASE_URL`` 指向测试库，供 ``migrations/env.py`` 读取。

    ``migrations/env.py`` 刻意从应用配置读取连接串（避免凭据入库），
    因此进程内执行迁移时必须通过环境变量切换目标库。
    """
    from app.core.config import get_settings

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()


def upgrade_head(url: str) -> None:
    """以子进程执行 ``alembic upgrade head``。

    走子进程而不是 Alembic 的 Python API，有两个好处：
    1. 验证的就是文档里写给团队的那条命令；
    2. 迁移环境与应用配置彻底隔离，不会污染测试进程的 ``get_settings`` 缓存。
    """
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "alembic upgrade head 失败：\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )


def truncate_all_tables(engine: Engine) -> None:
    """清空业务表，保证用例之间互不影响（保留迁移建好的 schema）。"""
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE login_attempts, auth_sessions, users"
                " RESTART IDENTITY CASCADE"
            )
        )
