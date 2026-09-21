"""已有资料的片段回填（``docs/api-contract.md`` 6.1 的运维配套）。

问答只检索 ``material_chunks`` 中的原文片段；片段是在解析 Worker 引入后
才落库的，因此**早于该功能解析完成**的 ``READY`` 资料没有片段、检索不到。
本模块提供可重复运行的维护逻辑（命令入口见
``scripts/backfill_material_chunks.py``）：

1. 选取候选：状态 ``READY``、未删除、**尚无片段**的资料（``force=True``
   时连已有片段的资料一并重做）；
2. 从对象存储读取对象并复核大小与 SHA-256（与资料声明比对，不符即拒绝）；
3. 按来源顺序切分检索片段；
4. **提交前复查**：在写入事务内再次确认资料仍为 ``READY`` 且未删除——
   回填期间被删除或状态变化的资料不会留下可检索片段；
5. 失败只记录**安全摘要**（不含对象键、原文与堆栈）并保留资料原状态，
   下一次运行自然重试。

幂等性：默认跳过已有片段的资料，重复执行结果一致（片段全量重写 +
``(material_id, order)`` 唯一约束，不会累积）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, select
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

#: 单次运行的默认处理上限，避免维护命令长时间占用连接
DEFAULT_BATCH_LIMIT = 100


@dataclass(frozen=True, slots=True)
class BackfillFailure:
    """一条资料的回填失败（安全摘要，不含原文或对象键）。"""

    material_id: uuid.UUID
    reason: str


@dataclass(slots=True)
class BackfillReport:
    """一次回填的汇总结果。"""

    filled: int = 0
    skipped: int = 0
    failed: list[BackfillFailure] = field(default_factory=list)

    @property
    def failed_count(self) -> int:
        return len(self.failed)


async def _list_candidates(
    session: AsyncSession,
    *,
    force: bool,
    limit: int,
    material_id: uuid.UUID | None,
) -> list[Material]:
    """待回填资料：``READY``、未删除、（非 force 时）尚无片段。"""
    statement = select(Material).where(
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
    statement = statement.order_by(Material.created_at).limit(limit)
    return list((await session.execute(statement)).scalars().all())


async def _replace_chunks(
    session: AsyncSession,
    *,
    material_id: uuid.UUID,
    chunks: list[extraction.RetrievalChunk],
    now: datetime,
) -> bool:
    """写入事务：复查资料状态后全量重写片段。

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


async def backfill_material_chunks(
    session: AsyncSession,
    *,
    storage: S3Storage,
    settings: Settings,
    force: bool = False,
    limit: int = DEFAULT_BATCH_LIMIT,
    material_id: uuid.UUID | None = None,
) -> BackfillReport:
    """回填 ``READY`` 资料的检索片段；重复执行结果一致。

    :param force: 连已有片段的资料也重做（默认只补没有片段的）。
    :param material_id: 只回填指定资料（排障用）。
    """
    report = BackfillReport()
    candidates = await _list_candidates(
        session, force=force, limit=limit, material_id=material_id
    )

    for material in candidates:
        material_uuid = material.id
        try:
            data = await asyncio.to_thread(
                storage.get_object_verified,
                material.storage_key,
                expected_size=material.size,
                expected_sha256_hex=material.sha256,
            )
        except StorageObjectNotFoundError:
            report.failed.append(
                BackfillFailure(material_uuid, "对象存储中不存在该文件")
            )
            logger.warning("回填失败：对象不存在（material_id=%s）", material_uuid)
            continue
        except StorageUnavailableError as exc:
            report.failed.append(
                BackfillFailure(material_uuid, f"对象存储暂时不可用（{exc.reason}）")
            )
            logger.warning("回填失败：存储不可用（material_id=%s）", material_uuid)
            continue
        except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
            report.failed.append(
                BackfillFailure(
                    material_uuid, f"读取对象失败（{type(exc).__name__}）"
                )
            )
            logger.warning("回填失败：读取对象异常（material_id=%s）", material_uuid)
            continue

        try:
            result = await asyncio.to_thread(
                extraction.extract_and_chunk,
                data,
                content_type=material.content_type,
                max_chars=settings.material_parse_max_chars,
                chunk_chars=settings.material_parse_chunk_chars,
            )
        except extraction.ExtractError as exc:
            report.failed.append(
                BackfillFailure(material_uuid, str(exc)[:MATERIAL_ERROR_MAX_LENGTH])
            )
            logger.warning("回填失败：提取异常（material_id=%s）", material_uuid)
            continue

        written = await _replace_chunks(
            session,
            material_id=material_uuid,
            chunks=result.retrieval_chunks,
            now=utc_now(),
        )
        if written:
            report.filled += 1
        else:
            # 复查未通过（回填期间被删除或状态变化）：不写片段，计入跳过
            report.skipped += 1

    return report


__all__ = [
    "BackfillFailure",
    "BackfillReport",
    "DEFAULT_BATCH_LIMIT",
    "backfill_material_chunks",
]
