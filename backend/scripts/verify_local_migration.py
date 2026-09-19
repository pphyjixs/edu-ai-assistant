"""在本地 PostgreSQL 上验证「全新数据库迁移 + 回滚」。

流程（每一步都会打印结果，任一步失败即中止并返回非零码）：

1. 从 ``DATABASE_URL`` 里取出连接信息；
2. 创建（或重建）一个一次性数据库 ``<原库名>_migration_check``；
3. 把 ``DATABASE_URL`` / ``TEST_DATABASE_URL`` 指向它，执行
   ``tests/integration/test_migrations.py``
   —— 该测试会跑 ``upgrade head`` → 比对模型 → ``downgrade base`` → 再 ``upgrade head``；
4. 删除一次性数据库。

用法（在 ``backend`` 目录下执行）::

    ..\\.venv\\Scripts\\python.exe scripts\\verify_local_migration.py

需要在 ``backend/.env`` 中配置好可用的 ``DATABASE_URL``（该账号需有
``CREATE DATABASE`` 权限）。删除临时库前脚本不会触碰原库的任何数据。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import normalize_database_url  # noqa: E402

#: 一次性校验库的后缀
SCRATCH_SUFFIX = "_migration_check"

#: 新建数据库时连接的维护库
MAINTENANCE_DATABASE = "postgres"

#: 需要执行的测试模块
MIGRATION_TEST = "tests/integration/test_migrations.py"


def _sync_url(url: str) -> str:
    """换成 psycopg2 同步驱动，用于 CREATE/DROP DATABASE。"""
    return url.replace("postgresql+asyncpg", "postgresql+psycopg2")


def _with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, ""))


def main() -> int:
    # 子进程（pytest）直接写控制台，这里改成行缓冲，保证输出顺序与真实执行顺序一致
    sys.stdout.reconfigure(line_buffering=True)

    settings = get_settings()
    if not settings.database_url.strip():
        print("[FAIL] DATABASE_URL 未配置：请先在 backend/.env 中填写连接串")
        return 2

    raw = normalize_database_url(settings.database_url).url
    sync_url = _sync_url(raw)
    parsed = make_url(sync_url)
    if not parsed.database:
        print("[FAIL] DATABASE_URL 缺少数据库名")
        return 2
    scratch_db = f"{parsed.database}{SCRATCH_SUFFIX}"

    admin_engine = create_engine(
        _with_database(sync_url, MAINTENANCE_DATABASE),
        isolation_level="AUTOCOMMIT",
    )

    try:
        with admin_engine.connect() as connection:
            print(f"[1/4] 重建一次性数据库 {scratch_db} ...")
            # 结束残留连接后重建，保证是"全新数据库"
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": scratch_db},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))
            connection.execute(text(f'CREATE DATABASE "{scratch_db}"'))
            version = connection.execute(text("SHOW server_version")).scalar()
        print(f"      服务器版本 {version}")

        scratch_url = _with_database(sync_url, scratch_db)
        async_scratch_url = scratch_url.replace(
            "postgresql+psycopg2", "postgresql+asyncpg"
        )
        print(f"[2/4] 在 {scratch_db} 上执行迁移 / 比对模型 / 回滚 ...")
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", MIGRATION_TEST, "-q", "-p", "no:cacheprovider"],
            cwd=BACKEND_DIR,
            env={
                **os.environ,
                "DATABASE_URL": async_scratch_url,
                "TEST_DATABASE_URL": async_scratch_url,
            },
            check=False,
        )

        if completed.returncode != 0:
            print(f"[FAIL] 迁移校验未通过（pytest 退出码 {completed.returncode}）")
            print(f"       临时库 {scratch_db} 已保留，便于排查；确认后手动删除即可")
            return completed.returncode

        print("[3/4] 清理一次性数据库 ...")
        with admin_engine.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": scratch_db},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{scratch_db}"'))

        print("[4/4] 本地迁移与回滚验证通过")
        return 0
    finally:
        admin_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
