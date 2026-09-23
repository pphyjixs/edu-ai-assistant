"""Chat 数据访问层（``docs/api-contract.md`` 第 6 节）。

只做查询与写入，不含业务判断、不提交事务——事务边界在 ``service.py``。

并发控制的关键是 :func:`bump_session_version`：它用**条件更新**
（``WHERE version = :expected``）实现乐观锁，同一会话的并发发送只有一条
能成功递增版本，其余得到 ``rowcount == 0``，由服务层转成
``409 CHAT_CONFLICT``（契约 6.1）。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.chat.models import (
    ChatAttemptStatus,
    ChatGenerationAttempt,
    ChatMessage,
    ChatMessageCitation,
    ChatMessageRole,
    ChatSession,
    CitationSourceKind,
)

#: 单条消息最多保存的引用数量（与检索上限一致）
MAX_CITATIONS_PER_MESSAGE = 5


def create_session(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    course_id: uuid.UUID,
    user_id: uuid.UUID,
    now: datetime,
) -> ChatSession:
    """创建会话；``last_message_at`` 初始等于创建时间（契约 6.3 的排序要求）。"""
    chat_session = ChatSession(
        id=session_id,
        course_id=course_id,
        user_id=user_id,
        version=0,
        last_message_at=now,
        created_at=now,
    )
    session.add(chat_session)
    return chat_session


async def get_owned_session(
    session: AsyncSession, *, session_id: uuid.UUID, user_id: uuid.UUID
) -> ChatSession | None:
    """取当前用户拥有的会话；不存在或不是所有者一律返回 ``None``（契约 6.1）。"""
    result = await session.execute(
        select(ChatSession).where(
            ChatSession.id == session_id,
            ChatSession.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def list_owned_sessions(
    session: AsyncSession,
    *,
    course_id: uuid.UUID,
    user_id: uuid.UUID,
    offset: int,
    limit: int,
) -> tuple[list[ChatSession], int]:
    """我的会话列表（契约 6.3）：按 ``last_message_at`` 倒序、``id`` 倒序。"""
    conditions = (
        ChatSession.course_id == course_id,
        ChatSession.user_id == user_id,
    )
    total = await session.scalar(
        select(func.count()).select_from(ChatSession).where(*conditions)
    )
    result = await session.execute(
        select(ChatSession)
        .where(*conditions)
        .order_by(ChatSession.last_message_at.desc(), ChatSession.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), int(total or 0)


async def bump_session_version(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    expected_version: int,
    now: datetime,
) -> bool:
    """乐观锁：会话版本仍等于 ``expected_version`` 时 +1 并刷新最近消息时间。

    返回 ``False`` 表示会话在生成期间被并发修改（版本已前进），调用方必须
    放弃写入并返回 ``409 CHAT_CONFLICT``——不会写入半组消息（契约 6.1）。
    """
    result = await session.execute(
        update(ChatSession)
        .where(
            ChatSession.id == session_id,
            ChatSession.version == expected_version,
        )
        .values(version=ChatSession.version + 1, last_message_at=now)
    )
    return result.rowcount == 1


def add_message(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    session_id: uuid.UUID,
    role: ChatMessageRole,
    content: str,
    grounded: bool | None,
    now: datetime,
) -> ChatMessage:
    """暂存一条消息（用户消息与助手消息在同一事务内写入）。"""
    message = ChatMessage(
        id=message_id,
        session_id=session_id,
        role=role,
        content=content,
        grounded=grounded,
        created_at=now,
    )
    session.add(message)
    return message


async def list_messages(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    offset: int,
    limit: int,
) -> tuple[list[ChatMessage], int]:
    """会话消息（契约 6.4）：按 ``created_at`` 升序、``id`` 升序。"""
    total = await session.scalar(
        select(func.count())
        .select_from(ChatMessage)
        .where(ChatMessage.session_id == session_id)
    )
    result = await session.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), int(total or 0)


def add_citation(
    session: AsyncSession,
    *,
    citation_id: uuid.UUID,
    message_id: uuid.UUID,
    order: int,
    material_id: uuid.UUID | None,
    material_name: str | None,
    section_id: uuid.UUID | None,
    section_title: str | None,
    source_type: str,
    location_start: int | None,
    location_end: int | None,
    page: int | None,
    quote: str,
    now: datetime,
    source_kind: str = CitationSourceKind.MATERIAL.value,
    source_id: uuid.UUID | None = None,
    source_label: str | None = None,
) -> ChatMessageCitation:
    """暂存一条引用快照（契约 6.6）。

    ``source_kind`` 区分资料引用与作业/评分标准引用（评审文档「一、#2」）。
    非资料引用不填 ``material_*`` 与 ``location_*``，只填 ``source_id`` /
    ``source_label``；资料引用则让 ``source_id`` 默认取 ``material_id``，
    这样前端可以统一用 ``source_id`` 定位来源。
    """
    citation = ChatMessageCitation(
        id=citation_id,
        message_id=message_id,
        order=order,
        source_kind=source_kind,
        source_id=source_id if source_id is not None else material_id,
        source_label=source_label if source_label is not None else material_name,
        material_id=material_id,
        material_name=material_name,
        section_id=section_id,
        section_title=section_title,
        source_type=source_type,
        location_start=location_start,
        location_end=location_end,
        page=page,
        quote=quote,
        created_at=now,
    )
    session.add(citation)
    return citation


async def list_citations_for_messages(
    session: AsyncSession, *, message_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ChatMessageCitation]]:
    """批量取多条消息的引用，按 (消息, order) 聚合。"""
    if not message_ids:
        return {}
    result = await session.execute(
        select(ChatMessageCitation)
        .where(ChatMessageCitation.message_id.in_(message_ids))
        .order_by(ChatMessageCitation.message_id, ChatMessageCitation.order)
    )
    grouped: dict[uuid.UUID, list[ChatMessageCitation]] = {}
    for citation in result.scalars().all():
        grouped.setdefault(citation.message_id, []).append(citation)
    return grouped


def add_attempt(
    session: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    status: ChatAttemptStatus,
    model: str | None,
    prompt_version: str,
    retrieved_count: int,
    grounded: bool | None,
    duration_ms: int,
    error: str | None,
    now: datetime,
) -> ChatGenerationAttempt:
    """暂存一条生成尝试记录（内部可观测性，契约 6.1）。"""
    attempt = ChatGenerationAttempt(
        id=attempt_id,
        session_id=session_id,
        user_id=user_id,
        status=status,
        model=model,
        prompt_version=prompt_version,
        retrieved_count=retrieved_count,
        grounded=grounded,
        duration_ms=duration_ms,
        error=error,
        created_at=now,
    )
    session.add(attempt)
    return attempt


async def count_attempts(session: AsyncSession, *, session_id: uuid.UUID) -> int:
    """统计会话的生成尝试数量（验收用：失败也要留下尝试记录）。"""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ChatGenerationAttempt)
            .where(ChatGenerationAttempt.session_id == session_id)
        )
        or 0
    )
