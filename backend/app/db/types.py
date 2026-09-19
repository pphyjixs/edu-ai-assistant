"""自定义 SQL 类型。

:class:`UtcDateTime` 是唯一的时间戳类型，保证「所有时间按 UTC 处理」：

- PostgreSQL 使用 ``TIMESTAMP WITH TIME ZONE``（迁移脚本中对应
  ``sa.DateTime(timezone=True)``）；
- 其他方言退化为无时区 ``DATETIME``，
  但绑定前会先转换为 UTC，读取后再补回 UTC 时区，因此上层永远拿到
  带时区的 UTC ``datetime``，不会出现 naive/aware 混用。

对上层暴露的时间一律是带 UTC 时区的 ``datetime``；写入 naive datetime 会直接报错，
避免把本地时间静默当成 UTC 存进数据库。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.core.time import utc_now


class UtcDateTime(TypeDecorator[datetime]):
    """以 UTC 读写的时间戳。"""

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            # PostgreSQL 原生带时区时间戳
            return dialect.type_descriptor(DateTime(timezone=True))
        # SQLite 等方言不支持带时区时间戳，存储前已转为 UTC，读取时补回时区
        return dialect.type_descriptor(DateTime(timezone=False))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "UtcDateTime 不接受 naive datetime；请使用 app.core.time.utc_now()"
            )
        return value.astimezone(utc_now().tzinfo)

    def process_result_value(
        self, value: datetime | None, dialect: Dialect
    ) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=utc_now().tzinfo)
        return value.astimezone(utc_now().tzinfo)
