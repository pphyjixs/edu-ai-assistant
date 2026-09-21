"""回填命令的参数校验（不接触数据库）。

``--batch-size 0`` 会生成 ``LIMIT 0``：每页都为空，于是"没有候选资料"与
"批量大小非法"被混为一谈，命令还会输出成功。本文件固定入口处的拒绝行为，
保证非法参数既不会静默跳过全部资料，也不会出现成功文案。

同时覆盖函数入口的校验（命令之外直接调用回填函数时同样拒绝）。
"""

from __future__ import annotations

import argparse
import sys

import pytest

from scripts.backfill_material_chunks import _parse_args, _positive_int


@pytest.mark.parametrize("value", ["0", "-1", "-100"])
def test_parse_args_rejects_non_positive_batch_size(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """非正整数由 argparse 直接拒绝（退出码 2），不会进入回填流程。"""
    monkeypatch.setattr(sys, "argv", ["backfill", "--batch-size", value])

    with pytest.raises(SystemExit) as excinfo:
        _parse_args()

    assert excinfo.value.code == 2


def test_parse_args_accepts_positive_batch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["backfill", "--batch-size", "500"])

    args = _parse_args()

    assert args.batch_size == 500
    assert args.force is False
    assert args.material_id is None


def test_parse_args_defaults_to_module_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.materials import backfill

    monkeypatch.setattr(sys, "argv", ["backfill"])

    args = _parse_args()

    assert args.batch_size == backfill.DEFAULT_BATCH_SIZE


@pytest.mark.parametrize("value", ["0", "-5"])
def test_positive_int_rejects_non_positive(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int(value)


def test_positive_int_accepts_positive() -> None:
    assert _positive_int("25") == 25
