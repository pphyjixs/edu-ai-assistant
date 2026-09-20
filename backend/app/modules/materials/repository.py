"""Materials 数据访问层。

只做查询与写入，不含业务判断，不提交事务——事务边界属于 ``service.py``。
行级锁（``FOR UPDATE``）也在这里声明：完成上传必须先锁住上传会话行，
否则并发重复完成会各自读到"未完成"的旧状态。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.materials.models import (
    Material,
    MaterialStatus,
    MaterialUploadSession,
)

#: 过期清理每批处理的会话数上限，避免单次清理长时间持有大量行锁
CLEANUP_BATCH_LIMIT = 100


def add_upload_session(
    session: AsyncSession,
    *,
    upload_id: uuid.UUID,
    course_id: uuid.UUID,
    teacher_id: uuid.UUID,
    object_key: str,
    filename: str,
    content_type: str,
    size: int,
    sha256: str,
    upload_url_expires_at: datetime,
    confirm_deadline_at: datetime,
    now: datetime,
) -> MaterialUploadSession:
    """暂存上传会话；对象键唯一冲突在提交时由数据库抛出。"""
    upload = MaterialUploadSession(
        id=upload_id,
        course_id=course_id,
        teacher_id=teacher_id,
        object_key=object_key,
        filename=filename,
        content_type=content_type,
        size=size,
        sha256=sha256,
        upload_url_expires_at=upload_url_expires_at,
        confirm_deadline_at=confirm_deadline_at,
        created_at=now,
        updated_at=now,
    )
    session.add(upload)
    return upload


async def get_upload_session(
    session: AsyncSession, upload_id: uuid.UUID
) -> MaterialUploadSession | None:
    return await session.get(MaterialUploadSession, upload_id)


async def get_upload_session_for_update(
    session: AsyncSession, upload_id: uuid.UUID
) -> MaterialUploadSession | None:
    """锁住上传会话行，直到当前事务结束。

    完成上传的临界区：幂等判定、对象确认与「创建资料 + 任务 + 标记完成」
    必须在同一把锁下完成，否则并发请求会各自创建一份资料。
    """
    result = await session.execute(
        select(MaterialUploadSession)
        .where(MaterialUploadSession.id == upload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


def add_material(
    session: AsyncSession,
    *,
    material_id: uuid.UUID,
    course_id: uuid.UUID,
    upload_id: uuid.UUID,
    filename: str,
    content_type: str,
    size: int,
    sha256: str,
    storage_key: str,
    status: MaterialStatus,
    uploaded_by: uuid.UUID,
    now: datetime,
) -> Material:
    """暂存资料；``upload_id`` 唯一约束是幂等完成的最后一道防线。"""
    material = Material(
        id=material_id,
        course_id=course_id,
        upload_id=upload_id,
        filename=filename,
        content_type=content_type,
        size=size,
        sha256=sha256,
        storage_key=storage_key,
        status=status,
        uploaded_by=uploaded_by,
        created_at=now,
        updated_at=now,
    )
    session.add(material)
    return material


async def get_material_by_id(
    session: AsyncSession, material_id: uuid.UUID
) -> Material | None:
    return await session.get(Material, material_id)


async def get_material_by_upload_id(
    session: AsyncSession, upload_id: uuid.UUID
) -> Material | None:
    result = await session.execute(
        select(Material).where(Material.upload_id == upload_id)
    )
    return result.scalar_one_or_none()


async def list_expired_incomplete_uploads(
    session: AsyncSession, *, now: datetime, limit: int = CLEANUP_BATCH_LIMIT
) -> list[MaterialUploadSession]:
    """取出超过确认窗口仍未完成、且尚未被清理的会话，并锁定其行。

    - ``completed_material_id IS NULL``：已完成会话及其资料绝不进入清理范围；
    - ``expired_at IS NULL``：已标记过期的会话幂等跳过；
    - ``SKIP LOCKED``：跳过正被其他事务（如并发的完成请求）锁住的行，
      避免清理阻塞业务请求，也不会误删「刚确认完成」的对象。
    """
    result = await session.execute(
        select(MaterialUploadSession)
        .where(
            MaterialUploadSession.confirm_deadline_at <= now,
            MaterialUploadSession.completed_material_id.is_(None),
            MaterialUploadSession.expired_at.is_(None),
        )
        .order_by(MaterialUploadSession.confirm_deadline_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(result.scalars().all())


def mark_upload_expired(
    session: AsyncSession, upload: MaterialUploadSession, *, now: datetime
) -> None:
    """标记上传会话为过期（已清理）。"""
    upload.expired_at = now
    upload.updated_at = now
