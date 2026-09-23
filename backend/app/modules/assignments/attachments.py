"""作业附件（``docs/api-contract.md`` 8.15）。

教师给任务附参考文件、学生下载。整个特性只有三件事：签发上传、确认上传、读列表。

与课件、报告上传共用同一套协议（契约 §4）：

- 后端签发预签名 ``PUT``，浏览器直传对象存储，**文件体不经过 API 进程**；
- 完成确认用 ``HeadObject`` 核对存在性、大小、类型与摘要（``app.core.uploads``）；
- 字段规则（文件名、PDF/PPTX/DOCX 白名单、大小范围、sha256 格式）直接复用
  课件上传的校验实现，避免同一件事出现两套判定。

一处刻意的简化：**一行同时是"上传会话"与"附件"**。``completed_at`` 为空表示
这次上传还没确认（对读接口不可见）。附件只属于教师本人与这份任务，不需要像
实验报告那样按学生复用记录，因此一个待完成行就够了；重复初始化同名文件时
**复用那一行**并换发新的对象键（旧对象尽力删除），pending 行不会越堆越多。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import NamedTuple, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    AttachmentAlreadyExistsError,
    InternalError,
    ResourceNotFoundError,
)
from app.core.time import utc_now
from app.core.uploads import upload_invalid, verify_stored_object
from app.modules.assignments import repository as repo
from app.modules.assignments.attachment_schemas import AttachmentUploadInitRequest
from app.modules.assignments.models import Assignment, AssignmentAttachment
from app.modules.auth import repository as auth_repo
from app.modules.auth.models import User
from app.modules.materials.schemas import MaterialUploadInitRequest
from app.modules.materials.service import ValidatedUpload, validate_upload_request
from app.storage import (
    PresignedDownload,
    PresignedUpload,
    S3Storage,
    StorageConfigError,
    StorageUnavailableError,
    as_service_unavailable,
    build_attachment_object_key,
    presign_download,
)

logger = logging.getLogger("app.assignments.attachments")


class AttachmentView(NamedTuple):
    """附件 + 现场签发的下载地址（读接口的返回单元）。"""

    attachment: AssignmentAttachment
    download: PresignedDownload
    uploaded_by_name: str | None


def _validate(
    payload: AttachmentUploadInitRequest, *, max_bytes: int
) -> ValidatedUpload:
    """字段校验直接复用课件上传的实现。

    两个请求模型的字段名与类型完全一致（``filename`` / ``content_type`` /
    ``size`` / ``sha256``），白名单也同为一套（PDF / PPTX / DOCX）。复制一份
    校验最容易出现"改了 A 忘了改 B"，因此这里显式复用并在此说明来源。
    """
    return validate_upload_request(
        cast(MaterialUploadInitRequest, payload), max_bytes=max_bytes
    )


def _presign_put(
    storage: S3Storage,
    settings: Settings,
    *,
    object_key: str,
    content_type: str,
    sha256: str,
    now: datetime,
) -> PresignedUpload:
    try:
        return storage.create_presigned_put(
            object_key,
            content_type=content_type,
            sha256_hex=sha256,
            expires_in=settings.storage_upload_url_ttl_seconds,
            now=now,
        )
    except StorageUnavailableError as exc:
        raise as_service_unavailable(exc) from exc
    except StorageConfigError as exc:  # pragma: no cover - 入参已被校验
        logger.error("预签名参数不合法：%s", exc)
        raise InternalError() from exc


def _try_delete_object(storage: S3Storage, object_key: str) -> None:
    """尽力删除被替换掉的旧对象；失败只记日志，不影响本次请求。

    删不掉会留下一个孤立对象，但它已经不被任何记录引用，也不会再出现在
    任何列表里 —— 这比"因为清理失败而让教师的上传报错"要好。
    """
    try:
        storage.delete_object(object_key)
    except Exception:  # noqa: BLE001 - 清理是尽力而为，不向外抛
        logger.warning("删除被替换的附件对象失败（key=%s）", object_key, exc_info=True)


async def init_upload(
    session: AsyncSession,
    *,
    storage: S3Storage,
    settings: Settings,
    assignment: Assignment,
    user: User,
    payload: AttachmentUploadInitRequest,
    now: datetime | None = None,
) -> tuple[AssignmentAttachment, PresignedUpload]:
    """初始化附件上传（契约 8.15）。

    权限、归档与任务可见性由调用方的守卫完成并已锁住课程与任务行。

    已存在**同名且已完成**的附件时返回 ``409 ATTACHMENT_ALREADY_EXISTS``：
    同名附件不允许并存，教师应先删除旧附件（界面上就在同一个列表里）。
    """
    validated = _validate(
        payload, max_bytes=settings.assignment_attachment_max_upload_bytes
    )
    current = now or utc_now()

    completed = await repo.get_completed_attachment_by_filename(
        session, assignment_id=assignment.id, filename=validated.filename
    )
    if completed is not None:
        raise AttachmentAlreadyExistsError(
            details={"filename": validated.filename, "attachment_id": str(completed.id)}
        )

    upload_id = uuid.uuid4()
    object_key = build_attachment_object_key(
        assignment.course_id, assignment.id, upload_id, validated.extension
    )
    presigned = _presign_put(
        storage,
        settings,
        object_key=object_key,
        content_type=validated.content_type,
        sha256=validated.sha256,
        now=current,
    )
    confirm_deadline_at = current + timedelta(
        seconds=settings.assignment_attachment_confirm_ttl_seconds
    )

    pending = await repo.get_pending_attachment(
        session, assignment_id=assignment.id, filename=validated.filename
    )
    if pending is None:
        attachment = await repo.add_attachment(
            session,
            attachment_id=uuid.uuid4(),
            assignment_id=assignment.id,
            upload_id=upload_id,
            object_key=object_key,
            filename=validated.filename,
            content_type=validated.content_type,
            size=validated.size,
            sha256=validated.sha256,
            uploaded_by=user.id,
            upload_url_expires_at=presigned.expires_at,
            confirm_deadline_at=confirm_deadline_at,
            now=current,
        )
    else:
        # 复用这把待完成的记录，换发新的上传会话与对象键
        previous_key = pending.object_key
        attachment = pending
        attachment.upload_id = upload_id
        attachment.object_key = object_key
        attachment.content_type = validated.content_type
        attachment.size = validated.size
        attachment.sha256 = validated.sha256
        attachment.uploaded_by = user.id
        attachment.upload_url_expires_at = presigned.expires_at
        attachment.confirm_deadline_at = confirm_deadline_at
        attachment.updated_at = current
        if previous_key != object_key:
            _try_delete_object(storage, previous_key)

    await session.commit()
    return attachment, presigned


async def complete_upload(
    session: AsyncSession,
    *,
    storage: S3Storage,
    assignment: Assignment,
    upload_id: uuid.UUID,
    now: datetime | None = None,
) -> AssignmentAttachment:
    """确认附件上传（契约 8.15）。

    已确认过的同一 ``upload_id`` **幂等**返回原记录（不再受确认窗口限制），
    这让"网络抖动后前端重试"不会变成错误。
    """
    attachment = await repo.get_attachment_by_upload_id(
        session, assignment_id=assignment.id, upload_id=upload_id
    )
    if attachment is None:
        raise ResourceNotFoundError()

    if attachment.completed_at is not None:
        return attachment

    current = now or utc_now()
    if attachment.confirm_deadline_at <= current:
        raise upload_invalid(
            "UPLOAD_EXPIRED",
            "上传确认窗口已过，请重新上传该附件",
            confirm_deadline_at=attachment.confirm_deadline_at.isoformat(),
        )

    verify_stored_object(
        storage,
        object_key=attachment.object_key,
        size=attachment.size,
        content_type=attachment.content_type,
        sha256=attachment.sha256,
    )

    attachment.completed_at = current
    attachment.updated_at = current
    try:
        await session.commit()
    except IntegrityError as exc:
        # 部分唯一索引 (assignment_id, filename) WHERE completed_at IS NOT NULL：
        # 并发完成同名附件时只有一个成功，另一个按"已存在"处理
        await session.rollback()
        raise AttachmentAlreadyExistsError(
            details={"filename": attachment.filename}
        ) from exc
    return attachment


async def list_attachments(
    session: AsyncSession,
    *,
    assignment: Assignment,
    storage: S3Storage,
    settings: Settings,
) -> list[AttachmentView]:
    """列出已完成的附件，并为每个签发**现场**下载地址（契约 8.15）。

    地址是纯本地签名，因此一次列表请求里逐个签发几乎没有成本；
    这样前端拿到的链接总是新鲜的，不需要自己判断是否过期。
    """
    attachments = await repo.list_completed_attachments(
        session, assignment_id=assignment.id
    )
    names = await auth_repo.get_users_public_summaries(
        session, {item.uploaded_by for item in attachments}
    )

    views: list[AttachmentView] = []
    for attachment in attachments:
        download = presign_download(
            storage,
            attachment.object_key,
            ttl_seconds=settings.storage_upload_url_ttl_seconds,
        )
        summary = names.get(attachment.uploaded_by)
        views.append(
            AttachmentView(
                attachment=attachment,
                download=download,
                uploaded_by_name=summary.display_name if summary else None,
            )
        )
    return views


async def delete_attachment(
    session: AsyncSession,
    *,
    storage: S3Storage,
    attachment: AssignmentAttachment,
) -> None:
    """删除附件：先删记录，再尽力删除对象（契约 8.15）。

    顺序是刻意的：记录删掉后附件立刻从所有列表消失，用户看到的就是"删掉了"；
    对象删除失败只留下一个不再被引用的孤立对象，不会让删除操作报错。
    """
    object_key = attachment.object_key
    await session.delete(attachment)
    await session.commit()
    _try_delete_object(storage, object_key)


async def presign_existing_download(
    storage: S3Storage,
    attachment: AssignmentAttachment,
    *,
    settings: Settings,
) -> PresignedDownload:
    """为单个附件签发下载地址（重复删除等需要回显的场景）。"""
    return presign_download(
        storage,
        attachment.object_key,
        ttl_seconds=settings.storage_upload_url_ttl_seconds,
    )


__all__ = [
    "AttachmentView",
    "complete_upload",
    "delete_attachment",
    "init_upload",
    "list_attachments",
    "presign_existing_download",
]
