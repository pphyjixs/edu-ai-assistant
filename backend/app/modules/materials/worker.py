"""解析 Worker（契约 5.5）：从 PostgreSQL 队列领取并执行课件解析。

**独立进程**：由 ``scripts/parse_worker.py`` 启动，直接轮询数据库领取
``MATERIAL_PARSE`` 任务，不依赖 Vercel 请求进程或任何内存队列——
API 进程只负责创建任务，解析一律由本 Worker 完成。

崩溃防护三件套：

- **租约**（``lease_expires_at``）：领取时设置，RUNNING 超过租约视为执行者失联；
- **心跳**：执行期间按租约的 1/3 定期续租，长解析不会被误判崩溃；
- **运行令牌**（``run_token``）：领取时生成，回写必须携带匹配值；
  任务被重试重置（新令牌）或资料被删除后，旧执行者的回写一律放弃。

执行流程（每条任务）：

1. 流式下载对象并复核大小与 SHA-256（与资料声明比对，不符即拒绝）；
2. 按来源顺序提取文本并分块（默认全文 120,000 字符、每块 8,000 字符；
   超限直接 FAILED，不截断后宣称成功；扫描版 PDF 明确失败，不做 OCR）；
3. 调用 Chat Completions 兼容端点生成章节与知识点（Pydantic 校验、
   原文摘录必须在来源文本中找到、同原文主要语言）；
4. 全部结果准备好后**一次事务**发布章节、知识点、资料 ``READY`` 与
   任务 ``SUCCEEDED``；失败只保存安全错误说明。

领取、心跳与回写使用 async 会话（``NullPool`` 场景连接不绑定循环）；
下载、提取与模型调用在线程池中执行。所有错误都不会向外抛出：
任务与资料进入 ``FAILED`` 并附安全摘要。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.time import utc_now
from app.modules.jobs.models import Job, JobStatusValue, JobType
from app.modules.materials import extraction, outline_ai, repository as repo
from app.modules.materials.models import Material, MaterialStatus
from app.storage import S3Storage
from app.storage.errors import StorageObjectNotFoundError

logger = logging.getLogger("app.materials.worker")

#: 所有写入 ``error`` 列的摘要都在此长度内
_ERROR_MAX_LENGTH = 500

#: 单批最多领取的任务数（独立进程每次循环的上限）
DEFAULT_BATCH_SIZE = 5

#: 生成 AI 客户端的工厂（测试注入本地假模型 HTTP 服务）
AiClientFactory = Callable[[], object]


@dataclass(slots=True)
class ClaimedJob:
    """一次领取的执行上下文。"""

    job_id: uuid.UUID
    material: Material
    run_token: str


def _safe_error(exc: BaseException) -> str:
    """把意外异常转成可安全展示的中文摘要（不暴露堆栈与内部细节）。"""
    if isinstance(exc, (extraction.ExtractError, outline_ai.OutlineGenerationError)):
        return str(exc)[:_ERROR_MAX_LENGTH]
    return f"解析失败（{type(exc).__name__}），请稍后重试"[:_ERROR_MAX_LENGTH]


async def claim_next(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    lease_seconds: int,
) -> ClaimedJob | None:
    """从队列原子领取一个 ``PENDING`` 的解析任务（契约 5.5 第 1 步）。

    ``FOR UPDATE SKIP LOCKED`` 保证多 Worker 实例互不重复领取；已删除
    资料的任务不领取。领取时递增 ``attempts``、生成 ``run_token`` 并设置
    租约，同时把资料推进到 ``PROCESSING``。队列空时返回 ``None``。
    """
    run_token = secrets.token_hex(16)
    async with session_factory() as session:
        job = (
            await session.execute(
                select(Job)
                .join(
                    Material,
                    (Material.id == Job.resource_id)
                    & (Material.deleted_at.is_(None)),
                )
                .where(
                    Job.type == JobType.MATERIAL_PARSE,
                    Job.status == JobStatusValue.PENDING,
                )
                .order_by(Job.created_at)
                .limit(1)
                .with_for_update(skip_locked=True, of=Job)
            )
        ).scalars().first()
        if job is None:
            await session.rollback()
            return None

        material = (
            await session.execute(
                select(Material)
                .where(Material.id == job.resource_id)
                .with_for_update()
            )
        ).scalars().one()

        job.status = JobStatusValue.RUNNING
        job.started_at = now
        job.progress = 0
        job.attempts += 1
        job.run_token = run_token
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        material.status = MaterialStatus.PROCESSING
        material.error_message = None
        material.updated_at = now
        await session.commit()
        return ClaimedJob(job_id=job.id, material=material, run_token=run_token)


async def renew_lease(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    job_id: uuid.UUID,
    run_token: str,
    now: datetime,
    lease_seconds: int,
) -> bool:
    """心跳：为仍属于本执行者的任务续租。

    令牌不匹配或任务已非 ``RUNNING``（被删除事务取消/被重试重置）时
    返回 ``False``，执行方据此中止本轮解析。
    """
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


async def _load_material(
    session: AsyncSession, *, material_id: uuid.UUID
) -> Material | None:
    result = await session.execute(
        select(Material).where(Material.id == material_id)
    )
    return result.scalar_one_or_none()


async def _write_success(
    session: AsyncSession,
    *,
    material: Material,
    outline: outline_ai.GeneratedOutline,
    run_token: str,
    now: datetime,
) -> bool:
    """一次事务发布章节、知识点、资料 ``READY`` 与任务 ``SUCCEEDED``。

    发布前再次检查删除标记与运行令牌（契约 5.5 第 5 步）：资料在执行
    期间被删除，或任务已被重试重置并由新一轮执行接管（令牌不匹配）时
    放弃回写，返回 ``False``。
    """
    fresh = await repo.get_visible_material_for_update(session, material.id)
    if fresh is None:
        await session.rollback()
        logger.info("资料在解析期间被删除，放弃回写结果（material_id=%s）", material.id)
        return False

    job = await session.execute(
        select(Job)
        .where(
            Job.type == JobType.MATERIAL_PARSE,
            Job.resource_id == material.id,
            Job.run_token == run_token,
        )
        .with_for_update()
    )
    job_row = job.scalar_one_or_none()
    if job_row is None:
        # 任务已被重试重置并由新的运行令牌接管：本轮结果作废
        await session.rollback()
        logger.info("运行令牌不匹配，放弃回写结果（material_id=%s）", material.id)
        return False

    # 全量重写解析产物：重试场景下清掉上一次（未成功）的残留
    await repo.delete_sections(session, material_id=material.id)
    # 两表之间没有 relationship，unit of work 不保证插入顺序：
    # 先写章节并 flush，知识点的外键才有可引用的行
    section_ids: list[uuid.UUID] = []
    for order, section in enumerate(outline.sections, start=1):
        section_id = uuid.uuid4()
        repo.add_section(
            session,
            section_id=section_id,
            material_id=material.id,
            order=order,
            title=section.title,
            location_start=section.location_start,
            location_end=section.location_end,
        )
        section_ids.append(section_id)
    await session.flush()

    for section, section_id in zip(outline.sections, section_ids, strict=True):
        for point_order, point in enumerate(section.knowledge_points, start=1):
            repo.add_knowledge_point(
                session,
                point_id=uuid.uuid4(),
                section_id=section_id,
                order=point_order,
                title=point.title,
                description=point.description,
                quote=point.quote,
                location_start=point.location_start,
                location_end=point.location_end,
            )

    job_row.status = JobStatusValue.SUCCEEDED
    job_row.progress = 100
    job_row.error = None
    job_row.finished_at = now
    fresh.status = MaterialStatus.READY
    fresh.error_message = None
    fresh.updated_at = now
    await session.commit()
    return True


async def _write_failure(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    material_id: uuid.UUID,
    run_token: str,
    message: str,
    now: datetime,
) -> None:
    """把任务与资料置为 ``FAILED``；资料已删除或令牌不匹配时放弃。"""
    async with session_factory() as session:
        fresh = await repo.get_visible_material_for_update(session, material_id)
        if fresh is None:
            return
        job = await session.execute(
            select(Job)
            .where(
                Job.type == JobType.MATERIAL_PARSE,
                Job.resource_id == material_id,
                Job.run_token == run_token,
            )
            .with_for_update()
        )
        job_row = job.scalar_one_or_none()
        if job_row is None:
            return
        job_row.status = JobStatusValue.FAILED
        job_row.error = message
        job_row.finished_at = now
        fresh.status = MaterialStatus.FAILED
        fresh.error_message = message
        fresh.updated_at = now
        await session.commit()


async def run_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    claimed: ClaimedJob,
    storage: S3Storage,
    settings: Settings,
    ai_client_factory: AiClientFactory | None = None,
) -> None:
    """执行一条已领取的任务。任何异常都不会向外抛出。"""
    material = claimed.material
    run_token = claimed.run_token
    lease_seconds = settings.material_parse_lease_seconds
    aborted = {"flag": False}
    stop_event = asyncio.Event()

    async def fail(message: str) -> None:
        await _write_failure(
            session_factory,
            material_id=material.id,
            run_token=run_token,
            message=message,
            now=utc_now(),
        )

    async def heartbeat_loop() -> None:
        """RUNNING 期间按租约的 1/3 定期续租（契约 5.5 的心跳）。"""
        interval = max(lease_seconds / 3, 0.05)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return  # 任务完成，正常退出心跳
            except TimeoutError:
                pass
            try:
                ok = await renew_lease(
                    session_factory,
                    job_id=claimed.job_id,
                    run_token=run_token,
                    now=utc_now(),
                    lease_seconds=lease_seconds,
                )
            except Exception:  # noqa: BLE001 - 心跳失败不中断解析，下轮再试
                logger.warning("心跳续租失败，将在下轮重试（job=%s）", claimed.job_id)
                continue
            if not ok:
                aborted["flag"] = True
                logger.warning(
                    "心跳发现任务已被接管，中止解析（job=%s）", claimed.job_id
                )
                return

    heartbeat_task = asyncio.create_task(heartbeat_loop())
    ai_client: object | None = None

    try:
        try:
            data = await asyncio.to_thread(
                storage.get_object_verified,
                material.storage_key,
                expected_size=material.size,
                expected_sha256_hex=material.sha256,
            )
        except StorageVerificationError as exc:
            logger.warning("对象复核失败（material_id=%s）：%s", material.id, exc)
            await fail(f"对象内容校验失败（{exc.reason}），已拒绝解析")
            return
        except StorageObjectNotFoundError as exc:
            logger.warning("对象不存在（material_id=%s）：%s", material.id, exc)
            await fail("对象存储中不存在该文件，无法解析")
            return
        except StorageUnavailableError as exc:
            logger.warning("读取对象失败（material_id=%s）：%s", material.id, exc)
            await fail("解析服务暂时无法读取文件，请稍后重试")
            return

        if aborted["flag"]:
            logger.info("任务在解析前已被接管，放弃（material_id=%s）", material.id)
            return

        try:
            chunks = await asyncio.to_thread(
                extraction.extract_and_chunk,
                data,
                content_type=material.content_type,
                max_chars=settings.material_parse_max_chars,
                chunk_chars=settings.material_parse_chunk_chars,
            )
        except extraction.ExtractError as exc:
            await fail(str(exc))
            return

        if aborted["flag"]:
            logger.info("任务在提取后被接管，放弃（material_id=%s）", material.id)
            return

        try:
            if ai_client_factory is not None:
                ai_client = ai_client_factory()
        except Exception as exc:  # noqa: BLE001 - 客户端构造失败按生成失败处理
            logger.warning("模型客户端构造失败：%s", exc)
            await fail("模型服务暂不可用，请稍后重试")
            return

        try:
            outline = await asyncio.to_thread(
                outline_ai.generate_outline,
                chunks,
                base_url=settings.ai_base_url,
                api_key=settings.ai_api_key,
                model=settings.ai_model,
                timeout_seconds=settings.ai_timeout_seconds,
                client=ai_client,
            )
        except outline_ai.OutlineGenerationError as exc:
            logger.warning("大纲生成失败（material_id=%s）：%s", material.id, exc)
            await fail(str(exc))
            return
        finally:
            close = getattr(ai_client, "close", None)
            if close is not None:
                close()

        if aborted["flag"]:
            logger.info("任务在发布前被接管，放弃（material_id=%s）", material.id)
            return

        async with session_factory() as session:
            fresh_material = await _load_material(session, material_id=material.id)
            if fresh_material is None:  # pragma: no cover - 领取阶段已确认存在
                return
            published = await _write_success(
                session,
                material=fresh_material,
                outline=outline,
                run_token=run_token,
                now=utc_now(),
            )
        if published:
            logger.info("解析完成（material_id=%s）", material.id)
        else:
            logger.info("解析结果未发布（资料已删除或任务被接管）：%s", material.id)
    finally:
        stop_event.set()
        heartbeat_task.cancel()


async def run_pending_batch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    storage: S3Storage,
    settings: Settings,
    ai_client_factory: AiClientFactory | None = None,
    max_jobs: int = DEFAULT_BATCH_SIZE,
    stop_requested: Callable[[], bool] | None = None,
) -> int:
    """领取并执行一批待解析任务；返回处理数量。

    供独立进程主循环与集成测试共用：队列空或 ``stop_requested()`` 为真
    时返回。
    """
    processed = 0
    while processed < max_jobs:
        if stop_requested is not None and stop_requested():
            break
        claimed = await claim_next(
            session_factory,
            now=utc_now(),
            lease_seconds=settings.material_parse_lease_seconds,
        )
        if claimed is None:
            break
        try:
            await run_job(
                session_factory,
                claimed=claimed,
                storage=storage,
                settings=settings,
                ai_client_factory=ai_client_factory,
            )
        except Exception:  # noqa: BLE001 - 单条任务的意外异常不拖垮整批
            logger.exception(
                "任务执行出现意外异常（job=%s material=%s）",
                claimed.job_id,
                claimed.material.id,
            )
            try:
                await _write_failure(
                    session_factory,
                    material_id=claimed.material.id,
                    run_token=claimed.run_token,
                    message=_safe_error(sys.exc_info()[1]),
                    now=utc_now(),
                )
            except Exception:  # noqa: BLE001 - 数据库也不可用时只能记录
                logger.exception("写入失败状态时再次出错（job=%s）", claimed.job_id)
        processed += 1
    return processed
