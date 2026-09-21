"""维护脚本必须能**独立**加载完整 ORM 元数据（子进程验证）。

背景：``scripts/parse_worker.py`` 只导入 materials 相关模型时，
``materials.course_id`` 的外键目标表 ``courses`` 尚未注册，脚本启动即
``NoReferencedTableError``；而在测试进程里因为已导入全量模型，这个缺陷
不会被发现。因此本文件在**子进程**中导入脚本并解析映射，确保每个维护
脚本都能单独启动（脚本内需导入 :mod:`app.db.registry`）。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from app.core.config import BACKEND_DIR

#: 面向运维的全部独立入口脚本
MAINTENANCE_SCRIPTS = (
    "scripts/parse_worker.py",
    "scripts/cleanup_deleted_materials.py",
    "scripts/backfill_material_chunks.py",
)


@pytest.mark.parametrize("script", MAINTENANCE_SCRIPTS)
def test_maintenance_script_loads_full_metadata(script: str) -> None:
    """导入脚本（不执行 main）后解析映射，不应出现外键目标缺失。"""
    code = (
        "import runpy, sqlalchemy.orm as orm;"
        f"runpy.run_path({script!r}, run_name='__not_main__');"
        "orm.configure_mappers();"
        "print('mappers-ok')"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND_DIR),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"{script} 无法独立加载元数据：\n{result.stderr}"
    assert "mappers-ok" in result.stdout
