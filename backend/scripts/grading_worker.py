"""独立报告批改 Worker 进程（``docs/api-contract.md`` 9.11）。

从 PostgreSQL 队列领取 ``SUBMISSION_GRADE`` 任务并批改实验报告，
**不在 API 请求进程内执行模型调用**。

用法（在 ``backend`` 目录下执行；依赖 ``DATABASE_URL``、对象存储 ``STORAGE_*``
与模型 ``AI_*`` 配置，均读 ``backend/.env``）::

    ..\\.venv\\Scripts\\python.exe scripts\\grading_worker.py

可选环境变量：

- ``GRADING_WORKER_POLL_SECONDS``：队列空时的轮询间隔（默认 5 秒）；
- ``GRADING_WORKER_BATCH_SIZE``：单批最多领取的任务数（默认 5）。

批改需要读取报告对象，因此对象存储必须配置；模型未配置时任务会以
「模型服务未配置」写入 ``FAILED``，可在配置后通过触发批改接口重试。
进程长期运行；``Ctrl+C``（SIGINT）完成当前批次后优雅退出。
退出码：``0`` 正常退出；``2`` 配置缺失。
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
from app.modules.grading import worker  # noqa: E402
from app.storage import S3Storage, S3StorageConfig  # noqa: E402

logger = logging.getLogger("app.grading.worker")

POLL_SECONDS = float(os.environ.get("GRADING_WORKER_POLL_SECONDS", "5"))
BATCH_SIZE = int(os.environ.get("GRADING_WORKER_BATCH_SIZE", "5"))


async def _main() -> int:
    settings = get_settings()
    if not settings.database_url.strip():
        print("[FAIL] DATABASE_URL 未配置：请先在 backend/.env 中填写连接串")
        return 2

    storage_config = S3StorageConfig.from_settings(settings)
    problem = storage_config.problem()
    if problem:
        print(f"[FAIL] 对象存储配置不完整：{problem}")
        print("       批改需要下载报告对象，请配置 STORAGE_ENDPOINT / STORAGE_BUCKET /")
        print("       STORAGE_ACCESS_KEY / STORAGE_SECRET_KEY。")
        return 2

    if not settings.ai_base_url.strip() or not settings.ai_model.strip():
        logger.warning(
            "未配置 AI_BASE_URL / AI_MODEL：领取到的任务会以"
            "「模型服务未配置」写入 FAILED，可在配置后重新触发批改"
        )

    storage = S3Storage(storage_config)
    session_factory = get_session_factory(settings)
    stopping = asyncio.Event()

    def _handle_signal() -> None:
        logger.info("收到停止信号，完成当前批次后退出")
        stopping.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:  # pragma: no cover - Windows 回退
            signal.signal(sig, lambda *_: _handle_signal())

    logger.info(
        "报告批改 Worker 启动（poll=%ss batch=%s lease=%ss model=%s）",
        POLL_SECONDS,
        BATCH_SIZE,
        settings.submission_grade_lease_seconds,
        settings.ai_model or "<未配置>",
    )
    try:
        while not stopping.is_set():
            try:
                processed = await worker.run_pending_batch(
                    session_factory,
                    settings=settings,
                    storage=storage,
                    max_jobs=BATCH_SIZE,
                    stop_requested=stopping.is_set,
                )
            except Exception:  # noqa: BLE001 - 批次异常不终止进程
                logger.exception("批次处理异常，稍后重试")
                processed = 0
            if processed == 0 and not stopping.is_set():
                await asyncio.sleep(POLL_SECONDS)
    finally:
        storage.close()
        logger.info("报告批改 Worker 退出")
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [-] %(name)s: %(message)s",
    )
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
