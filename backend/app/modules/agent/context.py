"""Agent 上下文解析器（``docs/local-development-agent-backend.md`` 第 6.6 节）。

职责边界很硬：:func:`resolve_context` **只接收已经鉴权过的事实**
（``user_id`` + 会话课程 + 已校验可见性的 typed context），输出标准化的
:class:`ResolvedContext`——业务对象摘要 + 可引用来源块 + 最近历史。

安全顺序（文档 6.6）：会话可见 → 课程成员 → 实体属于课程且可见 → 状态检查。
本模块负责“实体属于课程且可见”与“状态检查”两段；前两段由服务层完成。

每个注入块都带一个稳定编号（``S1``、``S2``…）。模型只能通过编号引用，
服务端再按编号还原成真实来源——因此模型无法引用本次上下文之外的任何东西。

只有**落在资料上的**来源（原文片段、章节大纲）可以成为用户可见引用，
因为消息引用表指向 ``materials``；作业与课程摘要作为上下文注入，
但不进入引用列表。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    AgentContextNotReadyError,
    AgentContextUnsupportedError,
    ResourceNotFoundError,
)
from app.modules.agent.models import AgentEntityType, AgentSourceType
from app.modules.agent.schemas import (
    ENTITY_TYPES_REQUIRING_ID,
    ENTITY_TYPES_REQUIRING_SECTION,
    SUPPORTED_ENTITY_TYPES,
)
from app.modules.chat.retrieval import search_chunks

#: 作业/课程的可见性：学生只能看这三种状态（契约 8.1）
_STUDENT_VISIBLE_ASSIGNMENT_STATUSES = ("PUBLISHED", "CLOSED", "ARCHIVED")

#: 规范 MIME → 大纲来源类型（契约 5.4）
_SOURCE_TYPE_BY_CONTENT_TYPE = {
    "application/pdf": "PDF_PAGE",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PPTX_SLIDE",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX_PARAGRAPH",
}


@dataclass(frozen=True, slots=True)
class ContextBlock:
    """一个可注入、可被引用的上下文块。"""

    ref: str
    source_type: AgentSourceType
    source_id: uuid.UUID
    label: str
    text: str
    material_id: uuid.UUID | None = None
    chunk_id: uuid.UUID | None = None
    location_start: int | None = None
    location_end: int | None = None
    #: 大纲来源在映射回消息引用时需要的补充字段
    section_title: str | None = None
    material_name: str | None = None
    source_location_type: str | None = None
    #: 是否允许成为用户可见引用（引用表指向 materials）
    citable: bool = False


@dataclass(slots=True)
class ResolvedContext:
    """标准化的上下文。"""

    course_id: uuid.UUID
    entity_type: AgentEntityType | None
    entity_id: uuid.UUID | None
    #: 业务对象的结构化摘要（作业标题/说明/评分项、资料名…）
    summary: str = ""
    blocks: list[ContextBlock] = field(default_factory=list)
    history: list[tuple[str, str]] = field(default_factory=list)
    #: 因预算被省略的内容说明；会写进提示词，避免模型误以为看到了全部
    truncated_note: str | None = None

    def block_by_ref(self) -> dict[str, ContextBlock]:
        return {block.ref: block for block in self.blocks}

    @property
    def citable_blocks(self) -> list[ContextBlock]:
        return [block for block in self.blocks if block.citable]


class _Budget:
    """上下文字符预算；超出的块会被丢弃并记录说明。"""

    def __init__(self, limit: int) -> None:
        self.limit = max(1_000, limit)
        self.used = 0
        self.dropped = 0

    def take(self, text: str) -> bool:
        if self.used + len(text) > self.limit:
            self.dropped += 1
            return False
        self.used += len(text)
        return True

    def note(self) -> str | None:
        if self.dropped == 0:
            return None
        return f"（因上下文长度限制，省略了 {self.dropped} 段内容）"


_COURSE_SQL = text(
    "SELECT id, name, description, teacher_id, status FROM courses WHERE id = :course_id"
)

_MATERIAL_SQL = text(
    """
    SELECT id, course_id, filename, content_type, status
    FROM materials
    WHERE id = :material_id AND course_id = :course_id AND deleted_at IS NULL
    """
)

_SECTIONS_SQL = text(
    """
    SELECT id, "order", title, location_start, location_end
    FROM material_sections
    WHERE material_id = :material_id
    ORDER BY "order" ASC, id ASC
    """
)

_SECTION_BY_ID_SQL = text(
    """
    SELECT id, "order", title, location_start, location_end
    FROM material_sections
    WHERE id = :section_id AND material_id = :material_id
    """
)

_KNOWLEDGE_POINTS_SQL = text(
    """
    SELECT section_id, "order", title, description, quote, location_start, location_end
    FROM material_knowledge_points
    WHERE section_id IN :section_ids
    ORDER BY "order" ASC, id ASC
    """
).bindparams(bindparam("section_ids", expanding=True))

_MATERIAL_CHUNKS_SQL = text(
    """
    SELECT id, "order", content, location_start, location_end
    FROM material_chunks
    WHERE material_id = :material_id
    ORDER BY "order" ASC, id ASC
    """
)

_ASSIGNMENT_SQL = text(
    """
    SELECT a.id, a.course_id, a.title, a.description, a.status, a.due_at,
           a.allow_late_submission, v.total_score
    FROM assignments AS a
    LEFT JOIN assignment_rubric_versions AS v ON v.id = a.current_rubric_version_id
    WHERE a.id = :assignment_id AND a.course_id = :course_id
    """
)

_RUBRIC_ITEMS_SQL = text(
    """
    SELECT title, description, max_score
    FROM assignment_rubric_items
    WHERE rubric_version_id = (
        SELECT current_rubric_version_id FROM assignments WHERE id = :assignment_id
    )
    ORDER BY "order" ASC, id ASC
    """
)

_HISTORY_SQL = text(
    """
    SELECT role, content
    FROM chat_messages
    WHERE session_id = :session_id
    ORDER BY created_at DESC, id DESC
    LIMIT :limit
    """
)


def _source_type_for(content_type: str) -> str:
    return _SOURCE_TYPE_BY_CONTENT_TYPE.get(content_type, "PDF_PAGE")


def _location_label(source_location_type: str, start: int | None, end: int | None) -> str:
    """把定位渲染成与前端一致的可读文案（P3 / 幻灯片5 / 段落12）。"""
    if start is None:
        return ""
    unit = {"PDF_PAGE": "P", "PPTX_SLIDE": "幻灯片", "DOCX_PARAGRAPH": "段落"}.get(
        source_location_type, ""
    )
    if not unit:
        return ""
    if end is not None and end > start:
        return f"{unit}{start}-{end}"
    return f"{unit}{start}"


async def _load_history(
    session: AsyncSession, *, session_id: uuid.UUID, limit: int
) -> list[tuple[str, str]]:
    if limit <= 0:
        return []
    rows = (
        await session.execute(_HISTORY_SQL, {"session_id": session_id, "limit": limit})
    ).all()
    # 查询是倒序取最近 N 条，返回时恢复时间正序，便于提示词按发生顺序排列
    return [(row.role, row.content) for row in reversed(rows)]


async def _load_material_row(
    session: AsyncSession, *, course_id: uuid.UUID, material_id: uuid.UUID
) -> object:
    row = (
        await session.execute(
            _MATERIAL_SQL, {"material_id": material_id, "course_id": course_id}
        )
    ).first()
    if row is None:
        # 不存在、不属于本课程、已删除：统一 404，避免 UUID 探测
        raise ResourceNotFoundError()
    if row.status != "READY":
        raise AgentContextNotReadyError()
    return row


async def _append_outline_blocks(
    session: AsyncSession,
    *,
    budget: _Budget,
    blocks: list[ContextBlock],
    material_row: object,
    only_section_id: uuid.UUID | None,
) -> None:
    """把资料大纲注入为可引用块；``only_section_id`` 非空时只取该章节及相邻章节。"""
    sections = (
        await session.execute(_SECTIONS_SQL, {"material_id": material_row.id})
    ).all()
    if not sections:
        return

    if only_section_id is not None:
        target_index = next(
            (index for index, row in enumerate(sections) if row.id == only_section_id), None
        )
        if target_index is None:
            raise ResourceNotFoundError()
        # 相邻章节：模型需要看到上下文，但用户只额外指定了"重点在这一节"
        sections = sections[max(0, target_index - 1) : target_index + 2]

    section_ids = [row.id for row in sections]
    points = (
        await session.execute(_KNOWLEDGE_POINTS_SQL, {"section_ids": section_ids})
    ).all()
    grouped: dict[uuid.UUID, list[object]] = {}
    for point in points:
        grouped.setdefault(point.section_id, []).append(point)

    source_location_type = _source_type_for(material_row.content_type)
    for row in sections:
        lines = [f"章节：{row.title}"]
        for point in grouped.get(row.id, []):
            lines.append(f"- 知识点：{point.title}")
            if point.description:
                lines.append(f"  说明：{point.description}")
            if point.quote:
                lines.append(f"  原文摘录：{point.quote}")
        text_body = "\n".join(lines)
        if not budget.take(text_body):
            continue
        location = _location_label(source_location_type, row.location_start, row.location_end)
        blocks.append(
            ContextBlock(
                ref="",
                source_type=AgentSourceType.MATERIAL_OUTLINE,
                source_id=row.id,
                label=f"资料《{material_row.filename}》· 章节「{row.title}」"
                + (f" · {location}" if location else ""),
                text=text_body,
                material_id=material_row.id,
                location_start=row.location_start,
                location_end=row.location_end,
                section_title=row.title,
                material_name=material_row.filename,
                source_location_type=source_location_type,
                citable=True,
            )
        )


async def _append_chunk_blocks(
    session: AsyncSession,
    *,
    budget: _Budget,
    blocks: list[ContextBlock],
    chunks: list[_ChunkLike],
) -> None:
    """把原文片段注入为可引用块。

    资料名与定位单位都取自片段自身携带的 ``material_name`` / ``content_type``，
    因此多份资料混合检索时也不会张冠李戴（DOCX 用"段落 n"而不是"Pn"）。
    """
    for chunk in chunks:
        if not budget.take(chunk.content):
            continue
        location_type = _source_type_for(chunk.content_type)
        location = _location_label(location_type, chunk.location_start, chunk.location_end)
        blocks.append(
            ContextBlock(
                ref="",
                source_type=AgentSourceType.MATERIAL_CHUNK,
                source_id=chunk.chunk_id,
                label=f"资料《{chunk.material_name}》· 原文"
                + (f"（{location}）" if location else ""),
                text=chunk.content,
                material_id=chunk.material_id,
                chunk_id=chunk.chunk_id,
                location_start=chunk.location_start,
                location_end=chunk.location_end,
                material_name=chunk.material_name,
                source_location_type=location_type,
                citable=True,
            )
        )


async def _resolve_material(
    session: AsyncSession,
    *,
    course_id: uuid.UUID,
    material_id: uuid.UUID,
    section_id: uuid.UUID | None,
    budget: _Budget,
) -> ResolvedContext:
    material = await _load_material_row(
        session, course_id=course_id, material_id=material_id
    )

    if section_id is not None:
        # 章节必须属于这份资料，否则按不可见处理
        owned = (
            await session.execute(
                _SECTION_BY_ID_SQL, {"section_id": section_id, "material_id": material_id}
            )
        ).first()
        if owned is None:
            raise ResourceNotFoundError()

    blocks: list[ContextBlock] = []
    await _append_outline_blocks(
        session,
        budget=budget,
        blocks=blocks,
        material_row=material,
        only_section_id=section_id,
    )

    chunk_rows = (
        await session.execute(_MATERIAL_CHUNKS_SQL, {"material_id": material_id})
    ).all()
    chunk_objects = [
        _ChunkRow(
            chunk_id=row.id,
            material_id=material_id,
            material_name=material.filename,
            content_type=material.content_type,
            content=row.content,
            location_start=row.location_start,
            location_end=row.location_end,
        )
        for row in chunk_rows
    ]
    await _append_chunk_blocks(session, budget=budget, blocks=blocks, chunks=chunk_objects)

    summary = (
        f"资料：《{material.filename}》\n"
        f"类型：{material.content_type}\n"
        f"状态：解析完成，共 {len(chunk_rows)} 个原文片段、"
        f"{len({b.source_id for b in blocks if b.source_type is AgentSourceType.MATERIAL_OUTLINE})} 个章节"
    )
    return ResolvedContext(
        course_id=course_id,
        entity_type=AgentEntityType.MATERIAL
        if section_id is None
        else AgentEntityType.MATERIAL_SECTION,
        entity_id=material_id,
        summary=summary,
        blocks=blocks,
        truncated_note=budget.note(),
    )


@dataclass(frozen=True, slots=True)
class _ChunkRow:
    """本地查询出来的片段（与 :class:`RetrievedChunk` 保持同一组字段）。"""

    chunk_id: uuid.UUID
    material_id: uuid.UUID
    material_name: str
    content_type: str
    content: str
    location_start: int
    location_end: int


class _ChunkLike(Protocol):
    """注入片段所需的最小字段集合。

    ``chat.retrieval.RetrievedChunk`` 与本地查询的 ``_ChunkRow`` 都满足它，
    因此检索路径与"整份资料"路径可以共用同一段注入逻辑。
    """

    chunk_id: uuid.UUID
    material_id: uuid.UUID
    material_name: str
    content_type: str
    content: str
    location_start: int
    location_end: int


async def _resolve_assignment(
    session: AsyncSession,
    *,
    course_id: uuid.UUID,
    assignment_id: uuid.UUID,
    question: str,
    is_staff: bool,
    settings: Settings,
    budget: _Budget,
) -> ResolvedContext:
    row = (
        await session.execute(
            _ASSIGNMENT_SQL, {"assignment_id": assignment_id, "course_id": course_id}
        )
    ).first()
    if row is None:
        raise ResourceNotFoundError()
    # 学生只能看到已发布/已关闭/已归档的作业（契约 8.1）
    if not is_staff and row.status not in _STUDENT_VISIBLE_ASSIGNMENT_STATUSES:
        raise ResourceNotFoundError()

    rubric = (
        await session.execute(_RUBRIC_ITEMS_SQL, {"assignment_id": assignment_id})
    ).all()
    lines = [f"作业：{row.title}", f"状态：{row.status}"]
    if row.total_score is not None:
        lines.append(f"总分：{row.total_score}")
    if row.due_at is not None:
        lines.append(f"截止时间：{row.due_at.isoformat()}（UTC）")
        lines.append(f"允许补交：{'是' if row.allow_late_submission else '否'}")
    if row.description:
        lines.append(f"任务说明：{row.description}")
    if rubric:
        lines.append("评分标准：")
        for item in rubric:
            description = f"（{item.description}）" if item.description else ""
            lines.append(f"- {item.title}{description}：{item.max_score} 分")

    summary_text = "\n".join(lines)
    blocks: list[ContextBlock] = [
        ContextBlock(
            ref="",
            source_type=AgentSourceType.ASSIGNMENT,
            source_id=row.id,
            label=f"作业《{row.title}》",
            text=summary_text,
            # 作业不是资料，不能成为消息引用（引用表指向 materials）
            citable=False,
        )
    ]
    # 作业页提问时补一次课程资料检索，让回答有机会带上可核对的资料来源
    if budget.take(summary_text):
        retrieved = await search_chunks(
            session, course_id=course_id, question=question, limit=settings.agent_max_retrieved_chunks
        )
        await _append_chunk_blocks(session, budget=budget, blocks=blocks, chunks=retrieved)
    else:
        blocks = []

    return ResolvedContext(
        course_id=course_id,
        entity_type=AgentEntityType.ASSIGNMENT,
        entity_id=assignment_id,
        summary=summary_text,
        blocks=blocks,
        truncated_note=budget.note(),
    )


async def _resolve_course(
    session: AsyncSession,
    *,
    course_id: uuid.UUID,
    question: str,
    settings: Settings,
    budget: _Budget,
) -> ResolvedContext:
    course = (
        await session.execute(_COURSE_SQL, {"course_id": course_id})
    ).first()
    if course is None:  # pragma: no cover - 会话存在即课程存在
        raise ResourceNotFoundError()

    summary_parts = [f"课程：{course.name}"]
    if course.description:
        summary_parts.append(f"课程说明：{course.description}")
    summary = "\n".join(summary_parts)

    blocks: list[ContextBlock] = []
    retrieved = await search_chunks(
        session, course_id=course_id, question=question, limit=settings.agent_max_retrieved_chunks
    )
    await _append_chunk_blocks(session, budget=budget, blocks=blocks, chunks=retrieved)

    return ResolvedContext(
        course_id=course_id,
        entity_type=AgentEntityType.COURSE,
        entity_id=course_id,
        summary=summary,
        blocks=blocks,
        truncated_note=budget.note(),
    )


async def validate_context_target(
    session: AsyncSession,
    *,
    course_id: uuid.UUID,
    entity_type: AgentEntityType | None,
    entity_id: uuid.UUID | None,
    section_id: uuid.UUID | None,
    is_staff: bool,
) -> None:
    """创建 Run 时的同步校验（文档 6.4 第 3 步）。

    只做"对象属于本课程且当前用户可见"与状态检查，不构建上下文——
    真正注入发生在 Worker 里。这里必须同步失败，否则用户会拿到一个注定失败的 Run。

    :raises AgentContextUnsupportedError: 实体类型未实现或缺少必填 ID
    :raises ResourceNotFoundError: 不存在 / 不属于本课程 / 对学生不可见
    :raises AgentContextNotReadyError: 资料尚未解析完成
    """
    if entity_type is None:
        return

    if entity_type not in SUPPORTED_ENTITY_TYPES:
        raise AgentContextUnsupportedError(
            f"{entity_type.value} 上下文尚未实现，等对应领域模块落地后再开放"
        )

    if entity_type in ENTITY_TYPES_REQUIRING_ID and entity_id is None:
        raise AgentContextUnsupportedError(f"{entity_type.value} 上下文必须提供 entity_id")

    if entity_type in ENTITY_TYPES_REQUIRING_SECTION and section_id is None:
        raise AgentContextUnsupportedError("MATERIAL_SECTION 上下文必须提供 section_id")

    if entity_type in (AgentEntityType.MATERIAL, AgentEntityType.MATERIAL_SECTION):
        material = await _load_material_row(
            session, course_id=course_id, material_id=entity_id  # type: ignore[arg-type]
        )
        if section_id is not None:
            owned = (
                await session.execute(
                    _SECTION_BY_ID_SQL,
                    {"section_id": section_id, "material_id": material.id},
                )
            ).first()
            if owned is None:
                raise ResourceNotFoundError()
        return

    if entity_type is AgentEntityType.ASSIGNMENT:
        row = (
            await session.execute(
                _ASSIGNMENT_SQL, {"assignment_id": entity_id, "course_id": course_id}
            )
        ).first()
        if row is None:
            raise ResourceNotFoundError()
        if not is_staff and row.status not in _STUDENT_VISIBLE_ASSIGNMENT_STATUSES:
            raise ResourceNotFoundError()
        return


async def resolve_context(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    course_id: uuid.UUID,
    entity_type: AgentEntityType | None,
    entity_id: uuid.UUID | None,
    section_id: uuid.UUID | None,
    selected_text: str | None,
    question: str,
    is_staff: bool,
    settings: Settings,
) -> ResolvedContext:
    """解析本次 Run 的上下文。

    :param is_staff: 当前用户是否为本课程教师（决定作业可见性）
    :raises ResourceNotFoundError: 实体不存在、不属于会话课程或对学生不可见
    :raises AgentContextNotReadyError: 资料尚未解析完成
    :raises AgentContextUnsupportedError: 该实体类型尚未实现
    """
    budget = _Budget(settings.agent_context_max_chars)

    if entity_type is None:
        resolved = await _resolve_course(
            session, course_id=course_id, question=question, settings=settings, budget=budget
        )
    elif entity_type in (AgentEntityType.MATERIAL, AgentEntityType.MATERIAL_SECTION):
        if entity_id is None:
            raise AgentContextUnsupportedError("MATERIAL 上下文必须提供 entity_id")
        resolved = await _resolve_material(
            session,
            course_id=course_id,
            material_id=entity_id,
            section_id=section_id,
            budget=budget,
        )
    elif entity_type is AgentEntityType.ASSIGNMENT:
        if entity_id is None:
            raise AgentContextUnsupportedError("ASSIGNMENT 上下文必须提供 entity_id")
        resolved = await _resolve_assignment(
            session,
            course_id=course_id,
            assignment_id=entity_id,
            question=question,
            is_staff=is_staff,
            settings=settings,
            budget=budget,
        )
    else:
        # SUBMISSION / GRADE 等：明确告知未支持，不假装已实现（契约 6.3）
        raise AgentContextUnsupportedError(
            f"{entity_type.value} 上下文尚未实现，等对应领域模块落地后再开放"
        )

    if selected_text:
        # 用户附加文本是不可信材料，单独成块并明确标注来源
        trimmed = selected_text[: settings.agent_selected_text_max_chars]
        resolved.blocks.append(
            ContextBlock(
                ref="",
                source_type=AgentSourceType.COURSE,
                source_id=session_id,
                label="用户选中的文本（由用户提供，不是课程资料）",
                text=trimmed,
                citable=False,
            )
        )

    resolved.history = await _load_history(
        session, session_id=session_id, limit=settings.agent_history_max_messages
    )
    # 统一编号：模型只能用 S<n> 引用，服务端再按编号还原真实来源
    resolved.blocks = [
        ContextBlock(
            ref=f"S{index}",
            source_type=block.source_type,
            source_id=block.source_id,
            label=block.label,
            text=block.text,
            material_id=block.material_id,
            chunk_id=block.chunk_id,
            location_start=block.location_start,
            location_end=block.location_end,
            section_title=block.section_title,
            material_name=block.material_name,
            source_location_type=block.source_location_type,
            citable=block.citable,
        )
        for index, block in enumerate(resolved.blocks, start=1)
    ]
    return resolved
