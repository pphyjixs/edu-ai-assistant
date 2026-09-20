"""Materials HTTP 路由（初始化、完成上传与资料状态查询）。

只做协议转换与依赖注入，业务规则全部在 :mod:`app.modules.materials.service`。
路径、状态码与响应结构以 ``docs/api-contract.md`` 第 4 节为准。

任务状态查询由 jobs 路由提供；过期会话清理由独立维护命令执行。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.deps import SettingsDep
from app.core.schemas import ErrorResponse
from app.db.session import SessionDep
from app.modules.auth.permissions import CurrentUserDep, TeacherDep
from app.modules.jobs.schemas import JobStatus
from app.modules.materials import service
from app.modules.materials.schemas import (
    MaterialDetail,
    MaterialUploadCompleteRequest,
    MaterialUploadCompleteResponse,
    MaterialUploadInitRequest,
    MaterialUploadInitResponse,
)
from app.storage.deps import StorageDep

materials_router = APIRouter(tags=["materials"])

#: 所有上传接口都可能返回的认证错误
_AUTH_ERRORS: dict[int, dict[str, object]] = {
    401: {
        "model": ErrorResponse,
        "description": "Access Token 缺失、无效或已过期（AUTH_TOKEN_EXPIRED）",
    },
}

#: 上传接口共有的业务错误
_UPLOAD_ERRORS: dict[int, dict[str, object]] = {
    403: {
        "model": ErrorResponse,
        "description": ("学生调用返回 ROLE_FORBIDDEN；非创建教师返回 COURSE_FORBIDDEN"),
    },
    404: {
        "model": ErrorResponse,
        "description": "课程或上传会话不存在（RESOURCE_NOT_FOUND）",
    },
    409: {
        "model": ErrorResponse,
        "description": "课程已归档，不能新增资料（COURSE_ARCHIVED）",
    },
    503: {
        "model": ErrorResponse,
        "description": "对象存储未配置或不可达（SERVICE_UNAVAILABLE）",
    },
    **_AUTH_ERRORS,
}


@materials_router.post(
    "/courses/{course_id}/materials/uploads",
    status_code=status.HTTP_201_CREATED,
    response_model=MaterialUploadInitResponse,
    summary="初始化课件上传",
    description=(
        "仅课程创建教师可调用。校验文件元数据后创建上传会话，返回预签名 PUT 地址。"
        "文件字节由浏览器按该地址直传对象存储，不经过后端。"
        "expires_at 是 PUT 地址的到期时间（10 分钟），"
        "confirm_deadline_at 是完成确认的截止时间（24 小时）。"
    ),
    responses={
        201: {"description": "上传会话已创建，返回预签名信息"},
        422: {
            "model": ErrorResponse,
            "description": (
                "请求结构不合法（VALIDATION_ERROR）；"
                "文件类型、MIME、大小或 sha256 不合规（UPLOAD_INVALID）"
            ),
        },
        **_UPLOAD_ERRORS,
    },
)
async def init_material_upload(
    course_id: uuid.UUID,
    payload: MaterialUploadInitRequest,
    teacher: TeacherDep,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> MaterialUploadInitResponse:
    upload, presigned = await service.init_upload(
        session,
        user=teacher,
        course_id=course_id,
        payload=payload,
        settings=settings,
        storage=storage,
    )
    return MaterialUploadInitResponse(
        upload_id=upload.id,
        upload_url=presigned.url,
        method=presigned.method,
        headers=presigned.headers,
        expires_at=presigned.expires_at,
        confirm_deadline_at=upload.confirm_deadline_at,
    )


@materials_router.post(
    "/courses/{course_id}/materials/uploads/{upload_id}/complete",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=MaterialUploadCompleteResponse,
    summary="完成课件上传",
    description=(
        "仅课程创建教师可调用。确认对象存在且大小、类型、SHA-256 与初始化声明一致后，"
        "在同一事务中创建资料（PROCESSING）与 MATERIAL_PARSE 任务（PENDING）。"
        "重复确认（含并发）返回同一份 202 结果，不产生第二份资料或任务。"
        "第一版不实现解析 Worker，资料与任务状态不会自动推进。"
    ),
    responses={
        202: {"description": "已受理，返回资料与解析任务"},
        422: {
            "model": ErrorResponse,
            "description": (
                "请求结构不合法（VALIDATION_ERROR）；"
                "对象缺失、大小/类型/校验值不符或确认窗口已过（UPLOAD_INVALID）"
            ),
        },
        **_UPLOAD_ERRORS,
    },
)
async def complete_material_upload(
    course_id: uuid.UUID,
    upload_id: uuid.UUID,
    teacher: TeacherDep,
    session: SessionDep,
    storage: StorageDep,
    # 该接口没有请求字段：只接受省略请求体或空对象 {}，多余字段返回 422
    _payload: MaterialUploadCompleteRequest | None = None,
) -> MaterialUploadCompleteResponse:
    result = await service.complete_upload(
        session,
        user=teacher,
        course_id=course_id,
        upload_id=upload_id,
        storage=storage,
    )
    return MaterialUploadCompleteResponse(
        material=MaterialDetail.model_validate(result.material),
        job=JobStatus.model_validate(result.job),
    )


@materials_router.get(
    "/materials/{material_id}",
    status_code=status.HTTP_200_OK,
    response_model=MaterialDetail,
    summary="资料详情与处理状态",
    description=(
        "资料所属课程的成员（教师或学生）可读；归档课程的资料仍可读。"
        "资料不存在，或当前用户不是课程成员时，统一返回 404 RESOURCE_NOT_FOUND，"
        "不区分「不存在」与「不可见」。第一版没有解析 Worker，"
        "完成确认后资料恒为 PROCESSING，状态不会自动推进。"
    ),
    responses={
        401: {
            "model": ErrorResponse,
            "description": "Access Token 缺失、无效或已过期（AUTH_TOKEN_EXPIRED）",
        },
        404: {
            "model": ErrorResponse,
            "description": "资料不存在，或当前用户不是课程成员（RESOURCE_NOT_FOUND）",
        },
    },
)
async def get_material(
    material_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> MaterialDetail:
    material = await service.get_material_for_member(
        session, user=user, material_id=material_id
    )
    return MaterialDetail.model_validate(material)
