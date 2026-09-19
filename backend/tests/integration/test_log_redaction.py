"""日志脱敏验收测试（真实请求链路）。

对应 ``docs/acceptance.md`` 第 10 节与 ``docs/deployment-vercel.md`` 第 7 节：
日志包含 request ID，不包含密码、令牌或完整敏感正文。

捕获用的 handler 挂的是 **应用真实的格式化器**（``build_formatter()``），
因此断言的是生产环境实际会输出的内容，而不是某个测试专用的格式。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from app.core.logging import build_formatter

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"
ME_URL = "/api/v1/users/me"

PASSWORD = "Super Secret Pass 2026!"
EMAIL = "logredaction@example.com"


class CapturingHandler(logging.Handler):
    """按应用的真实格式化器记录最终输出。"""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(build_formatter())
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(self.format(record))


@pytest.fixture
def captured_logs() -> Iterator[list[str]]:
    handler = CapturingHandler()
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield handler.messages
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def test_request_flow_never_writes_secrets_to_logs(api_client, captured_logs) -> None:
    """完整的注册 / 登录 / 取资料链路上，日志里不出现密码与令牌。"""
    register = api_client.post(
        REGISTER_URL,
        json={
            "email": EMAIL,
            "password": PASSWORD,
            "display_name": "日志测试",
            "role": "STUDENT",
        },
    )
    assert register.status_code == 201
    user_id = register.json()["id"]

    login = api_client.post(LOGIN_URL, json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200
    tokens = login.json()

    me = api_client.get(
        ME_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert me.status_code == 200

    logs = "\n".join(captured_logs)

    assert PASSWORD not in logs, "密码不得进入日志"
    assert tokens["access_token"] not in logs, "Access Token 不得进入日志"
    assert tokens["refresh_token"] not in logs, "Refresh Token 不得进入日志"
    # 密码哈希同样不能出现
    assert "$argon2id$" not in logs

    # 访问日志保留可排查的信息：接口、状态码、request ID，认证后还有用户 ID
    assert "POST /api/v1/auth/register -> 201" in logs
    assert f"GET /api/v1/users/me -> 200" in logs
    assert f"user={user_id}" in logs


def test_formatter_masks_secrets_even_if_someone_logs_them(captured_logs) -> None:
    """即使某个模块直接把敏感值拼进日志，格式化出口也会脱敏。"""
    logger = logging.getLogger("app.test.redaction")
    logger.info(
        "调试信息 authorization=Bearer %s password=%s refresh_token=%s",
        "abc123def456ghi789",
        PASSWORD,
        "rt-abcdef123456",
    )
    logger.info(
        "连接串 postgresql+asyncpg://demo:s3cr3t-pwd@127.0.0.1:5432/edu_ai 不可达"
    )

    logs = "\n".join(captured_logs)

    for leaked in ("abc123def456ghi789", PASSWORD, "rt-abcdef123456", "s3cr3t-pwd"):
        assert leaked not in logs, f"{leaked} 应被脱敏"
    # 非敏感上下文保留，便于定位问题
    assert "127.0.0.1:5432" in logs


def test_request_id_is_present_on_every_access_log_line(api_client, captured_logs) -> None:
    api_client.get("/health/live", headers={"X-Request-ID": "redaction-trace-9"})

    access_lines = [line for line in captured_logs if "app.access" in line]

    assert access_lines, "应当有访问日志"
    assert any("redaction-trace-9" in line for line in access_lines)


def test_long_messages_are_truncated(captured_logs) -> None:
    """避免把完整报告正文之类的大段内容写进日志。"""
    logging.getLogger("app.test.long").info("body=%s", "x" * 5000)

    logs = "\n".join(captured_logs)

    assert "[truncated]" in logs
