"""创建本地开发数据库（幂等）。

用法（在 ``backend`` 目录下执行）::

    ..\\.venv\\Scripts\\python.exe scripts\\create_local_database.py

读取 ``backend/.env`` 的 ``DATABASE_URL``，取其中的数据库名在本地 PostgreSQL 上创建。
已存在时直接跳过。相比 ``psql -c "CREATE DATABASE ..."``，它不需要把 PostgreSQL 的
``bin`` 目录加入 PATH，也保证库名与 ``DATABASE_URL`` 永远一致。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import normalize_database_url  # noqa: E402

#: 连接维护库来执行 CREATE DATABASE
MAINTENANCE_DATABASE = "postgres"

#: 库名白名单：CREATE DATABASE 不能用绑定参数，只能拼标识符，必须校验
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")


def main() -> int:
    settings = get_settings()
    if not settings.database_url.strip():
        print("[FAIL] DATABASE_URL 未配置：请先在 backend/.env 中填写连接串")
        return 2

    sync_url = make_url(normalize_database_url(settings.database_url).url).set(
        drivername="postgresql+psycopg2"
    )
    target = sync_url.database or ""
    if not SAFE_IDENTIFIER.match(target):
        print(f"[FAIL] 数据库名不合法: {target!r}")
        return 2

    admin_engine = create_engine(
        sync_url.set(database=MAINTENANCE_DATABASE), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin_engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": target},
            ).scalar()
            if exists:
                print(f"[SKIP] 数据库 {target} 已存在")
                return 0
            connection.execute(text(f'CREATE DATABASE "{target}"'))
        print(f"[OK] 已创建数据库 {target}")
        print("     下一步：..\\.venv\\Scripts\\python.exe -m alembic upgrade head")
        return 0
    finally:
        admin_engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
