"""导出 OpenAPI 契约文件，交给前端生成 TypeScript 类型。

用法（在 ``backend`` 目录下执行）::

    ..\\.venv\\Scripts\\python.exe scripts\\export_openapi.py

输出：仓库根目录的 ``contracts/openapi/openapi.json``
（目录约定见 ``docs/architecture.md`` 第 3 节）。

该脚本 **不需要数据库连接**：``create_app`` 只读取环境变量，不会访问外部服务，
因此可以在没有 PostgreSQL 的机器上导出契约。

修改接口后请重新导出并随 PR 一起提交，前端据此生成
``contracts/generated`` 下的类型（生成命令见 ``docs/collaboration.md`` 第 4 节）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.main import app  # noqa: E402

#: 输出位置，与 docs/architecture.md 的目录约定一致
OUTPUT_PATH = REPO_DIR / "contracts" / "openapi" / "openapi.json"


def main() -> int:
    schema = app.openapi()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    paths = schema.get("paths", {})
    info = schema.get("info", {})
    print(f"OpenAPI 已导出: {OUTPUT_PATH}")
    print(f"title={info.get('title')} version={info.get('version')} paths={len(paths)}")
    for path in sorted(paths):
        methods = ",".join(sorted(method.upper() for method in paths[path]))
        print(f"  {methods:18} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
