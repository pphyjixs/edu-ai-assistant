"""Agent 上下文解析器（``docs/local-development-agent-backend.md`` 第 6.6 节 +
``docs/agent-backend-implementation-review.md`` 第一节）。

职责边界很硬：:func:`resolve_context` **只接收已经鉴权过的事实**
（``user_id`` + 会话课程 + 已校验可见性的 typed context），输出标准化的
:class:`ResolvedContext`——业务对象摘要 + 可引用来源块 + 最近历史。

安全顺序（文档 6.6）：会话可见 → 课程成员 → 实体属于课程且可见 → 状态检查。
本模块负责"实体属于课程且可见"与"状态检查"两段；前两段由服务层完成。

每个注入块都带一个稳定编号（``S1``、``S2``…）。模型只能通过编号引用，
服务端再按编号还原成真实来源——因此模型无法引用本次上下文之外的任何东西。

**依据与引用的分离**（评审文档「一、#2」）：一个块有两个互相独立的属性。

- ``groundable``：能否支撑一次**有依据**的回答。资料片段、章节大纲、作业说明、
  评分标准都是可信依据；课程名与用户选中文本不是。
- ``display_kind``：能否成为**用户可见引用**，以及引用类别。``MATERIAL`` 可以跳到
  资料阅读器；``ASSIGNMENT`` 指向作业与评分标准，只做展示。

这样「拆解这个作业」在只有作业上下文时也能给出正文并标注依据，而不是被抹成
"未找到依据"。

**预算**（评审文档「一、#7」）：预算按**整次请求**计算，先预留历史、选中文本与
固定提示词的份额，剩余部分才给来源块；块按"平均大小 × 剩余容量"决定注入多少，
并在**整份资料/整门课程**上均匀取样，避免长资料只喂开头（评审文档「一、#6」）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
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
from app.modules.agent.models import AgentEntityType, AgentRunAction, AgentSourceType
from app.modules.agent.schemas import (
    ENTITY_TYPES_REQUIRING_ID,
    ENTITY_TYPES_REQUIRING_SECTION,
    SUPPORTED_ENTITY_TYPES,
)
from app.modules.chat.retrieval import (
    merge_round_robin,
    search_chunks,
    search_material_chunks,
)

#: 作业/课程的可见性：学生只能看这三种状态（契约 8.1）
_STUDENT_VISIBLE_ASSIGNMENT_STATUSES = ("PUBLISHED", "CLOSED", "ARCHIVED")

#: 规范 MIME → 大纲来源类型（契约 5.4）
_SOURCE_TYPE_BY_CONTENT_TYPE = {
    "application/pdf": "PDF_PAGE",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "PPTX_SLIDE",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "DOCX_PARAGRAPH",
}

#: 给系统规则、动作模板与输出 JSON 说明预留的字符数。
#: 不精确等于 token 数，但能保证"总预算"不会只算资料块而漏掉固定提示词（评审文档 #7）。
PROMPT_OVERHEAD_RESERVE_CHARS = 2_000

#: 业务对象摘要的预留字符数（资料名/作业标题这类小段文字）
SUMMARY_RESERVE_CHARS = 800

#: 单个来源块的最小字符配额：预算再紧也要让每块至少进来一点，
#: 否则"取样"会退化成全都进不来
MIN_BLOCK_CHARS = 200


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
    #: 能否支撑一次"有依据"的回答（资料、作业说明、评分标准都是可信依据）
    groundable: bool = False
    #: 能否成为用户可见引用及引用类别（``MATERIAL`` / ``ASSIGNMENT``）；
    #: ``None`` 表示只进提示词、不进引用列表（课程摘要、用户选中文本）
    display_kind: str | None = None


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
    #: 本次实际覆盖的资料数量与名称。
    #: 用于把"没有依据"说清楚（"已检索这 3 份资料，但没有出现该术语"），
    #: 而不是所有失败都回同一句话（评审文档「一、#1」与「二、4」）。
    searched_materials: int = 0
    searched_material_names: list[str] = field(default_factory=list)

    def block_by_ref(self) -> dict[str, ContextBlock]:
        return {block.ref: block for block in self.blocks}

    @property
    def groundable_blocks(self) -> list[ContextBlock]:
        return [block for block in self.blocks if block.groundable]

    def has_groundable_evidence(self) -> bool:
        return any(block.groundable for block in self.blocks)


class _Budget:
    """整次请求的字符预算；超出的块会被丢弃并记录说明。

    与"只统计资料块"的旧实现相比，:meth:`reserve` 会把历史、选中文本与固定
    提示词的份额先扣掉，因此 ``take()`` 用的是**真正剩余**的额度。
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(1_000, limit)
        self.used = 0
        self.dropped = 0

    @property
    def remaining(self) -> int:
        return max(MIN_BLOCK_CHARS, self.limit - self.used)

    def reserve(self, chars: int) -> None:
        """预留一段不参与接管的额度（历史、选中文本、固定提示词）。"""
        self.used += max(0, chars)

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


def _spread(items: Sequence[object], keep: int) -> list:
    """在整段序列上**均匀取样** ``keep`` 个元素。

    旧实现按数据库顺序塞满预算，长资料的末尾永远不会进入提示词
    （评审文档「一、#6」）。均匀取样保证首、中、尾都有代表。
    """
    total = len(items)
    if keep >= total:
        return list(items)
    if keep <= 0:
        return []
    step = total / keep
    picked: list = []
    seen: set[int] = set()
    for index in range(keep):
        position = min(total - 1, int(index * step))
        if position in seen:
            continue
        seen.add(position)
        picked.append(items[position])
    return picked


def _capacity_for(chunks: Sequence[_ChunkLike], budget: _Budget) -> int:
    """按平均片段长度估算能放进去的片段数量（至少 1）。"""
    if not chunks:
        return 0
    average = max(1, sum(len(chunk.content) for chunk in chunks) // len(chunks))
    return max(1, min(len(chunks), budget.remaining // average))


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

_COURSE_MATERIALS_SQL = text(
    """
    SELECT id, filename, content_type
    FROM materials
    WHERE course_id = :course_id AND deleted_at IS NULL AND status = 'READY'
    ORDER BY created_at ASC, id ASC
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
      AND (CAST(:exclude_id AS uuid) IS NULL OR id <> CAST(:exclude_id AS uuid))
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
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    limit: int,
    exclude_message_id: uuid.UUID | None = None,
) -> list[tuple[str, str]]:
    """最近若干条历史。

    ``exclude_message_id`` 用于**排除本次提问**：本次输入会在提示词的
    「本次输入」区段单独出现一次，若历史里再带一遍就会重复
    （评审文档「一、#13」）。
    """
    if limit <= 0:
        return []
    rows = (
        await session.execute(
            _HISTORY_SQL,
            {
                "session_id": session_id,
                "limit": limit,
                "exclude_id": exclude_message_id,
            },
        )
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


async def _list_ready_materials(
    session: AsyncSession, *, course_id: uuid.UUID
) -> list[object]:
    """课程中已解析完成、未删除的资料（用于说明"检索了哪些资料"）。"""
    rows = (
        await session.execute(_COURSE_MATERIALS_SQL, {"course_id": course_id})
    ).all()
    return list(rows)


def _block(
    *,
    source_type: AgentSourceType,
    source_id: uuid.UUID,
    label: str,
    text: str,
    groundable: bool,
    display_kind: str | None,
    material_id: uuid.UUID | None = None,
    chunk_id: uuid.UUID | None = None,
    location_start: int | None = None,
    location_end: int | None = None,
    section_title: str | None = None,
    material_name: str | None = None,
    source_location_type: str | None = None,
) -> ContextBlock:
    """构造上下文块（``ref`` 统一在 :func:`resolve_context` 末尾编号）。"""
    return ContextBlock(
        ref="",
        source_type=source_type,
        source_id=source_id,
        label=label,
        text=text,
        material_id=material_id,
        chunk_id=chunk_id,
        location_start=location_start,
        location_end=location_end,
        section_title=section_title,
        material_name=material_name,
        source_location_type=source_location_type,
        groundable=groundable,
        display_kind=display_kind,
    )


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
            _block(
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
                groundable=True,
                display_kind="MATERIAL",
            )
        )


async def _append_chunk_blocks(
    session: AsyncSession,
    *,
    budget: _Budget,
    blocks: list[ContextBlock],
    chunks: Sequence[_ChunkLike],
) -> None:
    """把原文片段注入为可引用块。

    资料名与定位单位都取自片段自身携带的 ``material_name`` / ``content_type``，
    因此多份资料混合检索时也不会张冠李戴（DOCX 用"段落 n"而不是"Pn"）。

    片段超过预算时**均匀取样**而不是"只留前几段"（评审文档「一、#6」）。
    """
    chosen = _spread(chunks, _capacity_for(chunks, budget))
    for chunk in chosen:
        if not budget.take(chunk.content):
            continue
        location_type = _source_type_for(chunk.content_type)
        location = _location_label(location_type, chunk.location_start, chunk.location_end)
        blocks.append(
            _block(
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
                groundable=True,
                display_kind="MATERIAL",
            )
        )


async def _resolve_material(
    session: AsyncSession,
    *,
    course_id: uuid.UUID,
    material_id: uuid.UUID,
    section_id: uuid.UUID | None,
    question: str,
    is_summarize: bool,
    settings: Settings,
    budget: _Budget,
) -> ResolvedContext:
    material = await _load_material_row(
        session, course_id=course_id, material_id=material_id
    )

    section_row = None
    if section_id is not None:
        # 章节必须属于这份资料，否则按不可见处理
        section_row = (
            await session.execute(
                _SECTION_BY_ID_SQL, {"section_id": section_id, "material_id": material_id}
            )
        ).first()
        if section_row is None:
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

    # 指定章节时，章节范围内的原文块优先进入上下文，剩余额度再给其他部分——
    # 否则"总结本节"会被资料开头的片段挤满（评审文档「一、#3」）。
    if section_row is not None:
        start = section_row.location_start
        end = section_row.location_end
        in_range = [
            item
            for item in chunk_objects
            if item.location_start >= start and item.location_end <= end
        ]
        in_range_ids = {item.chunk_id for item in in_range}
        out_of_range = [item for item in chunk_objects if item.chunk_id not in in_range_ids]
        await _append_chunk_blocks(session, budget=budget, blocks=blocks, chunks=in_range)
        await _append_chunk_blocks(
            session, budget=budget, blocks=blocks, chunks=out_of_range
        )
    elif is_summarize:
        # 「总结资料」不把用户那句话当成检索词，而是覆盖整份资料（评审文档「一、#1.6」）
        await _append_chunk_blocks(
            session, budget=budget, blocks=blocks, chunks=chunk_objects
        )
    else:
        # 资料页提问：先在这份资料内部按问题检索，命中为空才退化成全文取样，
        # 避免"整份资料里的相关段落"在长资料里被取样跳过。
        hits = await search_material_chunks(
            session,
            material_id=material_id,
            question=question,
            limit=settings.agent_max_retrieved_chunks,
            hard_limit=settings.agent_max_recall_chunks,
        )
        if hits:
            await _append_chunk_blocks(session, budget=budget, blocks=blocks, chunks=hits)
        else:
            await _append_chunk_blocks(
                session, budget=budget, blocks=blocks, chunks=chunk_objects
            )

    scope = f"章节「{section_row.title}」" if section_row is not None else "整份资料"
    summary = (
        f"资料：《{material.filename}》\n"
        f"类型：{material.content_type}\n"
        f"本次范围：{scope}；共 {len(chunk_rows)} 个原文片段、"
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
        searched_materials=1,
        searched_material_names=[material.filename],
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
    retrieve_materials: bool,
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

    # 作业说明与评分标准分成两块：模型可以分别引用，用户也能看到依据具体是哪一个。
    # 它们都是可信业务对象，因此 groundable=True（评审文档「一、#2」）。
    requirement_lines = [f"作业：{row.title}", f"状态：{row.status}"]
    if row.total_score is not None:
        requirement_lines.append(f"总分：{row.total_score}")
    if row.due_at is not None:
        requirement_lines.append(f"截止时间：{row.due_at.isoformat()}（UTC）")
        requirement_lines.append(f"允许补交：{'是' if row.allow_late_submission else '否'}")
    if row.description:
        requirement_lines.append(f"任务说明：{row.description}")
    requirement_text = "\n".join(requirement_lines)

    blocks: list[ContextBlock] = [
        _block(
            source_type=AgentSourceType.ASSIGNMENT,
            source_id=row.id,
            label=f"作业《{row.title}》· 要求",
            text=requirement_text,
            groundable=True,
            display_kind="ASSIGNMENT",
        )
    ]

    if rubric:
        rubric_lines = ["评分标准："]
        for item in rubric:
            description = f"（{item.description}）" if item.description else ""
            rubric_lines.append(f"- {item.title}{description}：{item.max_score} 分")
        blocks.append(
            _block(
                source_type=AgentSourceType.ASSIGNMENT,
                source_id=row.id,
                label=f"作业《{row.title}》· 评分标准",
                text="\n".join(rubric_lines),
                groundable=True,
                display_kind="ASSIGNMENT",
            )
        )

    # 摘要区只放"这是谁"，正文只在块里出现一次——旧实现把同一段作业说明
    # 同时写进 summary 和块，等于给模型喂了两遍（评审文档「一、#7」）。
    summary = f"当前对象：作业《{row.title}》（{row.status}）"

    # 作业页提问时补一次课程资料检索，让回答有机会带上可核对的资料来源。
    # 只有 ASK 才检索：「总结作业要求」「拆解任务」不该被检索结果带偏，
    # 它们本来就应当只依据作业与评分标准作答。
    if retrieve_materials:
        materials = await _list_ready_materials(session, course_id=course_id)
        retrieved = await search_chunks(
            session,
            course_id=course_id,
            question=question,
            limit=settings.agent_max_retrieved_chunks,
            hard_limit=settings.agent_max_recall_chunks,
        )
        await _append_chunk_blocks(session, budget=budget, blocks=blocks, chunks=retrieved)
        return ResolvedContext(
            course_id=course_id,
            entity_type=AgentEntityType.ASSIGNMENT,
            entity_id=assignment_id,
            summary=summary,
            blocks=blocks,
            truncated_note=budget.note(),
            searched_materials=len(materials),
            searched_material_names=[item.filename for item in materials],
        )

    return ResolvedContext(
        course_id=course_id,
        entity_type=AgentEntityType.ASSIGNMENT,
        entity_id=assignment_id,
        summary=summary,
        blocks=blocks,
        truncated_note=budget.note(),
    )


async def _resolve_course(
    session: AsyncSession,
    *,
    course_id: uuid.UUID,
    question: str,
    is_summarize: bool,
    settings: Settings,
    budget: _Budget,
) -> ResolvedContext:
    course = (await session.execute(_COURSE_SQL, {"course_id": course_id})).first()
    if course is None:  # pragma: no cover - 会话存在即课程存在
        raise ResourceNotFoundError()

    materials = await _list_ready_materials(session, course_id=course_id)

    summary_parts = [f"课程：{course.name}"]
    if course.description:
        summary_parts.append(f"课程说明：{course.description}")
    summary_parts.append(f"已解析资料：{len(materials)} 份")
    summary = "\n".join(summary_parts)

    blocks: list[ContextBlock] = []
    if is_summarize:
        # 「总结课程」用**逐份资料的章节大纲**做一级归纳，而不是拿"总结课程"
        # 这四个字去搜索（那样只会命中偶然含有该词的片段）。
        # 这是 Map/Reduce 的第一级；第二级由模型在提示词内完成。
        names: list[uuid.UUID] = []
        for material in materials[: settings.agent_course_summary_max_materials]:
            before = len(blocks)
            await _append_outline_blocks(
                session,
                budget=budget,
                blocks=blocks,
                material_row=material,
                only_section_id=None,
            )
            if len(blocks) > before:
                names.append(material.id)
    else:
        retrieved = await search_chunks(
            session,
            course_id=course_id,
            question=question,
            limit=settings.agent_max_retrieved_chunks,
            hard_limit=settings.agent_max_recall_chunks,
        )
        # 多份资料同时命中时按资料轮转，避免长资料独占上下文（评审文档「一、#6」）
        await _append_chunk_blocks(
            session,
            budget=budget,
            blocks=blocks,
            chunks=merge_round_robin(retrieved),
        )

    return ResolvedContext(
        course_id=course_id,
        entity_type=AgentEntityType.COURSE,
        entity_id=course_id,
        summary=summary,
        blocks=blocks,
        truncated_note=budget.note(),
        searched_materials=len(materials),
        searched_material_names=[item.filename for item in materials],
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


def build_no_evidence_message(context: ResolvedContext, question: str) -> str:
    """确实没有依据时的说明文案（评审文档「一、#1」/「二、4」）。

    以前所有失败都返回同一句"课程资料中未找到依据"，用户无法判断是"课程里
    没有这份资料"还是"我换一种说法就能问到"。这里按**真实检索范围**给出说明：
    检索了几份资料、资料名是什么、卡在哪一步。
    """
    if context.searched_materials <= 0:
        return (
            "这门课程还没有解析完成的资料，我暂时没有可以引用的内容。"
            "可以先把课件上传并等待解析完成，再来提问。"
        )

    names = context.searched_material_names
    shown = "、".join(f"《{name}》" for name in names[:3])
    suffix = "等" if len(names) > 3 else ""
    keyword = question.strip().replace("\n", " ")[:30]
    if keyword:
        return (
            f"我检索了这门课程的 {context.searched_materials} 份资料（{shown}{suffix}），"
            f"但没有找到与「{keyword}」相关的依据。"
            "可以换一种说法，或者确认相关课件是否已经上传并解析完成。"
        )
    return (
        f"我检索了这门课程的 {context.searched_materials} 份资料（{shown}{suffix}），"
        "但没有找到与本次问题相关的依据。"
    )


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
    action: AgentRunAction,
    input_message_id: uuid.UUID | None,
    is_staff: bool,
    settings: Settings,
) -> ResolvedContext:
    """解析本次 Run 的上下文。

    预算按**整次请求**计算：先扣掉历史、选中文本与固定提示词的份额，
    剩下的才是来源块的额度（评审文档「一、#7」）。

    :param action: 决定注入策略（``SUMMARIZE_CONTEXT`` 不做关键词检索）
    :param input_message_id: 本次提问的消息 ID，历史查询会排除它，避免重复注入
    :param is_staff: 当前用户是否为本课程教师（决定作业可见性）
    :raises ResourceNotFoundError: 实体不存在、不属于会话课程或对学生不可见
    :raises AgentContextNotReadyError: 资料尚未解析完成
    :raises AgentContextUnsupportedError: 该实体类型尚未实现
    """
    trimmed_selected = (
        selected_text[: settings.agent_selected_text_max_chars] if selected_text else None
    )
    history = await _load_history(
        session,
        session_id=session_id,
        limit=settings.agent_history_max_messages,
        exclude_message_id=input_message_id,
    )

    budget = _Budget(settings.agent_context_max_chars)
    budget.reserve(PROMPT_OVERHEAD_RESERVE_CHARS + SUMMARY_RESERVE_CHARS)
    budget.reserve(sum(len(content) for _, content in history))
    if trimmed_selected:
        budget.reserve(len(trimmed_selected))

    is_summarize = action is AgentRunAction.SUMMARIZE_CONTEXT

    if entity_type is None or entity_type is AgentEntityType.COURSE:
        resolved = await _resolve_course(
            session,
            course_id=course_id,
            question=question,
            is_summarize=is_summarize,
            settings=settings,
            budget=budget,
        )
    elif entity_type in (AgentEntityType.MATERIAL, AgentEntityType.MATERIAL_SECTION):
        if entity_id is None:
            raise AgentContextUnsupportedError("MATERIAL 上下文必须提供 entity_id")
        resolved = await _resolve_material(
            session,
            course_id=course_id,
            material_id=entity_id,
            section_id=section_id,
            question=question,
            is_summarize=is_summarize,
            settings=settings,
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
            retrieve_materials=action is AgentRunAction.ASK,
            is_staff=is_staff,
            settings=settings,
            budget=budget,
        )
    else:
        # SUBMISSION / GRADE 等：明确告知未支持，不假装已实现（契约 6.3）
        raise AgentContextUnsupportedError(
            f"{entity_type.value} 上下文尚未实现，等对应领域模块落地后再开放"
        )

    if trimmed_selected:
        # 用户附加文本是不可信材料，单独成块并明确标注来源；
        # 它不能支撑"有依据"的结论，也不能成为引用
        resolved.blocks.append(
            _block(
                source_type=AgentSourceType.COURSE,
                source_id=session_id,
                label="用户选中的文本（由用户提供，不是课程资料）",
                text=trimmed_selected,
                groundable=False,
                display_kind=None,
            )
        )

    resolved.history = history
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
            groundable=block.groundable,
            display_kind=block.display_kind,
        )
        for index, block in enumerate(resolved.blocks, start=1)
    ]
    return resolved
