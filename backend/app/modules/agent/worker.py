"""Agent Run 的 Worker（``docs/local-development-agent-backend.md`` 第 6.8 节）。

流程与练习生成 Worker 一致：

```text
FOR UPDATE SKIP LOCKED 领取 PENDING 的 AGENT_RUN 任务
→ 写 RUNNING / run_token / lease_expires_at / attempts+1
→ 提交并释放数据库连接
→ 只读事务解析上下文（解析完立即结束事务）
→ 事务外调用模型（同步客户端放到线程池）
→ 心跳按租约 1/3 续租
→ 短事务回写：助手消息 + 引用 + 来源快照 + SUCCEEDED
→ 失败写安全摘要 + FAILED
```

三条硬约束：

1. **模型调用期间不持有任何数据库事务**（文档 6.1 第 4 条）；
2. 回写必须复查 ``run_token`` / ``RUNNING`` / 租约未过期，任一不满足即放弃写入
   （防止过期 Worker 覆盖新一轮执行）；
3. 失败摘要截断到 500 字符，且**不含**提示词、课件原文、模型地址或密钥。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.time import utc_now
from app.modules.agent import context as context_module
from app.modules.agent import generation_ai, orchestration, skills
from app.modules.agent import repository as repo
from app.modules.agent.models import (
    EVIDENCE_LEVEL_MAX_LENGTH,
    MODEL_NAME_MAX_LENGTH,
    ORCHESTRATOR_VERSION_MAX_LENGTH,
    PROMPT_VERSION_MAX_LENGTH,
    AgentEntityType,
    AgentRunAction,
    AgentSourceType,
)
from app.modules.agent.prompts import prompt_version_for
from app.modules.agent.tool_registry import ToolRegistry
from app.modules.agent.tool_types import AgentArtifact, ToolContext
from app.modules.agent.tools import build_default_registry
from app.modules.auth.models import User, UserRole
from app.modules.chat import repository as chat_repo
from app.modules.chat.models import (
    NON_MATERIAL_SOURCE_TYPE,
    ChatMessage,
    ChatMessageRole,
    ChatSession,
    CitationSourceKind,
)
from app.modules.chat.retrieval import match_section
from app.modules.courses.models import Course, CourseStatus
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import (
    Job,
    JobFailureStage,
    JobStatusValue,
    JobType,
)

logger = logging.getLogger("app.agent.worker")

#: 单批默认处理量
DEFAULT_BATCH_SIZE = 1

#: 失败摘要的落库长度上限（与 Job.error 列一致）
ERROR_MAX_LENGTH = 500

#: 心跳/续租的最大间隔（秒）。
#:
#: 心跳不只是续租，也是**取消检测**：租约只有 ``RUNNING`` 且令牌匹配时才会续期，
#: 因此一旦 Run 被取消或令牌被轮换，下一次心跳就会失败并中止执行。
#: 把它限制在几秒内，取消请求才能在合理时间内阻止后续模型轮次
#: （开发方案 10.2：Run 执行中被取消 → 后续模型轮次不再开始）。
HEARTBEAT_MAX_INTERVAL_SECONDS = 5.0

#: 模型客户端工厂：返回带 MockTransport 的 httpx.Client（测试注入）
AiClientFactory = Callable[[], object]

#: 进程内冻结的工具注册表与 Skill 目录（部署新 Skill 后重启 Worker 生效）
_tooling_cache: tuple[ToolRegistry, skills.SkillCatalog] | None = None


def _tooling() -> tuple[ToolRegistry, skills.SkillCatalog]:
    """返回本进程冻结的工具注册表与 Skill 目录。

    Skill 目录在第一次使用时扫描并缓存：部署新 Skill 后重启 Worker 即生效
    （开发方案第 6 节：目录在 Worker 启动时扫描并冻结）。
    """
    global _tooling_cache
    if _tooling_cache is None:
        catalog = skills.load_skill_catalog()
        registry = build_default_registry(catalog)
        _tooling_cache = (registry, catalog)
        logger.info(
            "Agent 工具注册表就绪（tools=%s skills=%s）",
            len(registry),
            len(catalog),
        )
    return _tooling_cache


@dataclass(slots=True)
class ClaimedAgentRun:
    """已领取的 Run。

    只携带**标量快照**：领取事务提交后 ORM 对象会被 expire，任何属性访问都会
    触发同步懒加载；同时避免把 ORM 对象带出事务边界。
    """

    job_id: uuid.UUID
    run_id: uuid.UUID
    session_id: uuid.UUID
    course_id: uuid.UUID
    user_id: uuid.UUID
    action: AgentRunAction
    entity_type: AgentEntityType | None
    entity_id: uuid.UUID | None
    section_id: uuid.UUID | None
    selected_text: str | None
    question: str
    run_token: str
    #: 本次提问的消息 ID：历史查询要排除它，避免同一句话进提示词两次
    input_message_id: uuid.UUID
    #: 发起者的平台角色；工具的角色校验需要它，且必须来自服务端而不是模型
    user_role: UserRole = UserRole.STUDENT
    #: ``options.output_language``，为空表示"与用户输入同语言"
    output_language: str | None = None
    #: 第几次领取（1 起）
    attempt: int = 1


def _safe_error(exc: BaseException) -> str:
    """把异常收敛成可安全展示的摘要（不泄露提示词、原文与密钥）。"""
    text = str(exc).strip() or type(exc).__name__
    return text[:ERROR_MAX_LENGTH]


async def claim_next(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    lease_seconds: int,
    max_attempts: int = 3,
) -> ClaimedAgentRun | None:
    """领取一个待执行的 Agent Run。

    领取条件覆盖两类任务（评审文档「一、#9」）：

    1. ``PENDING``：正常排队；
    2. ``RUNNING`` 且**租约已过期**：执行者崩溃或失联，允许新 Worker 接管，
       并生成新的 ``run_token``——旧执行者持有旧令牌，回写会被拒绝。

    尝试次数超过 ``max_attempts`` 的任务不会再被领取，而是直接置为 ``FAILED``，
    避免一条注定失败的任务在队列里无限重试、把 Run 永远留在 RUNNING。
    """
    # 内部循环：跳过"尝试次数已用尽"的任务，直到拿到一个可执行的任务或队列空
    for _ in range(8):
        async with session_factory() as session:
            result = await session.execute(
                select(Job)
                .where(
                    Job.type == JobType.AGENT_RUN,
                    or_(
                        Job.status == JobStatusValue.PENDING,
                        and_(
                            Job.status == JobStatusValue.RUNNING,
                            Job.lease_expires_at.is_not(None),
                            Job.lease_expires_at <= now,
                        ),
                    ),
                )
                .order_by(Job.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            job = result.scalar_one_or_none()
            if job is None:
                await session.rollback()
                return None

            if job.attempts >= max_attempts:
                job.status = JobStatusValue.FAILED
                job.error = (
                    f"任务已尝试 {job.attempts} 次仍未完成，已停止重试。可以重新提问。"
                )
                job.failure_stage = JobFailureStage.UNKNOWN.value
                job.finished_at = now
                await session.commit()
                logger.warning("任务尝试次数已用尽，置为 FAILED（job=%s）", job.id)
                continue

            run = await repo.get_run(session, job.resource_id)
            if run is None:
                # Run 被删除（会话级联删除）：任务作废，避免空转
                job.status = JobStatusValue.CANCELLED
                job.finished_at = now
                await session.commit()
                return None

            chat_session = await session.get(ChatSession, run.session_id)
            if chat_session is None:  # pragma: no cover - 外键保证存在
                job.status = JobStatusValue.CANCELLED
                job.finished_at = now
                await session.commit()
                return None

            # 本次 input 就是创建 Run 时保存的用户消息
            source_message = await session.get(ChatMessage, run.input_message_id)

            # 平台角色来自数据库，模型无法通过任何参数影响它
            run_user = await session.get(User, run.user_id)
            user_role = run_user.role if run_user is not None else UserRole.STUDENT

            run_token = secrets.token_hex(16)
            job.status = JobStatusValue.RUNNING
            job.started_at = job.started_at or now
            job.progress = 0
            job.attempts += 1
            job.run_token = run_token
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)

            options = run.options or {}
            raw_language = options.get("output_language")

            claimed = ClaimedAgentRun(
                job_id=job.id,
                run_id=run.id,
                session_id=run.session_id,
                course_id=chat_session.course_id,
                user_id=run.user_id,
                action=run.action,
                entity_type=run.entity_type,
                entity_id=run.entity_id,
                section_id=run.section_id,
                selected_text=run.selected_text,
                question=source_message.content if source_message is not None else "",
                run_token=run_token,
                input_message_id=run.input_message_id,
                user_role=user_role,
                output_language=raw_language if isinstance(raw_language, str) else None,
                attempt=job.attempts,
            )
            await session.commit()
            return claimed
    return None


async def run_still_active(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: uuid.UUID,
    run_token: str,
) -> bool:
    """Run 是否仍由**本执行者**持有（未被取消、也未被新一轮接管）。

    编排循环每轮开始前调用一次：只靠心跳续租来发现取消太慢——租约是分钟级，
    而取消应当尽快生效（开发方案 10.2）。
    """
    async with session_factory() as session:
        row = (
            await session.execute(select(Job.status, Job.run_token).where(Job.id == job_id))
        ).first()
    if row is None:
        return False
    return row[0] is JobStatusValue.RUNNING and row[1] == run_token


async def renew_lease(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: uuid.UUID,
    run_token: str,
    now: datetime,
    lease_seconds: int,
) -> bool:
    """续租：令牌不匹配或任务已非 ``RUNNING`` 时返回 ``False``（执行方应中止）。"""
    async with session_factory() as session:
        result = await session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.run_token == run_token,
                Job.status == JobStatusValue.RUNNING,
            )
            .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
        )
        await session.commit()
        return result.rowcount > 0


async def _write_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    claimed: ClaimedAgentRun,
    message: str,
    now: datetime,
    stage: JobFailureStage = JobFailureStage.UNKNOWN,
    orchestrator_version: str | None = None,
) -> None:
    """把任务置为 FAILED（仍要复查令牌与租约，避免覆盖新一轮执行）。

    ``stage`` 是失败阶段码，前端据此区分「模型侧失败」「工具调用失败」与
    「服务不可用」（评审文档「一、#9」要求所有冲突路径都落到可解释终态）。

    ``orchestrator_version`` 即使失败也要记录：否则无法判断一次异常失败发生在
    哪一版编排逻辑下（开发方案 7.1）。
    """
    async with session_factory() as session:
        job = await jobs_service.lock_agent_run_job(session, run_id=claimed.run_id)
        if (
            job is None
            or job.run_token != claimed.run_token
            or job.status is not JobStatusValue.RUNNING
        ):
            await session.rollback()
            return
        job.status = JobStatusValue.FAILED
        job.error = message[:ERROR_MAX_LENGTH]
        job.failure_stage = stage.value
        job.finished_at = now
        run = await repo.get_run_for_update(session, claimed.run_id)
        if run is not None:
            run.updated_at = now
            if orchestrator_version:
                run.orchestrator_version = orchestrator_version[
                    :ORCHESTRATOR_VERSION_MAX_LENGTH
                ]
        await session.commit()


async def _write_success(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    claimed: ClaimedAgentRun,
    context: context_module.ResolvedContext,
    validated: generation_ai.ValidatedAgentAnswer,
    settings: Settings,
    model: str,
    now: datetime,
    orchestrator_version: str | None = None,
) -> bool:
    """短事务回写：锁序 课程 → 会话 → Run → 任务，然后写消息、引用与来源。

    ``orchestrator_version`` 记录本次使用的编排器版本；步骤与 artifact 不需要
    在这里再写一遍——它们已经由编排循环落进 ``agent_run_steps``，
    接口读取时再据此聚合（开发方案 7.2）。
    """
    async with session_factory() as session:
        course = (
            await session.execute(
                select(Course).where(Course.id == claimed.course_id).with_for_update()
            )
        ).scalar_one_or_none()
        chat_session = (
            await session.execute(
                select(ChatSession)
                .where(ChatSession.id == claimed.session_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        run = await repo.get_run_for_update(session, claimed.run_id)
        job = await jobs_service.lock_agent_run_job(session, run_id=claimed.run_id)

        if (
            run is None
            or job is None
            or chat_session is None
            or job.run_token != claimed.run_token
            or job.status is not JobStatusValue.RUNNING
        ):
            await session.rollback()
            logger.info("运行令牌不匹配或任务已非 RUNNING，放弃发布（run=%s）", claimed.run_id)
            return False
        if job.lease_expires_at is not None and job.lease_expires_at <= now:
            await session.rollback()
            logger.info("租约已过期，放弃发布（run=%s）", claimed.run_id)
            return False

        if course is not None and course.status is CourseStatus.ARCHIVED:
            # 归档后不再写入新消息；用 FAILED 让前端拿到明确终态而不是一直 RUNNING
            job.status = JobStatusValue.FAILED
            job.error = "课程已归档，本次生成被放弃"
            job.finished_at = now
            run.updated_at = now
            await session.commit()
            return False

        written_at = now
        assistant_at = written_at + timedelta(milliseconds=1)
        if not await chat_repo.bump_session_version(
            session,
            session_id=claimed.session_id,
            expected_version=chat_session.version,
            now=assistant_at,
        ):
            # 冲突也要落到**可解释终态**：任务归我们所有（令牌匹配、租约未过期），
            # 静默 return 会让 Run 永远停在 RUNNING（评审文档「一、#9」）。
            job.status = JobStatusValue.FAILED
            job.error = "会话在生成期间被更新，本次回答未写入，请重新提问。"
            job.failure_stage = JobFailureStage.PUBLISH.value
            job.finished_at = written_at
            run.updated_at = written_at
            await session.commit()
            logger.info("会话版本冲突，已置为失败终态（run=%s）", claimed.run_id)
            return False

        assistant_message = chat_repo.add_message(
            session,
            message_id=uuid.uuid4(),
            session_id=claimed.session_id,
            role=ChatMessageRole.ASSISTANT,
            content=validated.content,
            grounded=validated.grounded,
            now=assistant_at,
        )
        await session.flush()

        for order, citation in enumerate(validated.display_citations, start=1):
            block = citation.block
            is_material = block.display_kind != CitationSourceKind.ASSIGNMENT.value
            section_id = None
            section_title = None
            if is_material and block.source_type is AgentSourceType.MATERIAL_OUTLINE:
                section_id = block.source_id
                section_title = block.section_title
            elif is_material and block.chunk_id is not None and block.material_id is not None:
                matched = await match_section(
                    session,
                    material_id=block.material_id,
                    location_start=block.location_start or 0,
                    location_end=block.location_end or 0,
                )
                if matched is not None:
                    section_id = matched.section_id
                    section_title = matched.section_title

            source_type = (
                (block.source_location_type or "PDF_PAGE")
                if is_material
                else NON_MATERIAL_SOURCE_TYPE
            )
            chat_repo.add_citation(
                session,
                citation_id=uuid.uuid4(),
                message_id=assistant_message.id,
                order=order,
                source_kind=(
                    CitationSourceKind.MATERIAL.value
                    if is_material
                    else CitationSourceKind.ASSIGNMENT.value
                ),
                source_id=block.material_id if is_material else block.source_id,
                source_label=block.material_name if is_material else block.label,
                material_id=block.material_id if is_material else None,
                material_name=block.material_name if is_material else None,
                section_id=section_id,
                section_title=section_title,
                source_type=source_type,
                location_start=block.location_start if is_material else None,
                location_end=block.location_end if is_material else None,
                page=(
                    block.location_start
                    if is_material and source_type == "PDF_PAGE"
                    else None
                ),
                quote=citation.quote,
                now=written_at,
            )

        # 记录本次**全部**注入来源（含不可引用的课程摘要与选中文本），便于审计
        snapshot_limit = settings.agent_source_snapshot_max_chars
        for order, block in enumerate(context.blocks, start=1):
            repo.add_source(
                session,
                source_id=uuid.uuid4(),
                run_id=claimed.run_id,
                order=order,
                source_type=block.source_type,
                target_id=block.source_id,
                material_id=block.material_id,
                chunk_id=block.chunk_id,
                location_start=block.location_start,
                location_end=block.location_end,
                label=block.label,
                snapshot=block.text[:snapshot_limit],
                now=written_at,
            )

        run.output_message_id = assistant_message.id
        run.prompt_version = prompt_version_for(claimed.action)[:PROMPT_VERSION_MAX_LENGTH]
        run.model = (model or "")[:MODEL_NAME_MAX_LENGTH] or None
        if orchestrator_version:
            run.orchestrator_version = orchestrator_version[:ORCHESTRATOR_VERSION_MAX_LENGTH]
        # 依据充分度落在 Run 上：前端与审计都能看到"这次是完整依据还是部分依据"
        run.evidence_level = validated.evidence_level[:EVIDENCE_LEVEL_MAX_LENGTH]
        run.updated_at = written_at
        job.status = JobStatusValue.SUCCEEDED
        job.progress = 100
        job.error = None
        job.failure_stage = None
        job.finished_at = written_at
        await session.commit()
        return True


async def run_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    claimed: ClaimedAgentRun,
    settings: Settings,
    ai_client_factory: AiClientFactory | None = None,
) -> None:
    """执行一个已领取的 Run。"""
    lease_seconds = settings.agent_run_lease_seconds
    stop_event = asyncio.Event()
    aborted = {"flag": False}
    started = time.monotonic()

    async def heartbeat_loop() -> None:
        # 续租的节奏同时决定"多久发现 Run 已被取消"，因此设上限（见常量说明）
        interval = min(
            max(lease_seconds / 3, 0.05), HEARTBEAT_MAX_INTERVAL_SECONDS
        )
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            ok = await renew_lease(
                session_factory,
                job_id=claimed.job_id,
                run_token=claimed.run_token,
                now=utc_now(),
                lease_seconds=lease_seconds,
            )
            if not ok:
                aborted["flag"] = True
                logger.info("续租失败，执行方中止（run=%s）", claimed.run_id)
                return

    heartbeat = asyncio.create_task(heartbeat_loop())
    ai_client: object | None = None
    try:
        async with session_factory() as session:
            course_row = (
                await session.execute(
                    select(Course.id, Course.teacher_id).where(Course.id == claimed.course_id)
                )
            ).first()
            is_staff = course_row is not None and course_row.teacher_id == claimed.user_id
            resolved = await context_module.resolve_context(
                session,
                session_id=claimed.session_id,
                course_id=claimed.course_id,
                entity_type=claimed.entity_type,
                entity_id=claimed.entity_id,
                section_id=claimed.section_id,
                selected_text=claimed.selected_text,
                question=claimed.question,
                action=claimed.action,
                input_message_id=claimed.input_message_id,
                is_staff=is_staff,
                settings=settings,
            )
        # 读事务到此结束：下面调用模型与执行工具时没有打开任何事务

        if aborted["flag"]:
            return

        no_evidence_message = context_module.build_no_evidence_message(
            resolved, claimed.question
        )

        # 工具上下文**只由服务端构造**：模型无法通过任何参数影响其中的字段
        tool_ctx = ToolContext(
            run_id=claimed.run_id,
            session_id=claimed.session_id,
            course_id=claimed.course_id,
            user_id=claimed.user_id,
            user_role=claimed.user_role,
            is_course_teacher=is_staff,
            session_factory=session_factory,
            settings=settings,
            question=claimed.question,
        )
        registry, catalog = _tooling()

        try:
            if ai_client_factory is not None:
                ai_client = ai_client_factory()
            else:
                ai_client = httpx.Client(
                    timeout=httpx.Timeout(settings.agent_model_timeout_seconds)
                )
            outcome = await orchestration.run_agent_loop(
                ctx=tool_ctx,
                registry=registry,
                initial_context=resolved,
                action=claimed.action,
                question=claimed.question,
                no_evidence_message=no_evidence_message,
                output_language=claimed.output_language,
                skills_catalog=skills.render_catalog(catalog),
                client=ai_client,  # type: ignore[arg-type]
                base_url=settings.ai_base_url,
                api_key=settings.ai_api_key,
                model=settings.ai_model,
                should_stop=lambda: aborted["flag"],
                is_still_active=lambda: run_still_active(
                    session_factory,
                    job_id=claimed.job_id,
                    run_token=claimed.run_token,
                ),
            )
        finally:
            close = getattr(ai_client, "close", None)
            if close is not None:
                close()

        if aborted["flag"]:
            return

        await _write_success(
            session_factory,
            claimed=claimed,
            context=resolved,
            validated=outcome.validated,
            settings=settings,
            model=outcome.model or settings.ai_model,
            now=utc_now(),
            orchestrator_version=outcome.orchestrator_version,
        )
    except orchestration.AgentRunAbortedError:
        # 已取消或租约失效：状态已由取消方/新执行者决定，这里不再写终态
        logger.info("运行已被取消，停止后续模型轮次（run=%s）", claimed.run_id)
        return
    except generation_ai.ModelToolCallUnsupportedError as exc:
        logger.warning("模型服务不支持工具协议（run=%s）：%s", claimed.run_id, exc)
        await _write_failure(
            session_factory,
            claimed=claimed,
            message=_safe_error(exc),
            now=utc_now(),
            stage=JobFailureStage.MODEL_TOOL_CALL_UNSUPPORTED,
            orchestrator_version=orchestration.ORCHESTRATOR_VERSION,
        )
    except (orchestration.AgentLoopLimitError, orchestration.AgentToolArgumentsError) as exc:
        logger.warning("工具调用阶段失败（run=%s）：%s", claimed.run_id, exc)
        await _write_failure(
            session_factory,
            claimed=claimed,
            message=_safe_error(exc),
            now=utc_now(),
            stage=JobFailureStage.TOOL_CALL,
            orchestrator_version=orchestration.ORCHESTRATOR_VERSION,
        )
    except generation_ai.AgentModelNotConfiguredError:
        logger.warning("Agent 未配置模型（run=%s）", claimed.run_id)
        await _write_failure(
            session_factory,
            claimed=claimed,
            message="模型服务未配置",
            now=utc_now(),
            stage=JobFailureStage.MODEL_CALL,
        )
    except generation_ai.AgentGenerationError as exc:
        logger.warning("Agent 生成失败（run=%s）：%s", claimed.run_id, exc)
        await _write_failure(
            session_factory,
            claimed=claimed,
            message=_safe_error(exc),
            now=utc_now(),
            stage=JobFailureStage.MODEL_CALL,
        )
    except Exception as exc:
        logger.exception("Agent 执行异常（run=%s）", claimed.run_id)
        await _write_failure(
            session_factory,
            claimed=claimed,
            message=_safe_error(exc),
            now=utc_now(),
            stage=JobFailureStage.UNKNOWN,
        )
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.info("Agent Run 结束（run=%s，%sms）", claimed.run_id, duration_ms)
        stop_event.set()
        heartbeat.cancel()


async def run_pending_batch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    ai_client_factory: AiClientFactory | None = None,
    max_jobs: int = DEFAULT_BATCH_SIZE,
    stop_requested: Callable[[], bool] | None = None,
) -> int:
    """处理至多 ``max_jobs`` 个待执行 Run，返回实际处理数。"""
    processed = 0
    while processed < max_jobs:
        if stop_requested is not None and stop_requested():
            break
        claimed = await claim_next(
            session_factory,
            now=utc_now(),
            lease_seconds=settings.agent_run_lease_seconds,
            max_attempts=settings.agent_max_attempts,
        )
        if claimed is None:
            break
        try:
            await run_job(
                session_factory,
                claimed=claimed,
                settings=settings,
                ai_client_factory=ai_client_factory,
            )
        except Exception:
            logger.exception("Agent Run 处理失败（job=%s）", claimed.job_id)
        processed += 1
    return processed


__all__ = [
    "ClaimedAgentRun",
    "claim_next",
    "renew_lease",
    "run_job",
    "run_pending_batch",
]
