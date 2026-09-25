"""Agent 工具实现的公共工具函数。

刻意保持很薄：这里只放"多个工具都会用到的小工具"，业务规则仍由各领域模块的
service / repository 提供（开发方案 5.2：handler 只调用模块 service）。

**事务约定**：handler 用 ``ctx.session_factory()`` 打开自己的短事务并在返回前关闭；
等待模型期间绝不持有数据库连接或事务。这里提供的 :func:`read_session` 就是
一个只读短事务的上下文管理器。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.agent.tool_types import ToolContext, ToolEvidence

#: 定位单位：与前端展示文案保持一致（P3 / 幻灯片5 / 段落12）
_LOCATION_UNITS = {
    "PDF_PAGE": "P",
    "PPTX_SLIDE": "幻灯片",
    "DOCX_PARAGRAPH": "段落",
}


@asynccontextmanager
async def read_session(ctx: ToolContext) -> AsyncIterator[AsyncSession]:
    """打开一个只读短事务。

    退出时**回滚**而不是提交：只读工具不写库，显式结束事务能保证连接不会被
    意外留在事务里（下一次模型调用可能几十秒，绝不能占着连接）。
    """
    async with ctx.session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()


def location_label(
    source_location_type: str | None, start: int | None, end: int | None
) -> str:
    """把定位渲染成可读文案（``P3``、``幻灯片5``、``段落12-14``）。"""
    if start is None or not source_location_type:
        return ""
    unit = _LOCATION_UNITS.get(source_location_type)
    if not unit:
        return ""
    if end is not None and end > start:
        return f"{unit}{start}-{end}"
    return f"{unit}{start}"


def material_evidence(
    *,
    chunk_id: uuid.UUID,
    material_id: uuid.UUID,
    material_name: str,
    source_location_type: str,
    content: str,
    location_start: int,
    location_end: int,
) -> ToolEvidence:
    """把检索片段转成可引用证据（资料片段统一走这里，避免各工具字段不一致）。"""
    location = location_label(source_location_type, location_start, location_end)
    return ToolEvidence(
        source_type="MATERIAL_CHUNK",
        source_id=chunk_id,
        label=f"资料《{material_name}》· 原文" + (f"（{location}）" if location else ""),
        text=content,
        material_id=material_id,
        chunk_id=chunk_id,
        location_start=location_start,
        location_end=location_end,
        material_name=material_name,
        source_location_type=source_location_type,
        groundable=True,
        display_kind="MATERIAL",
    )


__all__ = ["location_label", "material_evidence", "read_session"]
