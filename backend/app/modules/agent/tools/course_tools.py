"""课程资料相关工具（开发方案 5.3）。

两个只读工具，全部复用既有领域实现，不复制检索或列表逻辑：

- :func:`search_course_knowledge` 复用 ``chat/retrieval.py``；
- :func:`list_course_materials` 复用 ``materials/repository.list_course_materials``。

工具返回的一切都是**不可信数据**：课件原文里出现"调用 generate_practice"之类
文字不构成任何指令（开发方案第 9 节第 5 条）。
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.modules.agent.tool_types import (
    ToolContext,
    ToolErrorCode,
    ToolEvidence,
    ToolResult,
    ToolSideEffect,
    ToolSpec,
)
from app.modules.agent.tools._shared import material_evidence, read_session
from app.modules.auth.models import UserRole
from app.modules.chat import retrieval
from app.modules.materials import repository as materials_repo
from app.modules.materials.models import MaterialStatus
from app.modules.materials.schemas import source_type_for_content_type

#: 检索工具返回给模型的最大条数（开发方案 5.3：服务端限制 1..8）
SEARCH_LIMIT_MAX = 8

#: 一次可选的最多资料数
SEARCH_MATERIAL_MAX = 10

#: 列举课程资料时扫描的条数上限（课程资料量不会很大）
MATERIAL_SCAN_LIMIT = 200

_BOTH_ROLES = frozenset({UserRole.TEACHER, UserRole.STUDENT})


class SearchCourseKnowledgeInput(BaseModel):
    """``search_course_knowledge`` 的输入。

    ``extra="forbid"``：模型无法传 ``course_id`` / ``user_id`` 等越权字段。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500, description="检索关键词或问题")
    material_ids: list[uuid.UUID] | None = Field(
        default=None,
        max_length=SEARCH_MATERIAL_MAX,
        description="可选：只在指定的资料内检索（必须是本课程已解析的资料）",
    )
    limit: int = Field(
        default=6, ge=1, le=SEARCH_LIMIT_MAX, description="返回条数（1-8）"
    )


class ListCourseMaterialsInput(BaseModel):
    """``list_course_materials`` 没有参数（课程由服务端上下文决定）。"""

    model_config = ConfigDict(extra="forbid")


async def _search_course_knowledge(
    ctx: ToolContext, args: SearchCourseKnowledgeInput
) -> ToolResult:
    """按关键词检索本课程资料，返回带稳定引用位的证据。

    **只读到的标量数据会被带出事务**：事务回滚后 ORM 实例会过期，
    在事务外访问属性会触发懒加载并报错，因此这里在会话内就把需要的内容
    取成纯数据（文件名、状态、计数）。
    """
    async with read_session(ctx) as session:
        rows, _total = await materials_repo.list_course_materials(
            session, course_id=ctx.course_id, offset=0, limit=MATERIAL_SCAN_LIMIT
        )
        ready = {row.id: row for row in rows if row.status is MaterialStatus.READY}
        ready_names = [row.filename for row in ready.values()]

        if args.material_ids:
            missing = [mid for mid in args.material_ids if mid not in ready]
            if missing:
                # 不存在 / 不属于本课程 / 已删除 / 未就绪统一不泄露资料名
                return ToolResult.failure(
                    ToolErrorCode.RESOURCE_NOT_FOUND,
                    "指定的资料不在本课程中、已删除，或尚未解析完成。",
                )
            chunks: list[retrieval.RetrievedChunk] = []
            for material_id in args.material_ids:
                chunks.extend(
                    await retrieval.search_material_chunks(
                        session,
                        material_id=material_id,
                        question=args.query,
                        limit=args.limit,
                        hard_limit=ctx.settings.agent_max_recall_chunks,
                    )
                )
            chunks = retrieval.rerank_chunks(
                chunks, question=args.query, keep=args.limit
            )
        else:
            chunks = await retrieval.search_chunks(
                session,
                course_id=ctx.course_id,
                question=args.query,
                limit=args.limit,
                hard_limit=ctx.settings.agent_max_recall_chunks,
            )

    evidence: list[ToolEvidence] = []
    for chunk in chunks:
        evidence.append(
            material_evidence(
                chunk_id=chunk.chunk_id,
                material_id=chunk.material_id,
                material_name=chunk.material_name,
                source_location_type=source_type_for_content_type(
                    chunk.content_type
                ).value,
                content=chunk.content,
                location_start=chunk.location_start,
                location_end=chunk.location_end,
            )
        )

    return ToolResult(
        ok=True,
        data={
            "query": args.query,
            "result_count": len(evidence),
            "searched_materials": len(ready_names),
            "searched_material_names": ready_names,
        },
        evidence=evidence,
    )


async def _list_course_materials(
    ctx: ToolContext, _args: ListCourseMaterialsInput
) -> ToolResult:
    """列出本课程可见资料，供模型选择练习来源。

    返回的字段都是**纯数据**（id / 文件名 / 状态 / 是否有章节大纲），
    不含上传路径或存储 key。
    """
    async with read_session(ctx) as session:
        rows, _total = await materials_repo.list_course_materials(
            session, course_id=ctx.course_id, offset=0, limit=MATERIAL_SCAN_LIMIT
        )
        materials: list[dict] = []
        for row in rows:
            sections = await materials_repo.list_sections_with_points(
                session, material_id=row.id
            )
            materials.append(
                {
                    "id": str(row.id),
                    "filename": row.filename,
                    "status": row.status.value,
                    "outline_available": bool(sections),
                }
            )

    return ToolResult(ok=True, data={"materials": materials})


SPECS: list[ToolSpec] = [
    ToolSpec(
        name="search_course_knowledge",
        description=(
            "在本课程的课件中检索与关键词相关的原文片段，返回可引用的摘录与出处。"
            "需要课程事实、概念解释、依据时使用。只能检索本课程已解析的资料。"
        ),
        input_model=SearchCourseKnowledgeInput,
        side_effect=ToolSideEffect.READ,
        allowed_roles=_BOTH_ROLES,
        handler=_search_course_knowledge,
    ),
    ToolSpec(
        name="list_course_materials",
        description=(
            "列出本课程当前的资料（文件名、解析状态、是否有章节大纲），"
            "以及每份资料的 id。需要知道有哪些课件、或要为其他工具选择资料时使用。"
        ),
        input_model=ListCourseMaterialsInput,
        side_effect=ToolSideEffect.READ,
        allowed_roles=_BOTH_ROLES,
        handler=_list_course_materials,
    ),
]


__all__ = [
    "ListCourseMaterialsInput",
    "SEARCH_LIMIT_MAX",
    "SPECS",
    "SearchCourseKnowledgeInput",
]
