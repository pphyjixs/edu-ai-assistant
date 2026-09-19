"""Alembic 迁移环境。

设计要点：

- 数据库地址不写在 ``alembic.ini``，统一从应用配置读取（环境变量或
  ``backend/.env`` 的 ``DATABASE_URL``），避免凭据入库、连接串维护两份。
- ``target_metadata`` 来自 :mod:`app.db.registry`，新增业务模块时在那里登记模型。
- 打开 ``compare_type`` 与 ``compare_server_default``，让真实 PostgreSQL 上的
  迁移漂移检查更严格。
- 离线模式（``--sql``）把连接串换成同步驱动渲染 DDL，不需要 asyncpg 连库，
  便于在没装数据库的机器上审查 SQL。

常用命令（均在 ``backend`` 目录下执行）::

    ..\\.venv\\Scripts\\python.exe -m alembic upgrade head
    ..\\.venv\\Scripts\\python.exe -m alembic revision --autogenerate -m "描述"
    ..\\.venv\\Scripts\\python.exe -m alembic upgrade head --sql
"""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# 允许从任意工作目录执行 alembic：显式把 backend/ 放进 sys.path
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db.registry import target_metadata  # noqa: E402
from app.db.session import normalize_database_url  # noqa: E402

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False：alembic.ini 里的日志配置不应把应用
    # logger 关掉（集成测试在同一进程内执行迁移时会踩到）。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

#: 离线渲染 DDL 时使用的同步驱动，避免为了看 SQL 而依赖 asyncpg 连接
OFFLINE_DRIVER = "postgresql+psycopg2"

#: 迁移比较选项：类型与默认值变化都要能被检出
COMPARE_OPTIONS = {
    "compare_type": True,
    "compare_server_default": True,
}


def _database_url() -> str:
    """从应用配置读取并规范化连接串。"""
    return normalize_database_url(get_settings().database_url).url


def run_migrations_offline() -> None:
    """离线模式：只把 DDL 渲染成 SQL 输出，不连接数据库。"""
    url = _database_url().replace("postgresql+asyncpg", OFFLINE_DRIVER)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **COMPARE_OPTIONS,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        **COMPARE_OPTIONS,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线模式：用异步引擎执行迁移。"""
    # ConfigParser 会把 % 当插值符，密码里的 % 必须转义
    config.set_main_option("sqlalchemy.url", _database_url().replace("%", "%%"))

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
