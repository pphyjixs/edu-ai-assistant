"""清理已删除资料的对象（契约 5.2 的删除流水线维护命令）。

删除资料并不立即删除对象：原 PUT 地址在过期前仍可能被浏览器使用
（``If-None-Match: *`` 在对象不存在时放行晚到 PUT），立即删除会留下
重建窗口。本命令处理 ``material_delete_todos`` 中「PUT 地址过期 +
缓冲期（``MATERIAL_DELETE_BUFFER_SECONDS``，默认 1 小时）」已过的待办：

1. 删除对象（对象本就不存在时跳过）；
2. 再次核查晚到 PUT：HeadObject 确认对象不再出现后标记 ``DONE``；
   仍出现（删除与核查之间有晚到 PUT 重建对象）则保持 ``PENDING``，
   由下一轮继续清理——失败持续重试，不会留下永久孤立对象；
3. 存储不可用：记录 ``last_error`` 并跳过，下一轮重试。

与 ``cleanup_expired_uploads.py``（过期上传会话清理）配合使用；
可重复执行（幂等）。建议用 cron / 定时任务按固定间隔运行。

用法（在 ``backend`` 目录下执行）::

    ..\\.venv\\Scripts\\python.exe scripts\\cleanup_deleted_materials.py

依赖：``DATABASE_URL`` 与对象存储 ``STORAGE_*`` 配置（读 ``backend/.env``）。

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
        print("       STORAGE_SECRET_KEY，否则无法删除已删除资料的对象。")
        return 2

    storage = S3Storage(config)
    try:
        factory = get_session_factory(settings)
        async with factory() as session:
            try:
                done, pending = await materials_service.cleanup_deleted_materials(
                    session, storage=storage, settings=settings
                )
            except Exception as exc:  # noqa: BLE001 - 数据库不可达等，统一转退出码
                print(f"[FAIL] 清理失败：{type(exc).__name__}: {exc}")
                return 3

        print(f"[OK] 本轮完成删除并核查通过：{done} 个对象")
        print(f"     仍待重试（晚到 PUT 或存储故障）：{pending} 个")
        if done == 0 and pending == 0:
            print("     没有待处理的对象删除记录。")
        return 0
    finally:
        storage.close()


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
