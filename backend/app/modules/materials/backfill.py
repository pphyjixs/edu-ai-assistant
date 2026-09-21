"""已有资料的片段回填（``docs/api-contract.md`` 6.1 的运维配套）。

问答只检索 ``material_chunks`` 中的原文片段；片段是在解析 Worker 引入后
才落库的，因此**早于该功能解析完成**的 ``READY`` 资料没有片段、检索不到。
本模块提供可重复运行的维护逻辑（命令入口见
``scripts/backfill_material_chunks.py``）：

1. **游标遍历全部候选**：按 ``(created_at, id)`` 升序分批扫描 ``READY``、
   未删除、尚无片段的资料（``force=True`` 时连已有片段的一并重做），
   每页批量大小由 ``batch_size`` 决定——它是**分页大小**，不是总处理上限；
2. 每页查询结束后**立即结束只读事务**，再逐条读取对象存储（可能很慢）
   并复核大小与 SHA-256（与资料声明比对，不符即拒绝）；
3. 按来源顺序切分检索片段；
4. **写入用新事务并复查**：锁住资料行再次确认仍为 ``READY`` 且未删除——
   回填期间被删除或状态变化的资料不会留下可检索片段；
5. 失败只记录**安全摘要**（不含对象键、原文与堆栈）并保留资料原状态；
   单条失败仍推进游标，不会阻断后续资料；`force` 模式下每条资料在一次
   运行中只被访问一次。

幂等性：默认跳过已有片段的资料，重复执行结果一致（片段全量重写 +
``(material_id, order)`` 唯一约束，不会累积），因此重复运行只会重试
仍然缺片段的资料。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.time import utc_now
from app.modules.materials import extraction, repository as repo
from app.modules.materials.models import (
    MATERIAL_ERROR_MAX_LENGTH,
    Material,
    MaterialChunk,
    MaterialStatus,
)
from app.storage import S3Storage, StorageObjectNotFoundError, StorageUnavailableError

logger = logging.getLogger("app.materials.backfill")

#: 每页批量大小（游标分页），不是单次运行的资料总数上限
DEFAULT_BATCH_SIZE = 100


@dataclass(frozen=True, slots=True)
class BackfillFailure:
    """一条资料的回填失败（安全摘要，不含原文或对象键）。"""

    material_id: uuid.UUID
    reason: str


@dataclass(slots=True)
class BackfillReport:
    """一次回填的汇总结果（跨全部页）。"""

    filled: int = 0
    #: 复查未通过（回填期间被删除或状态变化）：应处理而未完成
    skipped: int = 0
    failed: list[BackfillFailure] = field(default_factory=list)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    @property
    def ok(self) -> bool:
        """是否全部处理成功：有失败或应处理而未完成都算未完成。"""
        return not self.failed and self.skipped == 0


@dataclass(frozen=True, slots=True)
class _Candidate:
    """候选资料的**标量快照**：查询事务结束后仍然可用，不绑定 ORM 会话。"""

    material_id: uuid.UUID
    storage_key: str
    size: int
    sha256: str
    content_type: str
    created_at: datetime


async def _list_candidates_page(
    session: AsyncSession,
    *,
    force: bool,
    batch_size: int,
    after: tuple[datetime, uuid.UUID] | None,
    material_id: uuid.UUID | None,
) -> list[_Candidate]:
    """一页候选：``READY``、未删除、（非 force 时）尚无片段；按游标升序。"""
    statement = select(
        Material.id,
        Material.storage_key,
        Material.size,
        Material.sha256,
        Material.content_type,
        Material.created_at,
    ).where(
        Material.status == MaterialStatus.READY,
        Material.deleted_at.is_(None),
    )
    if material_id is not None:
        statement = statement.where(Material.id == material_id)
    if not force:
        statement = statement.where(
            ~select(MaterialChunk.id)
            .where(MaterialChunk.material_id == Material.id)
            .exists()
        )
    if after is not None:
        # 行值比较：(created_at, id) 严格大于游标，保证不重不漏
        statement = statement.where(
            tuple_(Material.created_at, Material.id) > tuple_(*after)
        )
    statement = statement.order_by(Material.created_at, Material.id).limit(batch_size)

    rows = (await session.execute(statement)).all()
    return [
        _Candidate(
            material_id=row.id,
            storage_key=row.storage_key,
            size=row.size,
            sha256=row.sha256,
            content_type=row.content_type,
            created_at=row.created_at,
        )
        for row in rows
    ]


async def _replace_chunks(
    session: AsyncSession,
    *,
    material_id: uuid.UUID,
    chunks: list[extraction.RetrievalChunk],
    now: datetime,
) -> bool:
    """写入事务：锁定并复查资料状态后全量重写片段。

    返回 ``False`` 表示提交前复查不通过（资料已被删除或不再是 ``READY``），
    此时不写入任何片段——不会留下可检索的"孤儿片段"。
    """
    fresh = (
        await session.execute(
            select(Material)
            .where(Material.id == material_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()

    if (
        fresh is None
        or fresh.deleted_at is not None
        or fresh.status != MaterialStatus.READY
    ):
        await session.rollback()
        logger.info("回填复查未通过，放弃写入（material_id=%s）", material_id)
        return False

    await session.execute(
        delete(MaterialChunk).where(MaterialChunk.material_id == material_id)
    )
    for chunk in chunks:
        repo.add_chunk(
            session,
            chunk_id=uuid.uuid4(),
            material_id=material_id,
            order=chunk.index,
            content=chunk.content,
            location_start=chunk.location_start,
            location_end=chunk.location_end,
        )
    fresh.updated_at = now
    await session.commit()
    return True


async def _process_candidate(
    session: AsyncSession,
    *,
    candidate: _Candidate,
    storage: S3Storage,
    settings: Settings,
    report: BackfillReport,
) -> None:
    """处理单条候选：读对象 → 提取片段 → 新事务写入（失败只记安全摘要）。"""
    material_uuid = candidate.material_id
    try:
        data = await asyncio.to_thread(
            storage.get_object_verified,
            candidate.storage_key,
            expected_size=candidate.size,
            expected_sha256_hex=candidate.sha256,
        )
    except StorageObjectNotFoundError:
        report.failed.append(BackfillFailure(material_uuid, "对象存储中不存在该文件"))
        logger.warning("回填失败：对象不存在（material_id=%s）", material_uuid)
        return
    except StorageUnavailableError as exc:
        report.failed.append(
            BackfillFailure(material_uuid, f"对象存储暂时不可用（{exc.reason}）")
        )
        logger.warning("回填失败：存储不可用（material_id=%s）", material_uuid)
        return
    except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
        report.failed.append(
            BackfillFailure(material_uuid, f"读取对象失败（{type(exc).__name__}）")
        )
        logger.warning("回填失败：读取对象异常（material_id=%s）", material_uuid)
        return

    try:
        result = await asyncio.to_thread(
            extraction.extract_and_chunk,
            data,
            content_type=candidate.content_type,
            max_chars=settings.material_parse_max_chars,
            chunk_chars=settings.material_parse_chunk_chars,
        )
    except extraction.ExtractError as exc:
        report.failed.append(
            BackfillFailure(material_uuid, str(exc)[:MATERIAL_ERROR_MAX_LENGTH])
        )
        logger.warning("回填失败：提取异常（material_id=%s）", material_uuid)
        return

    written = await _replace_chunks(
        session,
        material_id=material_uuid,
        chunks=result.retrieval_chunks,
        now=utc_now(),
    )
    if written:
        report.filled += 1
    else:
        # 复查未通过（回填期间被删除或状态变化）：应处理而未完成
        report.skipped += 1


async def backfill_material_chunks(
    session: AsyncSession,
    *,
    storage: S3Storage,
    settings: Settings,
    force: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    material_id: uuid.UUID | None = None,
) -> BackfillReport:
    """回填 ``READY`` 资料的检索片段；游标分页遍历**全部**候选。

    :param force: 连已有片段的资料也重做（默认只补没有片段的）。
    :param batch_size: 每页批量大小；不是总处理上限，所有页都会被处理。
    :param material_id: 只回填指定资料（排障用）。
    """
    report = BackfillReport()
    after: tuple[datetime, uuid.UUID] | None = None

    while True:
        page = await _list_candidates_page(
            session,
            force=force,
            batch_size=batch_size,
            after=after,
            material_id=material_id,
        )
        # 结束候选查询的只读事务：读取对象存储期间不持有数据库事务
        await session.rollback()
        if not page:
            break

        for candidate in page:
            # 先推进游标：单条失败不影响后续资料的处理
            after = (candidate.created_at, candidate.material_id)
            await _process_candidate(
                session,
                candidate=candidate,
                storage=storage,
                settings=settings,
                report=report,
            )

        if len(page) < batch_size:
            break

    return report


__all__ = [
    "BackfillFailure",
    "BackfillReport",
    "DEFAULT_BATCH_SIZE",
    "backfill_material_chunks",
]
