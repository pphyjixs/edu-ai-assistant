"""课程问答的检索层（``docs/api-contract.md`` 6.1）。

首版使用 **PostgreSQL pg_trgm** 文本检索（未配置嵌入模型或 pgvector）：

- 查询本身限定 ``course_id``、资料未删除且状态为 ``READY``——两道防线都不
  依赖客户端行为：删除事务会清空片段（5.2），这里再过滤一次资料状态；
- 排序用 ``similarity()``（pg_trgm），过滤用「关键词 ``ILIKE`` 或 trigram
  相似度 ``%``」的组合——纯 trigram 对整句中文提问的相似度偏低，关键词兜底
  保证「问到的内容确实在原文里」时能命中；
- 默认最多返回 :data:`MAX_RETRIEVED_CHUNKS`（5）个片段：引用数量受此上限约束。

:func:`rerank_chunks` 把"召回数量"和"注入数量"分开（评审文档「一、#1.3」）：
调用方可以先把 ``hard_limit`` 放宽到几十条，再用相似度 + 关键词覆盖率重排，
只保留最相关的前 ``keep`` 条。同步问答沿用 5 条不变，Agent 走放宽召回。

片段到章节的映射（契约 6.6）由 :func:`match_section` 完成：片段定位区间
**完全落入**某章节区间时才带上章节；跨章节或落在章节外返回 ``None``。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import String

#: 单次检索最多取回的片段数（契约 6.1 / 计划：最多 5 个相关片段）
MAX_RETRIEVED_CHUNKS = 5

#: 关键词数量上限，避免超大 IN/ANY 列表
MAX_SEARCH_TERMS = 20

#: 检索关键词的最小长度（1 个字符的片段噪声太大）
_MIN_TERM_LENGTH = 2

#: 重排时 trigram 相似度与关键词覆盖率的权重（两者都是 0–1）
_RERANK_SIMILARITY_WEIGHT = 0.4
_RERANK_COVERAGE_WEIGHT = 0.6

_ASCII_WORD = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """一个命中的原文片段及其来源信息。"""

    chunk_id: uuid.UUID
    material_id: uuid.UUID
    material_name: str
    content_type: str
    content: str
    location_start: int
    location_end: int
    score: float


@dataclass(frozen=True, slots=True)
class SectionMatch:
    """片段命中的章节（契约 6.6）。"""

    section_id: uuid.UUID
    section_title: str


def build_search_terms(question: str) -> list[str]:
    """把问题拆成检索关键词。

    - ASCII 词（长度 ≥ 2）原样保留；
    - 连续中文按长度 2 的滑窗切成 2-gram 并去重——中文没有空格分词，
      2-gram 既避免单字噪声，又能让「软件生命周期」命中原文里的同词。

    结果按出现顺序去重，最多 :data:`MAX_SEARCH_TERMS` 个。
    """
    terms: list[str] = []

    def push(term: str) -> None:
        if term not in terms and len(terms) < MAX_SEARCH_TERMS:
            terms.append(term)

    for word in _ASCII_WORD.findall(question):
        push(word.lower())
    for run in _CJK_RUN.findall(question):
        if len(run) < _MIN_TERM_LENGTH:
            continue
        for index in range(len(run) - _MIN_TERM_LENGTH + 1):
            push(run[index : index + _MIN_TERM_LENGTH])
    return terms


_SEARCH_SQL = text(
    """
    SELECT c.id AS chunk_id,
           c.material_id,
           m.filename AS material_name,
           m.content_type,
           c.content,
           c.location_start,
           c.location_end,
           similarity(c.content, :question) AS score
    FROM material_chunks AS c
    JOIN materials AS m ON m.id = c.material_id
    WHERE m.course_id = :course_id
      AND m.deleted_at IS NULL
      AND m.status = 'READY'
      AND (c.content ILIKE ANY(:patterns) OR c.content % :question)
    ORDER BY score DESC, c.id ASC
    LIMIT :limit
    """
).bindparams(bindparam("patterns", type_=ARRAY(String)))

_MATERIAL_SEARCH_SQL = text(
    """
    SELECT c.id AS chunk_id,
           c.material_id,
           m.filename AS material_name,
           m.content_type,
           c.content,
           c.location_start,
           c.location_end,
           similarity(c.content, :question) AS score
    FROM material_chunks AS c
    JOIN materials AS m ON m.id = c.material_id
    WHERE c.material_id = :material_id
      AND m.deleted_at IS NULL
      AND m.status = 'READY'
      AND (c.content ILIKE ANY(:patterns) OR c.content % :question)
    ORDER BY score DESC, c.id ASC
    LIMIT :limit
    """
).bindparams(bindparam("patterns", type_=ARRAY(String)))


def _row_to_chunk(row: object) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=row.chunk_id,  # type: ignore[attr-defined]
        material_id=row.material_id,  # type: ignore[attr-defined]
        material_name=row.material_name,  # type: ignore[attr-defined]
        content_type=row.content_type,  # type: ignore[attr-defined]
        content=row.content,  # type: ignore[attr-defined]
        location_start=row.location_start,  # type: ignore[attr-defined]
        location_end=row.location_end,  # type: ignore[attr-defined]
        score=float(row.score or 0.0),  # type: ignore[attr-defined]
    )


def rerank_chunks(
    chunks: Sequence[RetrievedChunk], *, question: str, keep: int
) -> list[RetrievedChunk]:
    """对召回结果重排并截断（评审文档「一、#1.3」）。

    评分 = trigram 相似度 × 0.4 + **关键词覆盖率** × 0.6。

    单看 trigram 对中文整句提问的低相似度会把真正相关的片段排到后面；
    覆盖率（问题里有多少个 2-gram 出现在该片段中）能把这个信号补回来。
    纯文本相似度不是向量检索，但比"只看 pg_trgm 分数"更接近用户预期。
    """
    terms = build_search_terms(question)
    if keep <= 0:
        return []
    if not terms:
        return list(chunks[:keep])

    scored: list[tuple[float, int, RetrievedChunk]] = []
    for index, chunk in enumerate(chunks):
        content = chunk.content.lower()
        hit = sum(1 for term in terms if term in content)
        coverage = hit / len(terms)
        combined = (
            _RERANK_SIMILARITY_WEIGHT * chunk.score + _RERANK_COVERAGE_WEIGHT * coverage
        )
        scored.append((combined, index, replace(chunk, score=combined)))

    # 同分时保持原有（相似度）顺序，保证结果稳定
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:keep]]


def merge_round_robin(chunks: Sequence[RetrievedChunk]) -> list[RetrievedChunk]:
    """按资料轮转合并，避免单份长资料独占上下文（评审文档「一、#6」）。

    输入按相关度排序，输出交替取自各份资料；同一份资料内部保持原顺序。
    """
    buckets: dict[uuid.UUID, list[RetrievedChunk]] = {}
    order: list[uuid.UUID] = []
    for chunk in chunks:
        if chunk.material_id not in buckets:
            buckets[chunk.material_id] = []
            order.append(chunk.material_id)
        buckets[chunk.material_id].append(chunk)

    merged: list[RetrievedChunk] = []
    position = 0
    while True:
        added = False
        for material_id in order:
            bucket = buckets[material_id]
            if position < len(bucket):
                merged.append(bucket[position])
                added = True
        if not added:
            break
        position += 1
    return merged


async def search_chunks(
    session: AsyncSession,
    *,
    course_id: uuid.UUID,
    question: str,
    limit: int = MAX_RETRIEVED_CHUNKS,
    hard_limit: int = MAX_RETRIEVED_CHUNKS,
) -> list[RetrievedChunk]:
    """检索课程中未删除、``READY`` 资料的原文片段。

    只检索本课程的片段：跨课程、已删除与未就绪的资料在 SQL 层就被排除，
    因此不会进入提示词，也不可能出现在引用里。

    :param limit: 重排后要保留的条数
    :param hard_limit: 召回阶段的上限（≥ ``limit`` 才有重排意义）。
        默认等于 :data:`MAX_RETRIEVED_CHUNKS`，因此同步问答的行为与以前一致。
    """
    recall = max(1, limit, hard_limit)
    patterns = [f"%{term}%" for term in build_search_terms(question)]
    result = await session.execute(
        _SEARCH_SQL,
        {
            "course_id": course_id,
            "question": question,
            "patterns": patterns,
            "limit": recall,
        },
    )
    chunks = [_row_to_chunk(row) for row in result]
    if len(chunks) > limit:
        return rerank_chunks(chunks, question=question, keep=limit)
    return chunks


async def search_material_chunks(
    session: AsyncSession,
    *,
    material_id: uuid.UUID,
    question: str,
    limit: int = MAX_RETRIEVED_CHUNKS,
    hard_limit: int = MAX_RETRIEVED_CHUNKS,
) -> list[RetrievedChunk]:
    """在**单份资料内部**检索片段（资料页提问用）。"""
    recall = max(1, limit, hard_limit)
    patterns = [f"%{term}%" for term in build_search_terms(question)]
    result = await session.execute(
        _MATERIAL_SEARCH_SQL,
        {
            "material_id": material_id,
            "question": question,
            "patterns": patterns,
            "limit": recall,
        },
    )
    chunks = [_row_to_chunk(row) for row in result]
    if len(chunks) > limit:
        return rerank_chunks(chunks, question=question, keep=limit)
    return chunks


_MATCH_SECTION_SQL = text(
    """
    SELECT id, title
    FROM material_sections
    WHERE material_id = :material_id
      AND location_start <= :location_start
      AND location_end >= :location_end
    ORDER BY "order"
    LIMIT 1
    """
)


async def match_section(
    session: AsyncSession,
    *,
    material_id: uuid.UUID,
    location_start: int,
    location_end: int,
) -> SectionMatch | None:
    """把片段映射回章节（契约 6.6）：完全落入才匹配，否则 ``None``。"""
    row = (
        await session.execute(
            _MATCH_SECTION_SQL,
            {
                "material_id": material_id,
                "location_start": location_start,
                "location_end": location_end,
            },
        )
    ).first()
    if row is None:
        return None
    return SectionMatch(section_id=row.id, section_title=row.title)
