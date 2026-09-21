"""课程问答的检索层（``docs/api-contract.md`` 6.1）。

首版使用 **PostgreSQL pg_trgm** 文本检索（未配置嵌入模型或 pgvector）：

- 查询本身限定 ``course_id``、资料未删除且状态为 ``READY``——两道防线都不
  依赖客户端行为：删除事务会清空片段（5.2），这里再过滤一次资料状态；
- 排序用 ``similarity()``（pg_trgm），过滤用「关键词 ``ILIKE`` 或 trigram
  相似度 ``%``」的组合——纯 trigram 对整句中文提问的相似度偏低，关键词兜底
  保证「问到的内容确实在原文里」时能命中；
- 最多返回 :data:`MAX_RETRIEVED_CHUNKS`（5）个片段：引用数量受此上限约束。

片段到章节的映射（契约 6.6）由 :func:`match_section` 完成：片段定位区间
**完全落入**某章节区间时才带上章节；跨章节或落在章节外返回 ``None``。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

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


async def search_chunks(
    session: AsyncSession,
    *,
    course_id: uuid.UUID,
    question: str,
    limit: int = MAX_RETRIEVED_CHUNKS,
) -> list[RetrievedChunk]:
    """检索课程中未删除、``READY`` 资料的原文片段。

    只检索本课程的片段：跨课程、已删除与未就绪的资料在 SQL 层就被排除，
    因此不会进入提示词，也不可能出现在引用里。
    """
    patterns = [f"%{term}%" for term in build_search_terms(question)]
    result = await session.execute(
        _SEARCH_SQL,
        {
            "course_id": course_id,
            "question": question,
            "patterns": patterns,
            "limit": max(1, min(limit, MAX_RETRIEVED_CHUNKS)),
        },
    )
    return [
        RetrievedChunk(
            chunk_id=row.chunk_id,
            material_id=row.material_id,
            material_name=row.material_name,
            content_type=row.content_type,
            content=row.content,
            location_start=row.location_start,
            location_end=row.location_end,
            score=float(row.score or 0.0),
        )
        for row in result
    ]


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
