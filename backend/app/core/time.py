"""时间工具。

全项目时间统一按 UTC 处理（见 ``docs/architecture.md`` 第 6 节）：

- 落库时间一律使用带时区的 UTC ``datetime``，由
  :class:`app.db.types.UtcDateTime` 保证读写两端都是 UTC。
- API 输出统一为 ISO 8601 UTC，并以 ``Z`` 结尾（例如 ``2026-09-18T08:30:00Z``），
  与 ``docs/api-contract.md`` 的示例保持一致。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import PlainSerializer


def utc_now() -> datetime:
    """返回当前 UTC 时间（带时区）。

    不使用 ``datetime.utcnow()``：它返回 naive datetime，容易与带时区的时间做
    比较时报 TypeError，且已被官方标记为废弃。
    """
    return datetime.now(timezone.utc)


def to_utc(value: datetime) -> datetime:
    """把任意 datetime 归一到 UTC。

    :raises ValueError: 传入 naive datetime 时抛出——不接受“隐含时区”，
        避免把本地时间误当 UTC 落库。
    """
    if value.tzinfo is None:
        raise ValueError("时间必须带时区；请使用 app.core.time.utc_now() 生成")
    return value.astimezone(timezone.utc)


def isoformat_z(value: datetime) -> str:
    """格式化为以 ``Z`` 结尾的 ISO 8601 UTC 字符串。"""
    return to_utc(value).isoformat().replace("+00:00", "Z")


#: 响应模型可直接使用的 UTC 时间类型，保证输出为 ``...Z``
UtcTimestamp = Annotated[datetime, PlainSerializer(isoformat_z, return_type=str)]
