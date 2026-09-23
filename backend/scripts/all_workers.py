"""Run all durable background queues from one low-overhead process."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings
from app.db import registry  # noqa: F401
from app.db.session import dispose_engines, get_session_factory
from app.modules.agent import worker as agent_worker
from app.modules.grading import worker as grading_worker
from app.modules.materials import worker as materials_worker
from app.modules.practice import worker as practice_worker
from app.storage import S3Storage, S3StorageConfig

logger = logging.getLogger("app.workers")
POLL_SECONDS = 3


async def _main() -> int:
    settings = get_settings()
    if not settings.database_url.strip():
        logger.error("DATABASE_URL 未配置")
        return 2

    storage_config = S3StorageConfig.from_settings(settings)
    storage_problem = storage_config.problem()
    if storage_problem:
        logger.error("对象存储配置不完整：%s", storage_problem)
        return 2

    storage = S3Storage(storage_config)
    session_factory = get_session_factory(settings)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopping.set)

    queues = (
        ("材料解析", materials_worker.run_pending_batch, {"storage": storage}),
        ("练习生成", practice_worker.run_pending_batch, {}),
        ("报告批改", grading_worker.run_pending_batch, {"storage": storage}),
        ("Agent", agent_worker.run_pending_batch, {}),
    )
    logger.info("后台 Worker 启动，轮询间隔 %s 秒", POLL_SECONDS)
    try:
        while not stopping.is_set():
            processed = 0
            for name, run_batch, extra in queues:
                if stopping.is_set():
                    break
                try:
                    processed += await run_batch(
                        session_factory,
                        settings=settings,
                        max_jobs=1,
                        stop_requested=stopping.is_set,
                        **extra,
                    )
                except Exception:
                    logger.exception("%s 队列执行异常", name)
            if processed == 0 and not stopping.is_set():
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=POLL_SECONDS)
                except TimeoutError:
                    pass
    finally:
        storage.close()
        await dispose_engines()
        logger.info("后台 Worker 已停止")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [-] %(name)s: %(message)s",
    )
    raise SystemExit(asyncio.run(_main()))
