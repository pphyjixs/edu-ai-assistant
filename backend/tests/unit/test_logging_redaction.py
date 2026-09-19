"""日志脱敏与截断测试。

对应 ``docs/acceptance.md`` 第 10 节：日志不得包含密码、令牌或完整报告正文。
"""

from __future__ import annotations

import logging

import pytest

from app.core.logging import (
    LOG_FORMAT,
    MAX_LOG_MESSAGE_LENGTH,
    RedactingFormatter,
    redact,
    truncate,
)

LEAKY_SAMPLES = [
    # 连接串内嵌密码
    (
        "连接失败 postgresql+asyncpg://tester:s3cr3t-pwd@db.example.com:5432/edu_ai",
        "s3cr3t-pwd",
    ),
    # Bearer 令牌
    ("Authorization: Bearer abc123def456ghi789", "abc123def456ghi789"),
    # 键值形态的密码
    ('登录请求 password="Hunter2Hunter2"', "Hunter2Hunter2"),
    # JSON 中的刷新令牌
    ('{"refresh_token": "rt-abcdef123456"}', "rt-abcdef123456"),
    # 供应商密钥
    ("AI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz", "sk-abcdefghijklmnopqrstuvwxyz"),
    # 云厂商 Access Key
    ("credentials AKIAIOSFODNN7EXAMPLE loaded", "AKIAIOSFODNN7EXAMPLE"),
    # JWT
    (
        "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signaturePart",
        "eyJzdWIiOiIxIn0",
    ),
]


@pytest.mark.parametrize(("raw", "secret"), LEAKY_SAMPLES)
def test_redact_masks_sensitive_values(raw: str, secret: str) -> None:
    assert secret not in redact(raw)


def test_redact_keeps_ordinary_text_unchanged() -> None:
    text = "用户 张老师 完成课程 软件工程实验 的资料解析，course_id=8f2a1b"

    assert redact(text) == text


def test_truncate_limits_single_log_entry() -> None:
    result = truncate("x" * (MAX_LOG_MESSAGE_LENGTH + 100))

    assert len(result) < MAX_LOG_MESSAGE_LENGTH + 100
    assert result.endswith("[truncated]")


def test_formatter_redacts_after_interpolation_and_injects_request_id() -> None:
    """格式化出口统一脱敏：即使调用方直接拼接敏感值也不会泄露。"""
    formatter = RedactingFormatter(fmt=LOG_FORMAT)
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="login email=%s password=%s",
        args=("teacher@example.com", "Hunter2Hunter2"),
        exc_info=None,
    )

    output = formatter.format(record)

    assert "Hunter2Hunter2" not in output
    assert "teacher@example.com" in output, "非敏感字段应保留，便于排查"
    assert "app.test" in output
    assert "[-]" in output, "非请求链路日志的 request_id 记为 -"
