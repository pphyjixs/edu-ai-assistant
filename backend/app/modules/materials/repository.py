"""Materials 数据访问层。

只做查询与写入，不含业务判断，不提交事务——事务边界属于 ``service.py``。
行级锁（``FOR UPDATE``）也在这里声明：完成上传必须先锁住上传会话行，
否则并发重复完成会各自读到"未完成"的旧状态。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.materials.models import (
    Material,
    MaterialChunk,
    MaterialDeleteStatus,
    MaterialDeleteTodo,
    MaterialKnowledgePoint,
    MaterialSection,
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


async def get_visible_material_by_id(
    session: AsyncSession, material_id: uuid.UUID
) -> Material | None:
    """取未被删除的资料（契约 5.1–5.4 的读路径）：已删除视为不存在。"""
    result = await session.execute(
        select(Material).where(
            Material.id == material_id,
            Material.deleted_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def lock_live_materials(
    session: AsyncSession, *, material_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """按 ID 升序对资料加**共享锁**，返回仍为 ``READY`` 且未删除的资料 ID。

    问答写入事务在落库引用前调用（契约 6.1）：共享锁与删除资料的排他锁
    互斥，因此这些资料在本次事务提交前不会被并发删除——不会出现"引用指向
    刚被删除的资料"。查询按 ID 升序加锁，多个并发事务取锁顺序一致，避免死锁。

    返回的 ID 是**锁后**读到的状态：生成期间已失效的资料不在集合中，
    调用方据此丢弃对应引用（全部失效则按无依据回答处理）。
    """
    if not material_ids:
        return set()
    ordered_ids = sorted(set(material_ids))
    result = await session.execute(
        select(Material.id)
        .where(
            Material.id.in_(ordered_ids),
            Material.status == MaterialStatus.READY,
            Material.deleted_at.is_(None),
        )
        .order_by(Material.id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    return set(result.scalars().all())


async def get_visible_material_for_update(
    session: AsyncSession, material_id: uuid.UUID
) -> Material | None:
    """锁住未被删除的资料行。

    删除（5.2）与重试解析（5.3）的临界区：两者都要在行锁内判定状态并写入，
    行锁保证并发删除/重试互斥（例如删除与重试并发时不会出现
    "重试刚重置完状态、删除随后才标记删除"的交叉）。
    """
    result = await session.execute(
        select(Material)
        .where(
            Material.id == material_id,
            Material.deleted_at.is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def list_course_materials(
    session: AsyncSession,
    *,
    course_id: uuid.UUID,
    offset: int,
    limit: int,
) -> tuple[list[Material], int]:
    """课程资料列表（契约 5.1）：不含已删除，按创建时间倒序、ID 倒序。

    复合索引 ``ix_materials_course_id_created_at_id_desc`` 覆盖过滤与排序。
    """
    total = await session.scalar(
        select(func.count())
        .select_from(Material)
        .where(
            Material.course_id == course_id,
            Material.deleted_at.is_(None),
        )
    )
    result = await session.execute(
        select(Material)
        .where(
            Material.course_id == course_id,
            Material.deleted_at.is_(None),
        )
        .order_by(Material.created_at.desc(), Material.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all()), int(total or 0)


def mark_material_deleted(
    session: AsyncSession, material: Material, *, now: datetime
) -> None:
    """标记删除资料（契约 5.2）：记录保留，从所有读接口中消失。"""
    material.deleted_at = now
    material.updated_at = now


async def list_sections_with_points(
    session: AsyncSession, *, material_id: uuid.UUID
) -> list[tuple[MaterialSection, list[MaterialKnowledgePoint]]]:
    """按 order 升序取章节及其知识点（契约 5.4）。"""
    sections = await session.execute(
        select(MaterialSection)
        .where(MaterialSection.material_id == material_id)
        .order_by(MaterialSection.order)
    )
    section_list = list(sections.scalars().all())
    if not section_list:
        return []

    points = await session.execute(
        select(MaterialKnowledgePoint)
        .where(
            MaterialKnowledgePoint.section_id.in_(
                [section.id for section in section_list]
            )
        )
        .order_by(MaterialKnowledgePoint.section_id, MaterialKnowledgePoint.order)
    )
    points_by_section: dict[uuid.UUID, list[MaterialKnowledgePoint]] = {}
    for point in points.scalars().all():
        points_by_section.setdefault(point.section_id, []).append(point)

    return [
        (section, points_by_section.get(section.id, [])) for section in section_list
    ]


def add_section(
    session: AsyncSession,
    *,
    section_id: uuid.UUID,
    material_id: uuid.UUID,
    order: int,
    title: str,
    location_start: int,
    location_end: int,
) -> MaterialSection:
    """暂存章节（Worker 解析成功路径）。"""
    section = MaterialSection(
        id=section_id,
        material_id=material_id,
        order=order,
        title=title,
        location_start=location_start,
        location_end=location_end,
    )
    session.add(section)
    return section


def add_knowledge_point(
    session: AsyncSession,
    *,
    point_id: uuid.UUID,
    section_id: uuid.UUID,
    order: int,
    title: str,
    description: str,
    quote: str,
    location_start: int,
    location_end: int,
) -> MaterialKnowledgePoint:
    """暂存知识点（Worker 解析成功路径）。"""
    point = MaterialKnowledgePoint(
        id=point_id,
        section_id=section_id,
        order=order,
        title=title,
        description=description,
        quote=quote,
        location_start=location_start,
        location_end=location_end,
    )
    session.add(point)
    return point


async def delete_sections(
    session: AsyncSession, *, material_id: uuid.UUID
) -> None:
    """清空资料的解析产物（重试解析前全量重写）。"""
    await session.execute(
        sa_delete(MaterialSection).where(MaterialSection.material_id == material_id)
    )


async def delete_chunks(session: AsyncSession, *, material_id: uuid.UUID) -> None:
    """清空资料的可检索片段（删除资料与重新发布解析产物时调用）。"""
    await session.execute(
        sa_delete(MaterialChunk).where(MaterialChunk.material_id == material_id)
    )


def add_chunk(
    session: AsyncSession,
    *,
    chunk_id: uuid.UUID,
    material_id: uuid.UUID,
    order: int,
    content: str,
    location_start: int,
    location_end: int,
) -> MaterialChunk:
    """暂存一个检索片段（Worker 解析成功路径）。"""
    chunk = MaterialChunk(
        id=chunk_id,
        material_id=material_id,
        order=order,
        content=content,
        location_start=location_start,
        location_end=location_end,
    )
    session.add(chunk)
    return chunk


async def list_chunks(
    session: AsyncSession, *, material_id: uuid.UUID
) -> list[MaterialChunk]:
    """按 order 升序取资料的检索片段。"""
    result = await session.execute(
        select(MaterialChunk)
        .where(MaterialChunk.material_id == material_id)
        .order_by(MaterialChunk.order)
    )
    return list(result.scalars().all())


async def count_chunks(session: AsyncSession, *, material_id: uuid.UUID) -> int:
    """统计资料的片段数量（验收用：重复解析不得累积）。"""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(MaterialChunk)
            .where(MaterialChunk.material_id == material_id)
        )
        or 0
    )


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


# --------------------------------------------------------------------------- #
# 完成响应快照（契约 4.6 / 5.2：重复完成返回首次结果）
# --------------------------------------------------------------------------- #
def save_completion_snapshot(
    session: AsyncSession, upload: MaterialUploadSession, *, snapshot: dict
) -> None:
    """在会话行上保存完成响应快照（首次完成时调用）。"""
    upload.completion_snapshot = snapshot


async def get_completion_snapshot(
    session: AsyncSession, upload_id: uuid.UUID
) -> dict | None:
    """读取完成响应快照；未保存时返回 ``None``。"""
    upload = await get_upload_session(session, upload_id)
    if upload is None:
        return None
    return upload.completion_snapshot


# --------------------------------------------------------------------------- #
# 对象删除待办（契约 5.2 的删除流水线）
# --------------------------------------------------------------------------- #
def add_delete_todo(
    session: AsyncSession,
    *,
    todo_id: uuid.UUID,
    material_id: uuid.UUID,
    course_id: uuid.UUID,
    object_key: str,
    upload_expires_at: datetime,
    now: datetime,
) -> MaterialDeleteTodo:
    """写入对象删除待办（删除资料的事务内调用）。"""
    todo = MaterialDeleteTodo(
        id=todo_id,
        material_id=material_id,
        course_id=course_id,
        object_key=object_key,
        upload_expires_at=upload_expires_at,
        status=MaterialDeleteStatus.PENDING,
        requested_at=now,
    )
    session.add(todo)
    return todo


async def get_delete_todo_by_material(
    session: AsyncSession, material_id: uuid.UUID
) -> MaterialDeleteTodo | None:
    result = await session.execute(
        select(MaterialDeleteTodo).where(
            MaterialDeleteTodo.material_id == material_id
        )
    )
    return result.scalar_one_or_none()


async def list_due_delete_todos(
    session: AsyncSession,
    *,
    now: datetime,
    buffer_seconds: int,
    limit: int = 100,
) -> list[MaterialDeleteTodo]:
    """取出可执行的对象删除待办并锁定行（``SKIP LOCKED`` 防多实例竞争）。

    仅处理 ``PENDING`` 且「PUT 地址过期 + 缓冲期」已过的待办——
    在此之前浏览器仍可能拿着原地址直传，立即删除会留下重建窗口。
    """
    due_before = now - timedelta(seconds=buffer_seconds)
    result = await session.execute(
        select(MaterialDeleteTodo)
        .where(
            MaterialDeleteTodo.status == MaterialDeleteStatus.PENDING,
            MaterialDeleteTodo.upload_expires_at <= due_before,
        )
        .order_by(MaterialDeleteTodo.requested_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(result.scalars().all())
