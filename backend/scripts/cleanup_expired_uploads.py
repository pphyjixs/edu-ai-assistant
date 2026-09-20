"""清理超过确认窗口仍未完成的上传会话（契约 4.6 的过期清理）。

对应 ``docs/api-contract.md`` 第 4.6 节：

- 定期锁定超过确认窗口（默认 24 小时，``MATERIAL_UPLOAD_CONFIRM_TTL_SECONDS``）
  仍未确认的上传会话，删除其孤立对象并标记 ``expired_at``；
- 已完成的会话（``completed_material_id`` 非空）及其资料、对象**绝不删除**；
- 命令可重复执行（幂等）：已标记 ``expired_at`` 的会话不再扫描，删除对象天然幂等；
- 用 ``FOR UPDATE SKIP LOCKED`` 锁定会话行，与并发的完成请求互斥，
  不会误删「刚确认完成」的对象。

用法（在 ``backend`` 目录下执行）::

    ..\\.venv\\Scripts\\python.exe scripts\\cleanup_expired_uploads.py

依赖：``DATABASE_URL`` 与对象存储 ``STORAGE_*`` 配置（读 ``backend/.env``）。
存储未配置或不可达时，删除对象失败会被跳过并记日志，不会误标过期。

退出码：0 成功（含无需清理）；2 配置缺失；3 数据库不可达。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402
from app.modules.materials import service as materials_service  # noqa: E402
from app.storage.s3 import S3Storage, S3StorageConfig  # noqa: E402


async def _run() -> int:
    settings = get_settings()

    if not settings.database_url.strip():
        print("[FAIL] DATABASE_URL 未配置：请先在 backend/.env 中填写连接串")
        return 2

    config = S3StorageConfig.from_settings(settings)
    problem = config.problem()
    if problem:
        print(f"[FAIL] 对象存储配置不完整：{problem}")
        print("       清理需要 STORAGE_ENDPOINT / STORAGE_BUCKET / STORAGE_ACCESS_KEY /")
        print("       STORAGE_SECRET_KEY，否则无法删除过期上传的孤立对象。")
        return 2

    storage = S3Storage(config)
    try:
        factory = get_session_factory(settings)
        async with factory() as session:
            try:
                cleaned = await materials_service.cleanup_expired_uploads(
                    session, storage=storage
                )
            except Exception as exc:  # noqa: BLE001 - 数据库不可达等，统一转退出码
                print(f"[FAIL] 清理失败：{type(exc).__name__}: {exc}")
                return 3

        print(f"[OK] 本次标记过期的上传会话：{cleaned} 条")
        if cleaned == 0:
            print("     没有需要清理的过期未完成会话（或已在之前的运行中处理）。")
        return 0
    finally:
        storage.close()


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
