"""Chat 业务规则与事务边界（``docs/api-contract.md`` 第 6 节）。

关键约定：

- **可见性统一 404**：课程不存在/非成员、会话不存在/非所有者都返回同一个
  ``RESOURCE_NOT_FOUND``，不区分「不存在」与「不可见」（契约 6.1）。
- **归档只禁止写入**：创建会话与发送问题返回 ``409 COURSE_ARCHIVED``，
  会话列表与消息列表仍可读。
- **模型调用不持有数据库事务**：发送问题先做只读检索，再在**事务外**调用
  模型（在线程池中执行同步 HTTP），校验通过后才开启写入事务。
- **一问一答同事务写入**：成功路径在一个事务内递增会话版本、写入用户消息、
  助手消息、引用与生成尝试记录；失败路径只留尝试记录，不新增任何消息。
- **并发保护**：写入前用会话 ``version`` 做乐观锁（:func:`repository.bump_session_version`），
  版本不匹配返回新增的 ``409 CHAT_CONFLICT``，不会留下半组消息。
- **引用资料状态复查**：写库前再查一次引用资料的当前状态，只保留仍为
  ``READY`` 且未删除的资料；全部失效时按无依据处理（契约 6.1）。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    AiJobFailedError,
    ChatConflictError,
    CourseArchivedError,
    ResourceNotFoundError,
    ServiceUnavailableError,
)
from app.core.pagination import PaginationParams
from app.core.time import utc_now
from app.modules.auth.models import User
from app.modules.chat import answer_ai, retrieval
from app.modules.chat import repository as repo
from app.modules.chat.models import (
    ChatAttemptStatus,
    ChatGenerationAttempt,
    ChatMessage,
    ChatMessageCitation,
    ChatMessageRole,
    ChatSession,
    CitationSourceKind,
)
from app.modules.chat.schemas import NO_EVIDENCE_ANSWER, Citation
from app.modules.courses import service as courses_service
from app.modules.materials import repository as materials_repo
from app.modules.materials.schemas import source_type_for_content_type

logger = logging.getLogger("app.chat.service")

#: 生成 AI 客户端的工厂（测试注入本地假模型 HTTP 服务）
AiClientFactory = Callable[[], object]

#: 失败摘要的安全文案（不含提示词、原文与模型地址）
_MODEL_FAILED_MESSAGE = "回答生成失败，请稍后重试"


async def create_session(
    session: AsyncSession,
    *,
    user: User,
    course_id: uuid.UUID,
    now: datetime | None = None,
) -> ChatSession:
    """创建会话（契约 6.2）：课程成员均可，归档课程 409。

    成员检查与归档检查都在**课程行锁**内完成，写入与检查处于同一事务：
    与并发归档按事务顺序得到一致结果（先归档则 409；先创建则会话存在且
    归档随后生效），不会出现"读到 ACTIVE 后归档已提交仍写入"的窗口。
    """
    created_at = now or utc_now()
    course = await courses_service.lock_member_course(
        session, user_id=user.id, course_id=course_id
    )
    try:
        courses_service.require_course_active(course)
    except CourseArchivedError:
        await session.rollback()
        raise

    chat_session = repo.create_session(
        session,
        session_id=uuid.uuid4(),
        course_id=course.id,
        user_id=user.id,
        now=created_at,
    )
    await session.commit()
    return chat_session


async def list_sessions(
    session: AsyncSession,
    *,
    user: User,
    course_id: uuid.UUID,
    pagination: PaginationParams,
) -> tuple[list[ChatSession], int]:
    """我的会话列表（契约 6.3）：只返回当前用户自己的会话。"""
    await courses_service.require_member_course(
        session, user=user, course_id=course_id
    )
    return await repo.list_owned_sessions(
        session,
        course_id=course_id,
        user_id=user.id,
        offset=pagination.offset,
        limit=pagination.limit,
    )


async def _require_owned_session(
    session: AsyncSession, *, user: User, session_id: uuid.UUID
) -> ChatSession:
    """会话所有者检查：不存在或不是所有者统一 404（契约 6.1）。"""
    chat_session = await repo.get_owned_session(
        session, session_id=session_id, user_id=user.id
    )
    if chat_session is None:
        raise ResourceNotFoundError()
    return chat_session


async def list_messages(
    session: AsyncSession,
    *,
    user: User,
    session_id: uuid.UUID,
    pagination: PaginationParams,
) -> tuple[list[tuple[ChatMessage, list[ChatMessageCitation]]], int]:
    """会话消息（契约 6.4）：仅会话所有者可读，按时间升序。"""
    await _require_owned_session(session, user=user, session_id=session_id)
    messages, total = await repo.list_messages(
        session,
        session_id=session_id,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    citations = await repo.list_citations_for_messages(
        session, message_ids=[message.id for message in messages]
    )
    return [(message, citations.get(message.id, [])) for message in messages], total


async def _build_citations(
    session: AsyncSession,
    *,
    validated: answer_ai.ValidatedAnswer,
) -> list[tuple[answer_ai.ValidatedCitation, Citation]]:
    """把校验后的引用转成待落库的引用记录（含章节匹配与来源类型）。

    先按 ID 升序对引用资料加**共享锁**（契约 6.1）：这些资料在本次事务提交前
    不会被并发删除，因此落库的引用一定指向仍然存在且 ``READY`` 的资料；
    生成期间已失效的资料会被排除，全部失效时按无依据处理。
    """
    live_ids = await materials_repo.lock_live_materials(
        session, material_ids=[item.chunk.material_id for item in validated.citations]
    )
    built: list[tuple[answer_ai.ValidatedCitation, Citation]] = []
    for item in validated.citations:
        chunk = item.chunk
        if chunk.material_id not in live_ids:
            # 生成期间资料被删除或不再 READY：丢弃该引用（契约 6.1）
            logger.info("引用资料已失效，已丢弃该引用（material_id=%s）", chunk.material_id)
            continue
        section = await retrieval.match_section(
            session,
            material_id=chunk.material_id,
            location_start=chunk.location_start,
            location_end=chunk.location_end,
        )
        source_type = source_type_for_content_type(chunk.content_type)
        built.append(
            (
                item,
                Citation(
                    source_kind=CitationSourceKind.MATERIAL.value,
                    source_id=chunk.material_id,
                    source_label=chunk.material_name,
                    material_id=chunk.material_id,
                    material_name=chunk.material_name,
                    section_id=section.section_id if section else None,
                    section_title=section.section_title if section else None,
                    source_type=source_type.value,
                    location_start=chunk.location_start,
                    location_end=chunk.location_end,
                    page=(
                        chunk.location_start
                        if source_type.value == "PDF_PAGE"
                        else None
                    ),
                    quote=item.quote,
                ),
            )
        )
    return built


def _record_attempt(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    status: ChatAttemptStatus,
    settings: Settings,
    retrieved_count: int,
    grounded: bool | None,
    duration_ms: int,
    error: str | None,
    now: datetime,
) -> ChatGenerationAttempt:
    """写一条生成尝试记录（成功与失败都留痕）。

    只接收**标量 ID**：失败路径在只读事务已结束、模型调用失败之后执行，
    若在这里访问 ORM 对象会因对象过期触发同步懒加载（``MissingGreenlet``）。
    """
    return repo.add_attempt(
        session,
        attempt_id=uuid.uuid4(),
        session_id=session_id,
        user_id=user_id,
        status=status,
        model=settings.ai_model.strip() or None,
        prompt_version=answer_ai.PROMPT_VERSION,
        retrieved_count=retrieved_count,
        grounded=grounded,
        duration_ms=duration_ms,
        error=error,
        now=now,
    )


async def _record_failed_attempt(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    settings: Settings,
    retrieved_count: int,
    duration_ms: int,
    error: str,
    now: datetime | None,
) -> None:
    """失败路径的尝试记录：**新事务**写入，只用标量 ID（不碰过期 ORM 对象）。"""
    _record_attempt(
        session,
        session_id=session_id,
        user_id=user_id,
        status=ChatAttemptStatus.FAILED,
        settings=settings,
        retrieved_count=retrieved_count,
        grounded=None,
        duration_ms=duration_ms,
        error=error[:500],
        now=now or utc_now(),
    )
    await session.commit()


async def _generate_validated(
    *,
    content: str,
    chunks: list[retrieval.RetrievedChunk],
    settings: Settings,
    ai_client_factory: AiClientFactory | None,
) -> answer_ai.ValidatedAnswer:
    """在线程池中调用模型适配层；客户端由工厂提供（测试注入假模型服务）。"""
    ai_client: object | None = None
    try:
        if ai_client_factory is not None:
            ai_client = ai_client_factory()
        return await asyncio.to_thread(
            answer_ai.generate_answer,
            chunks,
            question=content,
            base_url=settings.ai_base_url,
            api_key=settings.ai_api_key,
            model=settings.ai_model,
            timeout_seconds=settings.ai_timeout_seconds,
            client=ai_client,
        )
    finally:
        close = getattr(ai_client, "close", None)
        if close is not None:
            close()


async def send_question(
    session: AsyncSession,
    *,
    user: User,
    session_id: uuid.UUID,
    content: str,
    settings: Settings,
    ai_client_factory: AiClientFactory | None = None,
    now: datetime | None = None,
) -> ChatMessage:
    """发送问题并生成受约束的回答（契约 6.5）。

    返回**助手消息**；同一事务内写入的用户消息可在 6.4 中读到。

    :raises ResourceNotFoundError: 会话不存在或不是所有者（404）。
    :raises CourseArchivedError: 课程已归档（409）。
    :raises ChatConflictError: 生成期间会话被并发修改（409 CHAT_CONFLICT）。
    :raises ServiceUnavailableError: 模型端点/模型名未配置（503）。
    :raises AiJobFailedError: 模型调用或输出失败（502，不落库消息）。
    """
    # ---- 阶段一：只读检查 + 检索；随后**结束只读事务** ----
    user_id = user.id
    chat_session = await _require_owned_session(
        session, user=user, session_id=session_id
    )
    course_id = chat_session.course_id
    expected_version = chat_session.version

    course = await courses_service.require_member_course(
        session, user=user, course_id=course_id
    )
    courses_service.require_course_active(course)

    chunks = await retrieval.search_chunks(
        session, course_id=course_id, question=content
    )
    # 只保留标量与会话无关的片段数据，然后结束只读事务：
    # 模型调用（可能数十秒）期间不持有任何数据库事务与连接。
    await session.rollback()
    logger.info("问答检索完成（session=%s 片段=%d）", session_id, len(chunks))

    # ---- 阶段二：事务外调用模型（只在真正需要模型时才检查模型配置）----
    started = time.monotonic()
    if not chunks:
        # 没有可引用的片段：按契约 6.1 直接给出"无依据"结果，
        # 不调用模型，也不因模型未配置而返回 503。
        validated = answer_ai.ValidatedAnswer(
            content=NO_EVIDENCE_ANSWER, grounded=False, citations=[]
        )
        duration_ms = int((time.monotonic() - started) * 1000)
    else:
        try:
            validated = await _generate_validated(
                content=content,
                chunks=chunks,
                settings=settings,
                ai_client_factory=ai_client_factory,
            )
        except answer_ai.AnswerModelNotConfiguredError as exc:
            await _record_failed_attempt(
                session,
                session_id=session_id,
                user_id=user_id,
                settings=settings,
                retrieved_count=len(chunks),
                duration_ms=int((time.monotonic() - started) * 1000),
                error="模型服务未配置",
                now=now,
            )
            logger.warning("问答模型未配置：%s", exc)
            raise ServiceUnavailableError(
                "问答服务暂不可用，请稍后重试",
                details={"component": "ai"},
            ) from exc
        except answer_ai.AnswerGenerationError as exc:
            await _record_failed_attempt(
                session,
                session_id=session_id,
                user_id=user_id,
                settings=settings,
                retrieved_count=len(chunks),
                duration_ms=int((time.monotonic() - started) * 1000),
                error=str(exc)[:500],
                now=now,
            )
            logger.warning("问答生成失败（session=%s）：%s", session_id, exc)
            raise AiJobFailedError(_MODEL_FAILED_MESSAGE) from exc
        duration_ms = int((time.monotonic() - started) * 1000)

    # ---- 阶段三：写入事务（固定顺序：课程行锁 → 版本 → 引用资料锁）----
    written_at = now or utc_now()
    # 助手消息晚 1 毫秒：同一事务写入的一问一答在 created_at 升序排序下顺序稳定
    # （同值会退化为 id 排序，而 id 是随机 UUID）；会话的 last_message_at 取
    # 助手消息的实际时间（契约 6.6：最近一条消息的时间）。
    assistant_at = written_at + timedelta(milliseconds=1)

    # 1) 锁定课程行后检查成员与归档状态：与并发归档按事务顺序得到一致结果。
    #    生成期间先完成归档 → 这里读到 ARCHIVED → 409 且不写消息。
    course = await courses_service.lock_member_course(
        session, user_id=user_id, course_id=course_id
    )
    try:
        courses_service.require_course_active(course)
    except CourseArchivedError:
        await session.rollback()
        logger.info("课程在生成期间被归档，放弃写入（session=%s）", session_id)
        raise

    # 2) 校验会话版本：生成期间有并发写入则本次不落库任何消息。
    if not await repo.bump_session_version(
        session,
        session_id=session_id,
        expected_version=expected_version,
        now=assistant_at,
    ):
        await session.rollback()
        logger.warning("会话并发冲突，已放弃写入（session=%s）", session_id)
        raise ChatConflictError()

    # 3) 引用资料按 ID 升序加共享锁：提交前不会被并发删除；已失效的按无依据处理。
    citations = await _build_citations(session, validated=validated)
    grounded = bool(citations)
    assistant_content = validated.content if grounded else NO_EVIDENCE_ANSWER

    repo.add_message(
        session,
        message_id=uuid.uuid4(),
        session_id=session_id,
        role=ChatMessageRole.USER,
        content=content,
        grounded=None,
        now=written_at,
    )
    assistant_message = repo.add_message(
        session,
        message_id=uuid.uuid4(),
        session_id=session_id,
        role=ChatMessageRole.ASSISTANT,
        content=assistant_content,
        grounded=grounded,
        now=assistant_at,
    )
    await session.flush()
    for order, (_, citation) in enumerate(citations, start=1):
        repo.add_citation(
            session,
            citation_id=uuid.uuid4(),
            message_id=assistant_message.id,
            order=order,
            source_kind=citation.source_kind,
            source_id=citation.source_id,
            source_label=citation.source_label,
            material_id=citation.material_id,
            material_name=citation.material_name,
            section_id=citation.section_id,
            section_title=citation.section_title,
            source_type=citation.source_type,
            location_start=citation.location_start,
            location_end=citation.location_end,
            page=citation.page,
            quote=citation.quote,
            now=written_at,
        )
    _record_attempt(
        session,
        session_id=session_id,
        user_id=user_id,
        status=ChatAttemptStatus.SUCCEEDED,
        settings=settings,
        retrieved_count=len(chunks),
        grounded=grounded,
        duration_ms=duration_ms,
        error=None,
        now=written_at,
    )
    await session.commit()
    return assistant_message


__all__ = [
    "create_session",
    "list_messages",
    "list_sessions",
    "send_question",
]
