"""日志配置与脱敏。

设计要点（对应 docs/deployment-vercel.md 第 7 节）：

- 每条日志都带 request ID，便于用一次请求串起解析、批改等异步链路。
- 统一在 **格式化出口** 脱敏：任何 logger 的输出都会经过
  :class:`RedactingFormatter`，避免个别模块忘记使用脱敏工具函数。
- 脱敏覆盖常见泄露形态：URL 内嵌密码、``Bearer`` 令牌、``password=`` /
  ``token`` / ``api_key`` 等键值、JWT、云厂商 Access Key。
- 单条日志截断，防止把完整报告正文或大段模型输出写进日志。
- 关闭 uvicorn 自带的访问日志，改用带 request ID 的访问日志中间件。
"""

from __future__ import annotations

import logging
import re

from app.core.request_context import get_request_id

#: 单条日志的最大字符数，超出部分截断
MAX_LOG_MESSAGE_LENGTH = 2000

_TRUNCATED_SUFFIX = "...[truncated]"

#: 日志格式：时间 级别 [request_id] logger 消息
LOG_FORMAT = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_MASK = "***"

#: 敏感键名，匹配 ``password=`` / ``"api_key":`` / ``token:`` 等写法
_SENSITIVE_KEYS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "api[_-]?key",
    "apikey",
    "access[_-]?key",
    "secret[_-]?key",
    "authorization",
    "credential",
    "signature",
)

_REDACTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # 连接串中的密码：postgresql+asyncpg://user:secret@host → user:***@host
    (re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://[^:/@\s]+):([^@\s]+)@"), rf"\1:{_MASK}@"),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/=]+"), rf"\1 {_MASK}"),
    # key=value / "key": "value" / key: value
    (
        re.compile(
            r"(?i)((?:"
            + "|".join(_SENSITIVE_KEYS)
            + r')s?["\']?\s*[:=]\s*)(["\']?)([^\s,;"\'}\]]+)'
        ),
        rf"\1\2{_MASK}",
    ),
    # JWT（Header.Payload.Signature）
    (
        re.compile(r"\beyJ[A-Za-z0-9\-_]{8,}\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\b"),
        _MASK,
    ),
    # 云厂商 Access Key ID
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), _MASK),
    # OpenAI 风格密钥
    (re.compile(r"\b(?:sk|pk)-[A-Za-z0-9\-_]{16,}\b"), _MASK),
)

_configured = False


def redact(text: str) -> str:
    """对文本执行脱敏，供日志之外的场景（如错误详情）复用。"""
    for pattern, replacement in _REDACTION_RULES:
        text = pattern.sub(replacement, text)
    return text


def truncate(text: str, limit: int = MAX_LOG_MESSAGE_LENGTH) -> str:
    """截断超长文本，避免日志被单条记录撑爆。"""
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATED_SUFFIX


class RedactingFormatter(logging.Formatter):
    """在格式化出口统一脱敏、截断并注入 request ID。

    脱敏放在格式化出口而非各调用点，是为了兜住所有 logger——包括没使用
    ``extra={"request_id": ...}`` 的三方库日志。
    """

    def format(self, record: logging.LogRecord) -> str:
        if not getattr(record, "request_id", None):
            record.request_id = get_request_id() or "-"
        return truncate(redact(super().format(record)))


def build_formatter() -> RedactingFormatter:
    """构造带脱敏能力的标准格式化器。"""
    return RedactingFormatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)


def setup_logging(level: str = "INFO", *, force: bool = False) -> None:
    """配置根 logger。

    幂等：重复调用不会叠加 handler，除非显式传入 ``force=True``。
    """
    global _configured

    root = logging.getLogger()
    if _configured and not force:
        root.setLevel(_resolve_level(level))
        return

    handler = logging.StreamHandler()
    handler.setFormatter(build_formatter())

    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(_resolve_level(level))

    # 访问日志由 RequestContextMiddleware 输出，包含 request ID 与耗时；
    # uvicorn 默认访问日志缺少请求标识且会打印完整 query string，故关闭。
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers.clear()
    access_logger.disabled = True
    access_logger.propagate = False

    _configured = True


def _resolve_level(level: str) -> int:
    """把配置中的日志级别字符串转换为 logging 常量，非法值回退 INFO。"""
    resolved = logging.getLevelName(level.strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO
