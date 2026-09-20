"""对任意 S3 兼容对象存储运行课件上传的存储侧验收（MinIO / 云端 S3）。

做三件事：

1. 读取存储端点配置（命令行参数 > ``TEST_S3_*`` > ``STORAGE_*`` 环境变量）；
2. 探测端点并确保测试桶存在（不存在则创建）；
3. 运行 ``tests/integration/test_storage_minio.py``，输出 PASS/FAIL。

验收内容：正确请求可 PUT；内容与签名哈希不符被拒绝；重复 PUT 被拒绝；
HeadObject 读回大小、类型与存储侧校验值；适配器从不代传文件内容。

用法（在 ``backend`` 目录下执行）::

    # MinIO 默认凭据示例
    ..\\.venv\\Scripts\\python.exe scripts\\verify_storage.py ^
        --endpoint http://127.0.0.1:9000 --bucket edu-ai-test ^
        --access-key minioadmin --secret-key minioadmin

    # 也可以只依赖环境变量
    $env:TEST_S3_ENDPOINT="http://127.0.0.1:9000"

该脚本只在测试桶内创建/删除自己的对象，不会触碰其他桶。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.storage import S3Storage, S3StorageConfig, StorageUnavailableError  # noqa: E402

#: 验收用例
STORAGE_TEST = "tests/integration/test_storage_minio.py"


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def resolve_config(args: argparse.Namespace) -> S3StorageConfig:
    """按 命令行 > TEST_S3_* > STORAGE_* 的优先级取配置。"""
    endpoint = args.endpoint or _env("TEST_S3_ENDPOINT", "STORAGE_ENDPOINT")
    bucket = args.bucket or _env("TEST_S3_BUCKET", "STORAGE_BUCKET")
    access_key = args.access_key or _env("TEST_S3_ACCESS_KEY", "STORAGE_ACCESS_KEY")
    secret_key = args.secret_key or _env("TEST_S3_SECRET_KEY", "STORAGE_SECRET_KEY")
    return S3StorageConfig(
        endpoint=endpoint.rstrip("/"),
        bucket=bucket,
        access_key=access_key,
        secret_key=secret_key,
        region=args.region or _env("TEST_S3_REGION", "STORAGE_REGION") or "us-east-1",
        path_style=not args.virtual_host,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="S3 兼容对象存储验收")
    parser.add_argument("--endpoint", default="", help="例如 http://127.0.0.1:9000")
    parser.add_argument("--bucket", default="", help="专用测试桶，脚本会按需创建")
    parser.add_argument("--access-key", default="")
    parser.add_argument("--secret-key", default="")
    parser.add_argument("--region", default="")
    parser.add_argument(
        "--virtual-host",
        action="store_true",
        help="使用 virtual-host 寻址（默认 path-style，MinIO 用默认值即可）",
    )
    parser.add_argument("--skip-bucket-check", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    config = resolve_config(args)
    problem = config.problem()
    if problem:
        print("[FAIL] 存储配置不完整：")
        print(f"       {problem}")
        print("       可用命令行参数或 TEST_S3_* / STORAGE_* 环境变量提供")
        return 2

    storage = S3Storage(config)
    try:
        print(f"[1/3] 探测 {config.endpoint} 并准备测试桶 {config.bucket} ...")
        if not args.skip_bucket_check:
            try:
                created = storage.ensure_bucket()
            except StorageUnavailableError as exc:
                print(f"[FAIL] 对象存储不可用（reason={exc.reason}）：{exc}")
                return 3
            print(f"      {'已创建测试桶' if created else '测试桶已存在'}")
    finally:
        storage.close()

    print(f"[2/3] 运行存储验收用例 {STORAGE_TEST} ...")
    child_env = {
        **os.environ,
        "TEST_S3_ENDPOINT": config.endpoint,
        "TEST_S3_BUCKET": config.bucket,
        "TEST_S3_ACCESS_KEY": config.access_key,
        "TEST_S3_SECRET_KEY": config.secret_key,
        "TEST_S3_REGION": config.region,
        "TEST_S3_PATH_STYLE": "false" if args.virtual_host else "true",
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            STORAGE_TEST,
            "-q",
            "-rs",  # 跳过项必须带原因，避免"静默通过"
            "-p",
            "no:cacheprovider",
        ],
        cwd=BACKEND_DIR,
        env=child_env,
        check=False,
    )

    if completed.returncode != 0:
        print(f"[FAIL] 存储验收未通过（pytest 退出码 {completed.returncode}）")
        return completed.returncode

    print("[3/3] 存储侧验收通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
