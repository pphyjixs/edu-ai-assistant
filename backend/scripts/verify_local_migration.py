"""在本地 PostgreSQL 上验证「全新数据库迁移 + 回滚」。

流程（每一步都会打印结果，任一步失败即中止并返回非零码）：

1. 从 ``DATABASE_URL`` 里取出库名，派生两个**专用库**：
   ``<原库名>_test``（pytest 夹具建立与回收）与 ``<原库名>_migration_check``
   （迁移用例会清空整个 schema，单独用一个库）。
   库名规则由 ``tests/pg_support.py`` 的守卫强制：只允许这两种后缀，
   因此脚本不能自己拿一个 ``_migration_check`` 库直接当测试库用。
2. 删除残留的迁移校验库，保证是「全新数据库」。
3. 以 ``TEST_DATABASE_URL=<原库名>_test`` 运行
   ``tests/integration/test_migrations.py``
   —— 该测试会在迁移校验库上跑 ``upgrade head`` → 比对模型 →
   ``downgrade base`` → 再 ``upgrade head``。
4. 删除遗留的一次性库。

用法（在 ``backend`` 目录下执行）::

    ..\\.venv\\Scripts\\python.exe scripts\\verify_local_migration.py

需要在 ``backend/.env`` 中配置好可用的 ``DATABASE_URL``（该账号需有
``CREATE DATABASE`` 权限）。脚本只创建与删除上述两个专用库，
不触碰原库的任何数据。
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
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import normalize_database_url  # noqa: E402

#: pytest 夹具使用的测试库后缀（tests/pg_support.TEST_SUFFIX）
TEST_SUFFIX = "_test"

#: 迁移专项校验库后缀（tests/pg_support.MIGRATION_SUFFIX）
MIGRATION_SUFFIX = "_migration_check"

#: 新建数据库时连接的维护库
MAINTENANCE_DATABASE = "postgres"

#: 需要执行的测试模块
MIGRATION_TEST = "tests/integration/test_migrations.py"


def _with_database(url: str, database: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, ""))


def _drop_database(connection, name: str) -> None:
    connection.execute(
        text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = :name AND pid <> pg_backend_pid()"
        ),
        {"name": name},
    )
    connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))


def main() -> int:
    # 子进程（pytest）直接写控制台，这里改成行缓冲，保证输出顺序与真实执行顺序一致
    sys.stdout.reconfigure(line_buffering=True)

    settings = get_settings()
    if not settings.database_url.strip():
        print("[FAIL] DATABASE_URL 未配置：请先在 backend/.env 中填写连接串")
        return 2

    raw = normalize_database_url(settings.database_url).url
    sync_url = raw.replace("postgresql+asyncpg", "postgresql+psycopg2")
    parsed = make_url(sync_url)
    if not parsed.database:
        print("[FAIL] DATABASE_URL 缺少数据库名")
        return 2

    base = parsed.database
    test_db = f"{base}{TEST_SUFFIX}"
    migration_db = f"{base}{MIGRATION_SUFFIX}"
    async_test_url = _with_database(raw, test_db)

    admin_engine = create_engine(
        _with_database(sync_url, MAINTENANCE_DATABASE),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )

    try:
        with admin_engine.connect() as connection:
            print(f"[1/4] 清理残留的一次性库 {migration_db} / {test_db} ...")
            _drop_database(connection, migration_db)
            _drop_database(connection, test_db)
            version = connection.execute(text("SHOW server_version")).scalar()
        print(f"      服务器版本 {version}")

        print(f"[2/4] 以 {test_db} 运行迁移 / 比对模型 / 回滚 ...")
        # DATABASE_URL 必须留空：夹具会用源库名做冲突检查，
        # 若把它也指向测试库会触发"测试库名不能与源库相同"。
        child_env = {**os.environ, "TEST_DATABASE_URL": async_test_url}
        child_env.pop("DATABASE_URL", None)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                MIGRATION_TEST,
                "-q",
                "-p",
                "no:cacheprovider",
            ],
            cwd=BACKEND_DIR,
            env=child_env,
            check=False,
        )

        if completed.returncode != 0:
            print(f"[FAIL] 迁移校验未通过（pytest 退出码 {completed.returncode}）")
            print(f"       临时库 {migration_db} 已保留，便于排查；确认后手动删除即可")
            return completed.returncode

        print("[3/4] 清理一次性数据库 ...")
        with admin_engine.connect() as connection:
            _drop_database(connection, migration_db)
            _drop_database(connection, test_db)

        print("[4/4] 本地迁移与回滚验证通过")
        return 0
    finally:
        admin_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
