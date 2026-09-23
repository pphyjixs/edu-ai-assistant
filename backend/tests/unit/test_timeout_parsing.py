"""``TEST_TIMEOUT_SECONDS`` 解析函数的纯函数回归（修复计划步骤三）。

非法值必须抛出包含变量名的 :class:`pytest.UsageError`（pytest 以配置错误
退出），而不是把原始字符串塞进 pytest-timeout 造成 ``INTERNALERROR``。
"""

from __future__ import annotations

import pytest

from tests.conftest import _TIMEOUT_ENV_VAR, _parse_test_timeout_seconds


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", 1.0), ("0.5", 0.5), ("120", 120.0), (" 30 ", 30.0)],
)
def test_parses_valid_seconds(raw: str, expected: float) -> None:
    assert _parse_test_timeout_seconds(raw) == expected


def test_unset_env_var_returns_none() -> None:
    """环境变量不存在：未配置，返回 None。"""
    assert _parse_test_timeout_seconds(None) is None


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "abc", "0", "-1", "nan", "inf", "-inf", "1.0.0"],
)
def test_rejects_invalid_values(raw: str) -> None:
    """非法值：UsageError 且错误信息包含变量名与原始值。"""
    with pytest.raises(pytest.UsageError) as excinfo:
        _parse_test_timeout_seconds(raw)
    message = str(excinfo.value)
    assert _TIMEOUT_ENV_VAR in message
    assert raw.strip() in message or raw in message
