"""回填已有 READY 资料的检索片段（``docs/api-contract.md`` 6.1 的运维命令）。

问答只检索 ``material_chunks`` 中的原文片段，而片段是解析 Worker 引入后
才落库的；早于该功能解析完成的 ``READY`` 资料没有片段。本命令为它们
补建片段索引，**开放问答前必须执行一次**。

行为（详见 :mod:`app.modules.materials.backfill`）：

1. 选取状态 ``READY``、未删除、尚无片段的资料；
2. 从对象存储读取对象并复核大小与 SHA-256（与资料声明比对，不符即拒绝）；
3. 按来源顺序切分片段；
4. **提交前复查**资料仍为 ``READY`` 且未删除——回填期间被删除或状态变化的
   资料不会留下可检索片段；
5. 失败只输出安全摘要并保留资料原状态，重复运行即可重试。

幂等：默认跳过已有片段的资料，重复执行结果一致。

用法（在 ``backend`` 目录下执行）::

    ..\\.venv\\Scripts\\python.exe scripts\\backfill_material_chunks.py
    ..\\.venv\\Scripts\\python.exe scripts\\backfill_material_chunks.py --force
    ..\\.venv\\Scripts\\python.exe scripts\\backfill_material_chunks.py --limit 500

依赖 ``DATABASE_URL`` 与对象存储 ``STORAGE_*`` 配置（读 ``backend/.env``）。

退出码：0 成功（含无需回填与部分失败）；2 配置缺失；3 数据库不可达。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import get_settings  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402
from app.modules.materials import backfill  # noqa: E402
from app.storage.s3 import S3Storage, S3StorageConfig  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="回填 READY 资料的检索片段（开放问答前执行）"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="连已有片段的资料也重新切分（默认只补没有片段的）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=backfill.DEFAULT_BATCH_LIMIT,
        help=f"单次最多处理的资料数（默认 {backfill.DEFAULT_BATCH_LIMIT}）",
    )
    return parser.parse_args()


async def _run() -> int:
    args = _parse_args()
    settings = get_settings()

    if not settings.database_url.strip():
        print("[FAIL] DATABASE_URL 未配置：请先在 backend/.env 中填写连接串")
        return 2

    config = S3StorageConfig.from_settings(settings)
    problem = config.problem()
    if problem:
        print(f"[FAIL] 对象存储配置不完整：{problem}")
        print("       回填需要 STORAGE_ENDPOINT / STORAGE_BUCKET /")
        print("       STORAGE_ACCESS_KEY / STORAGE_SECRET_KEY 才能读取课件对象。")
        return 2

    storage = S3Storage(config)
    try:
        factory = get_session_factory(settings)
        async with factory() as session:
            try:
                report = await backfill.backfill_material_chunks(
                    session,
                    storage=storage,
                    settings=settings,
                    force=args.force,
                    limit=args.limit,
                )
            except Exception as exc:  # noqa: BLE001 - 数据库不可达等，统一转退出码
                print(f"[FAIL] 回填失败：{type(exc).__name__}: {exc}")
                return 3

        print(f"[OK] 已回填片段：{report.filled} 条资料")
        print(f"     跳过（复查未通过）：{report.skipped} 条")
        print(f"     失败：{report.failed_count} 条")
        for failure in report.failed[:20]:
            print(f"       - {failure.material_id}: {failure.reason}")
        if report.failed_count > 20:
            print("       ...（其余失败见日志）")
        if report.filled == 0 and report.failed_count == 0:
            print("     没有需要回填的资料（已有片段的资料默认跳过）。")
        return 0
    finally:
        storage.close()


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
