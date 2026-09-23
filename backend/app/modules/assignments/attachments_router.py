"""作业附件的 HTTP 路由（``docs/api-contract.md`` 8.15）。

只做协议转换与依赖注入，业务规则全部在
:mod:`app.modules.assignments.attachments`。

上传沿用课件同一套三段式（初始化 → 浏览器直传 → 完成确认），
因此请求头与错误语义与第 4 节保持一致；差异只有两处：

- 路径挂在**任务**下（``/assignments/{id}/attachments``）；
- **学生可读、只有课程创建教师可写** —— 附件是教师给的参考资料。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response, status

from app.core.deps import SettingsDep
from app.core.errors import ResourceNotFoundError
from app.core.request_body import EMPTY_OBJECT_REQUEST_BODY, validate_empty_object_body
from app.core.schemas import ErrorResponse
from app.db.session import SessionDep
from app.modules.assignments import attachments as service
from app.modules.assignments.attachment_schemas import (
    AssignmentAttachmentSchema,
    AttachmentUploadInitRequest,
    AttachmentUploadInitResponse,
)
from app.modules.assignments.deps import (
    AttachmentAssignmentDep,
    VisibleAssignmentDep,
)
from app.modules.assignments.models import AssignmentAttachment
from app.modules.assignments.repository import get_attachment_by_id
from app.modules.auth import repository as auth_repo
from app.modules.auth.permissions import CurrentUserDep
from app.storage import PresignedDownload
from app.storage.deps import StorageDep

attachments_router = APIRouter(tags=["assignments"])

_AUTH_ERRORS: dict[int, dict[str, object]] = {
    401: {
        "model": ErrorResponse,
        "description": "Access Token 缺失、无效或已过期（AUTH_TOKEN_EXPIRED）",
    }
}

_FORBIDDEN: dict[str, object] = {
    "model": ErrorResponse,
    "description": "学生调用为 ROLE_FORBIDDEN；非创建教师为 COURSE_FORBIDDEN",
}

_NOT_FOUND_UNIFIED: dict[str, object] = {
    "model": ErrorResponse,
    "description": (
        "任务或附件不存在、对当前用户不可见，或不是课程成员"
        "（统一 RESOURCE_NOT_FOUND）"
    ),
}

_ARCHIVED: dict[str, object] = {
    "model": ErrorResponse,
    "description": "课程已归档（COURSE_ARCHIVED）或任务已归档（ASSIGNMENT_NOT_OPEN）",
}

_UPLOAD_INVALID: dict[str, object] = {
    "model": ErrorResponse,
    "description": (
        "文件类型、MIME、大小、sha256 不满足规则，或对象缺失/不一致、"
        "确认窗口已过（UPLOAD_INVALID）"
    ),
}

_VALIDATION: dict[str, object] = {
    "model": ErrorResponse,
    "description": "请求结构、字段类型或未知字段不合法（VALIDATION_ERROR）",
}

_STORAGE: dict[str, object] = {
    "model": ErrorResponse,
    "description": "对象存储未配置或不可达（SERVICE_UNAVAILABLE）",
}


def _schema(
    attachment: AssignmentAttachment,
    download: PresignedDownload,
    uploaded_by_name: str | None,
) -> AssignmentAttachmentSchema:
    return AssignmentAttachmentSchema(
        id=attachment.id,
        assignment_id=attachment.assignment_id,
        filename=attachment.filename,
        content_type=attachment.content_type,
        size=attachment.size,
        sha256=attachment.sha256,
        uploaded_by=attachment.uploaded_by,
        uploaded_by_name=uploaded_by_name,
        download_url=download.url,
        download_expires_at=download.expires_at,
        # 对用户有意义的时间是"附件生效的时间"，pending 记录不该出现在响应里
        created_at=attachment.completed_at or attachment.created_at,
    )


async def _single_schema(
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
    attachment: AssignmentAttachment,
) -> AssignmentAttachmentSchema:
    """单条附件的响应：现场签地址 + 补上传者显示名。"""
    download = await service.presign_existing_download(
        storage, attachment, settings=settings
    )
    summaries = await auth_repo.get_users_public_summaries(
        session, {attachment.uploaded_by}
    )
    summary = summaries.get(attachment.uploaded_by)
    return _schema(attachment, download, summary.display_name if summary else None)


@attachments_router.get(
    "/assignments/{assignment_id}/attachments",
    status_code=status.HTTP_200_OK,
    response_model=list[AssignmentAttachmentSchema],
    summary="作业附件列表",
    description=(
        "课程成员（教师或学生）可读；草稿任务对学生按不存在处理（404）。"
        "只返回**已完成**上传的附件，未完成的草稿态不可见；"
        "每个附件都带现场签发的短时下载地址。"
    ),
    responses={
        200: {"description": "查询成功"},
        404: _NOT_FOUND_UNIFIED,
        **_AUTH_ERRORS,
    },
)
async def list_attachments(
    assignment: VisibleAssignmentDep,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> list[AssignmentAttachmentSchema]:
    views = await service.list_attachments(
        session, assignment=assignment, storage=storage, settings=settings
    )
    return [
        _schema(view.attachment, view.download, view.uploaded_by_name) for view in views
    ]


@attachments_router.post(
    "/assignments/{assignment_id}/attachments/uploads",
    status_code=status.HTTP_201_CREATED,
    response_model=AttachmentUploadInitResponse,
    summary="初始化附件上传",
    description=(
        "仅课程创建教师可调用。返回预签名 PUT 地址与必须原样携带的请求头；"
        "只接受 PDF、PPTX、DOCX，单文件不超过 `ASSIGNMENT_ATTACHMENT_MAX_UPLOAD_BYTES`。"
        "同一任务下已有**同名**附件时返回 409 ATTACHMENT_ALREADY_EXISTS"
        "（请先删除旧附件）；重复初始化会复用未完成的上传记录并换发新的地址。"
    ),
    responses={
        201: {"description": "已签发上传地址"},
        403: _FORBIDDEN,
        404: _NOT_FOUND_UNIFIED,
        409: {
            "model": ErrorResponse,
            "description": (
                "课程或任务已归档（COURSE_ARCHIVED / ASSIGNMENT_NOT_OPEN），"
                "或已有同名附件（ATTACHMENT_ALREADY_EXISTS）"
            ),
        },
        422: _UPLOAD_INVALID,
        503: _STORAGE,
        **_AUTH_ERRORS,
    },
)
async def init_attachment_upload(
    payload: AttachmentUploadInitRequest,
    assignment: AttachmentAssignmentDep,
    user: CurrentUserDep,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> AttachmentUploadInitResponse:
    attachment, presigned = await service.init_upload(
        session,
        storage=storage,
        settings=settings,
        assignment=assignment,
        user=user,
        payload=payload,
    )
    return AttachmentUploadInitResponse(
        upload_id=attachment.upload_id,
        upload_url=presigned.url,
        method=presigned.method,
        headers=presigned.headers,
        expires_at=presigned.expires_at,
        confirm_deadline_at=attachment.confirm_deadline_at,
    )


@attachments_router.post(
    "/assignments/{assignment_id}/attachments/uploads/{upload_id}/complete",
    status_code=status.HTTP_201_CREATED,
    response_model=AssignmentAttachmentSchema,
    summary="完成附件上传",
    description=(
        "仅课程创建教师可调用。确认对象存在且大小、类型、摘要与初始化声明一致后，"
        "附件正式生效并对课程成员可见；同一 `upload_id` 重复确认**幂等**返回。"
        "请求体可省略或传 {}。"
    ),
    responses={
        201: {"description": "附件已生效（含幂等重放）"},
        403: _FORBIDDEN,
        404: _NOT_FOUND_UNIFIED,
        409: {
            "model": ErrorResponse,
            "description": "课程或任务已归档，或已有同名附件（ATTACHMENT_ALREADY_EXISTS）",
        },
        422: {
            **_UPLOAD_INVALID,
            "description": (
                "对象缺失或与初始化声明不一致、确认窗口已过（UPLOAD_INVALID），"
                "或请求体不为空对象（VALIDATION_ERROR）"
            ),
        },
        503: _STORAGE,
        **_AUTH_ERRORS,
    },
    openapi_extra=EMPTY_OBJECT_REQUEST_BODY,
)
async def complete_attachment_upload(
    upload_id: uuid.UUID,
    request: Request,
    assignment: AttachmentAssignmentDep,
    session: SessionDep,
    storage: StorageDep,
    settings: SettingsDep,
) -> AssignmentAttachmentSchema:
    # 完成确认没有请求字段：只接受省略请求体或 {}
    await validate_empty_object_body(request)

    attachment = await service.complete_upload(
        session, storage=storage, assignment=assignment, upload_id=upload_id
    )
    return await _single_schema(session, storage, settings, attachment)


@attachments_router.delete(
    "/assignments/{assignment_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除作业附件",
    description=(
        "仅课程创建教师可调用。删除后附件立即从列表消失；对象存储中的文件尽力删除，"
        "删除失败不影响本次删除结果。附件不存在或不属于该任务时返回 404。"
    ),
    responses={
        204: {"description": "删除成功"},
        403: _FORBIDDEN,
        404: _NOT_FOUND_UNIFIED,
        409: _ARCHIVED,
        **_AUTH_ERRORS,
    },
)
async def delete_attachment(
    attachment_id: uuid.UUID,
    assignment: AttachmentAssignmentDep,
    session: SessionDep,
    storage: StorageDep,
) -> Response:
    attachment = await get_attachment_by_id(session, attachment_id)
    # 必须属于该任务、且已确认完成：pending 记录不该被当成附件删除
    if (
        attachment is None
        or attachment.assignment_id != assignment.id
        or attachment.completed_at is None
    ):
        raise ResourceNotFoundError()

    await service.delete_attachment(session, storage=storage, attachment=attachment)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["attachments_router"]
