"""解析 Worker（契约 5.5）。

第一版为**同步内联实现**：完成上传或重试解析后由 BackgroundTasks 在响应
返回后执行，状态推进对下一次轮询可见。领取消费采用 ``UPDATE ... WHERE
status='PENDING'`` 的原子领取，因此重复调度（例如幂等完成后再调度一次）
不会重复解析。

事务分三段提交，每段独立可恢复（契约 5.5 第 6 步的崩溃安全）：

1. **领取**：任务 ``PENDING → RUNNING``（记录 ``started_at``）；
2. **执行**：从对象存储拉取字节流并解析（无事务）；
3. **回写**：章节/知识点与 ``SUCCEEDED``/``READY`` 同事务落库；失败路径把
   任务与资料置 ``FAILED``，错误信息只写可安全展示的中文摘要。

回写前重查资料的 ``deleted_at``：契约 5.2 删除与解析互斥，已删除资料的
回写整体放弃。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.time import utc_now
from app.db.session import get_session_factory
from app.modules.jobs.models import Job, JobStatusValue, JobType
from app.modules.materials import parser, repository as repo
from app.modules.materials.models import Material, MaterialStatus
from app.storage import S3Storage, StorageUnavailableError

logger = logging.getLogger("app.materials.worker")

#: 回写前最后一次检查资料是否已被删除；是则放弃（契约 5.5 第 5 步）
#: 所有写入 ``error`` 列的摘要都在此长度内
_ERROR_MAX_LENGTH = 500


def _safe_error(exc: BaseException) -> str:
    """把异常转成可安全展示的中文摘要。

    ``ParseFailedError`` 的消息本身就是面向用户的；其余异常只保留类型名，
    绝不写入堆栈、对象键或内部地址（契约 5.5 第 4 步）。
    """
    if isinstance(exc, parser.ParseFailedError):
        return str(exc)[:_ERROR_MAX_LENGTH]
    return f"解析失败（{type(exc).__name__}），请稍后重试"[:_ERROR_MAX_LENGTH]


async def _claim(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    material_id: uuid.UUID,
    now: datetime,
) -> bool:
    """原子领取 ``PENDING`` 的 ``MATERIAL_PARSE`` 任务并推进资料到 ``PROCESSING``。

    领取不到（任务不存在、已被处理或资料已删除）返回 ``False``。
    """
    async with session_factory() as session:
        material = await repo.get_visible_material_for_update(session, material_id)
        if material is None:
            return False

        result = await session.execute(
            update(Job)
            .where(
                Job.type == JobType.MATERIAL_PARSE,
                Job.resource_id == material_id,
                Job.status == JobStatusValue.PENDING,
            )
            .values(status=JobStatusValue.RUNNING, started_at=now, progress=0)
        )
        if result.rowcount == 0:
            await session.rollback()
            return False

        material.status = MaterialStatus.PROCESSING
        material.error_message = None
        material.updated_at = now
        await session.commit()
        return True


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
    sections: list[parser.ExtractedSection],
    now: datetime,
) -> bool:
    """把解析结果与 ``SUCCEEDED``/``READY`` 在同一事务落库。

    资料在执行期间被删除时放弃回写（返回 ``False``，契约 5.5 第 5 步）。
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
        )
        .with_for_update()
    )
    job_row = job.scalar_one()

    # 全量重写解析产物：重试场景下清掉上一次（未成功）的残留
    repo.delete_sections(session, material_id=material.id)
    # 两表之间没有 relationship，unit of work 不保证插入顺序：
    # 先写章节并 flush，知识点的外键才有可引用的行
    section_ids: list[uuid.UUID] = []
    for order, section in enumerate(sections, start=1):
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

    for section, section_id in zip(sections, section_ids, strict=True):
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
    message: str,
    now: datetime,
) -> None:
    """把任务与资料置为 ``FAILED``；资料已被删除时整体放弃。"""
    async with session_factory() as session:
        fresh = await repo.get_visible_material_for_update(session, material_id)
        if fresh is None:
            return
        job = await session.execute(
            select(Job)
            .where(
                Job.type == JobType.MATERIAL_PARSE,
                Job.resource_id == material_id,
            )
            .with_for_update()
        )
        job_row = job.scalar_one()
        job_row.status = JobStatusValue.FAILED
        job_row.error = message
        job_row.finished_at = now
        fresh.status = MaterialStatus.FAILED
        fresh.error_message = message
        fresh.updated_at = now
        await session.commit()


async def run_material_parse(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    material_id: uuid.UUID,
    storage: S3Storage,
    now: datetime | None = None,
) -> None:
    """执行一次资料解析（契约 5.5）。任何异常都不会向外抛出。"""
    started_at = now or utc_now()

    async with session_factory() as session:
        material = await _load_material(session, material_id=material_id)
        if material is None or material.deleted_at is not None:
            return
        content_type = material.content_type
        storage_key = material.storage_key

    if not await _claim(session_factory, material_id=material_id, now=started_at):
        return

    try:
        data = storage.get_object(storage_key)
        sections = parser.extract_outline(content_type=content_type, data=data)
    except StorageUnavailableError as exc:
        logger.warning("解析读取对象失败（material_id=%s）：%s", material_id, exc)
        await _write_failure(
            session_factory,
            material_id=material_id,
            message="解析服务暂时无法读取文件，请稍后重试",
            now=utc_now(),
        )
        return
    except Exception as exc:  # noqa: BLE001 - Worker 必须自行兜底全部异常
        message = _safe_error(exc)
        logger.warning("解析失败（material_id=%s）：%s", material_id, message)
        await _write_failure(
            session_factory,
            material_id=material_id,
            message=message,
            now=utc_now(),
        )
        return

    async with session_factory() as session:
        material = await _load_material(session, material_id=material_id)
        if material is None:  # pragma: no cover - 领取阶段已确认存在
            return
        await _write_success(
            session, material=material, sections=sections, now=utc_now()
        )


def schedule_material_parse(
    background_tasks,  # noqa: ANN001 - fastapi.BackgroundTasks 避免循环导入
    *,
    settings: Settings,
    material_id: uuid.UUID,
    storage: S3Storage,
) -> None:
    """把解析任务挂到响应后的后台执行（契约 5.5 的内联实现）。

    ``material_parse_worker_enabled`` 为 ``False`` 时不调度：测试用它把
    「上传/重试」与「解析执行」解耦，Worker 行为由专项用例显式开启。
    """
    if not settings.material_parse_worker_enabled:
        return
    session_factory = get_session_factory(settings)
    background_tasks.add_task(
        run_material_parse,
        session_factory,
        material_id=material_id,
        storage=storage,
    )
