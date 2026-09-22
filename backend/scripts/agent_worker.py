"""独立 Agent Run Worker 进程（``docs/local-development-agent-backend.md`` 第 6.8 节）。

从 PostgreSQL 队列领取 ``AGENT_RUN`` 任务，解析上下文后调用模型，
**绝不在 Vercel / API 请求进程内等待数分钟的模型响应**。

用法（在 ``backend`` 目录下执行；依赖 ``DATABASE_URL`` 与模型 ``AI_*`` 配置，
均读 ``backend/.env``）::

    ..\\.venv\\Scripts\\python.exe scripts\\agent_worker.py

可选环境变量：

- ``AGENT_WORKER_POLL_SECONDS``：队列空时的轮询间隔（默认 2 秒）；
- ``AGENT_WORKER_BATCH_SIZE``：单批最多领取的任务数（默认 1）。

上下文优先使用 PostgreSQL 中已解析的片段与大纲，不重新下载或解析整份文件
（文档 6.1 第 7 条）。进程长期运行；``Ctrl+C`` 完成当前批次后优雅退出。
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

from app.core.config import get_settings
from app.db import registry  # noqa: F401 - 导入全部 ORM 模型，否则外键目标缺失
from app.db.session import get_session_factory
from app.modules.agent import worker

logger = logging.getLogger("app.agent.worker")

POLL_SECONDS = float(os.environ.get("AGENT_WORKER_POLL_SECONDS", "2"))
BATCH_SIZE = int(os.environ.get("AGENT_WORKER_BATCH_SIZE", "1"))


async def _main() -> int:
    settings = get_settings()
    if not settings.database_url.strip():
        print("[FAIL] DATABASE_URL 未配置：请先在 backend/.env 中填写连接串")
        return 2
    if not settings.ai_base_url.strip() or not settings.ai_model.strip():
        logger.warning(
            "未配置 AI_BASE_URL / AI_MODEL：领取到的 Run 会以"
            "「模型服务未配置」写入 FAILED，配置后重新发起即可"
        )

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
        "Agent Worker 启动（poll=%ss batch=%s lease=%ss model_timeout=%ss model=%s）",
        POLL_SECONDS,
        BATCH_SIZE,
        settings.agent_run_lease_seconds,
        settings.agent_model_timeout_seconds,
        settings.ai_model or "<未配置>",
    )
    try:
        while not stopping.is_set():
            try:
                processed = await worker.run_pending_batch(
                    session_factory,
                    settings=settings,
                    max_jobs=BATCH_SIZE,
                    stop_requested=stopping.is_set,
                )
            except Exception:
                logger.exception("批次处理异常，稍后重试")
                processed = 0
            if processed == 0 and not stopping.is_set():
                await asyncio.sleep(POLL_SECONDS)
    finally:
        logger.info("Agent Worker 退出")
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [-] %(name)s: %(message)s",
    )
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
