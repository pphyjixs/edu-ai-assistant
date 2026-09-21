"""独立解析 Worker 进程（契约 5.5）。

从 PostgreSQL 队列领取 ``MATERIAL_PARSE`` 任务并执行解析，**不依赖
Vercel 请求进程或任何内存队列**：API 进程只创建 ``PENDING`` 任务，
本进程负责领取、执行与状态发布。

崩溃防护（详见 :mod:`app.modules.materials.worker`）：

- 领取时设置租约（``MATERIAL_PARSE_LEASE_SECONDS``，默认 300 秒）；
- 执行期间按租约的 1/3 心跳续租；
- 回写必须携带领取时生成的运行令牌，资料已删除或任务被重试重置后放弃。

用法（在 ``backend`` 目录下执行；依赖 ``DATABASE_URL``、对象存储
``STORAGE_*`` 与模型 ``AI_*`` 配置，均读 ``backend/.env``）::

    ..\\.venv\\Scripts\\python.exe scripts\\parse_worker.py

可选环境变量：

- ``WORKER_POLL_SECONDS``：队列空时的轮询间隔（默认 5 秒）；
- ``WORKER_BATCH_SIZE``：单批最多领取的任务数（默认 5）。

进程长期运行；``Ctrl+C``（SIGINT）优雅退出——完成当前批次后停止。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db import registry  # noqa: E402,F401 - 导入全部 ORM 模型，否则外键目标缺失
from app.db.session import get_session_factory  # noqa: E402
from app.modules.materials import worker  # noqa: E402
from app.storage.s3 import S3Storage, S3StorageConfig  # noqa: E402

logger = logging.getLogger("app.materials.worker")

#: 队列空时的轮询间隔（秒）
POLL_SECONDS = float(os.environ.get("WORKER_POLL_SECONDS", "5"))
#: 单批最多领取的任务数
BATCH_SIZE = int(os.environ.get("WORKER_BATCH_SIZE", "5"))


async def _main() -> int:
    settings = get_settings()

    if not settings.database_url.strip():
        print("[FAIL] DATABASE_URL 未配置：请先在 backend/.env 中填写连接串")
        return 2

    config = S3StorageConfig.from_settings(settings)
    problem = config.problem()
    if problem:
        print(f"[FAIL] 对象存储配置不完整：{problem}")
        print("       Worker 需要 STORAGE_ENDPOINT / STORAGE_BUCKET /")
        print("       STORAGE_ACCESS_KEY / STORAGE_SECRET_KEY 才能读取课件对象。")
        return 2

    session_factory = get_session_factory(settings)
    storage = S3Storage(config)

    if not settings.ai_base_url.strip() or not settings.ai_model.strip():
        print(
            "[WARN] AI_BASE_URL / AI_MODEL 未配置：领取到的任务将直接进入 "
            "FAILED（解析服务未配置模型端点）。"
        )

    stopping = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("收到退出信号，完成当前批次后停止")
        stopping.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:  # pragma: no cover - Windows 事件循环
            signal.signal(sig, lambda *_: stopping.set())

    logger.info(
        "解析 Worker 启动（poll=%.0fs batch=%d lease=%ds model=%s）",
        POLL_SECONDS,
        BATCH_SIZE,
        settings.material_parse_lease_seconds,
        settings.ai_model or "<未配置>",
    )

    try:
        while not stopping.is_set():
            try:
                processed = await worker.run_pending_batch(
                    session_factory,
                    storage=storage,
                    settings=settings,
                    max_jobs=BATCH_SIZE,
                    stop_requested=stopping.is_set,
                )
            except Exception:  # noqa: BLE001 - 主循环永不退出
                logger.exception("批次执行异常，将在下轮重试")
                processed = 0

            if processed == 0 and not stopping.is_set():
                await asyncio.sleep(POLL_SECONDS)
    finally:
        storage.close()
        logger.info("解析 Worker 已停止")
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
