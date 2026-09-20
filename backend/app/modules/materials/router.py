"""Materials HTTP 路由（上传协议与资料读写的协议层）。

只做协议转换与依赖注入，业务规则全部在 :mod:`app.modules.materials.service`。
路径、状态码与响应结构以 ``docs/api-contract.md`` 第 4、5 节为准。

任务状态查询由 jobs 路由提供；过期会话清理由独立维护命令执行。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Response, status

from app.core.deps import SettingsDep
from app.core.pagination import Page, PaginationDep
from app.core.schemas import ErrorResponse
from app.db.session import SessionDep
from app.modules.auth.permissions import CurrentUserDep, TeacherDep
from app.modules.jobs.schemas import JobStatus
from app.modules.materials import service, worker
from app.modules.materials.schemas import (
    MaterialDetail,
    MaterialKnowledgePoint,
    MaterialOutline,
    MaterialSection,
    MaterialUploadCompleteRequest,
    MaterialUploadCompleteResponse,
    MaterialUploadInitRequest,
    MaterialUploadInitResponse,
    source_type_for_content_type,
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
    settings: SettingsDep,
    storage: StorageDep,
    background_tasks: BackgroundTasks,
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
    # 契约 5.5：完成确认后调度解析 Worker（响应返回后执行）
    worker.schedule_material_parse(
        background_tasks,
        settings=settings,
        material_id=result.material.id,
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
        "资料不存在、已删除，或当前用户不是课程成员时，统一返回 404 RESOURCE_NOT_FOUND，"
        "不区分「不存在」与「不可见」。状态由解析 Worker 推进（契约 5.5）。"
    ),
    responses={
        401: {
            "model": ErrorResponse,
            "description": "Access Token 缺失、无效或已过期（AUTH_TOKEN_EXPIRED）",
        },
        404: {
            "model": ErrorResponse,
            "description": "资料不存在、已删除，或当前用户不是课程成员（RESOURCE_NOT_FOUND）",
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


@materials_router.get(
    "/courses/{course_id}/materials",
    status_code=status.HTTP_200_OK,
    response_model=Page[MaterialDetail],
    summary="课程资料列表",
    description=(
        "课程成员（教师或学生）可读，含归档课程；不含已删除资料。"
        "按创建时间倒序、ID 倒序分页返回。课程不存在或非成员统一 404。"
    ),
    responses={
        200: {"description": "查询成功"},
        404: {
            "model": ErrorResponse,
            "description": "课程不存在，或当前用户不是课程成员（RESOURCE_NOT_FOUND）",
        },
        422: {
            "model": ErrorResponse,
            "description": "分页参数不合法（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
)
async def list_materials(
    course_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    pagination: PaginationDep,
) -> Page[MaterialDetail]:
    materials, total = await service.list_course_materials(
        session, user=user, course_id=course_id, pagination=pagination
    )
    return Page[MaterialDetail](
        items=[MaterialDetail.model_validate(material) for material in materials],
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )


@materials_router.delete(
    "/materials/{material_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除资料",
    description=(
        "仅课程创建教师可调用，标记删除：资料从列表、详情、大纲与重试解析中消失。"
        "首次删除归档课程返回 409 COURSE_ARCHIVED；同一教师重复删除幂等返回 204。"
        "其他用户对已删除资料视同不存在，统一 404。"
    ),
    responses={
        204: {"description": "删除成功（含幂等重放）"},
        403: {
            "model": ErrorResponse,
            "description": "学生调用返回 ROLE_FORBIDDEN；非创建教师返回 COURSE_FORBIDDEN",
        },
        404: {
            "model": ErrorResponse,
            "description": "资料不存在、已删除或当前用户不是课程成员（RESOURCE_NOT_FOUND）",
        },
        409: {
            "model": ErrorResponse,
            "description": "课程已归档，不能删除资料（COURSE_ARCHIVED）",
        },
        **_AUTH_ERRORS,
    },
)
async def delete_material(
    material_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    storage: StorageDep,
) -> Response:
    await service.delete_material(
        session, user=user, material_id=material_id, storage=storage
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@materials_router.post(
    "/materials/{material_id}/parse",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobStatus,
    summary="重试解析",
    description=(
        "仅课程创建教师可调用。失败任务复用原 job ID 重置为 PENDING 并由解析 Worker "
        "重新处理；处理中重复调用幂等返回同一任务；已就绪资料返回 409 MATERIAL_ALREADY_READY；"
        "归档课程返回 409 COURSE_ARCHIVED。"
    ),
    responses={
        202: {"description": "已受理，返回解析任务"},
        403: {
            "model": ErrorResponse,
            "description": "学生调用返回 ROLE_FORBIDDEN；非创建教师返回 COURSE_FORBIDDEN",
        },
        404: {
            "model": ErrorResponse,
            "description": "资料不存在、已删除或当前用户不是课程成员（RESOURCE_NOT_FOUND）",
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "课程已归档（COURSE_ARCHIVED）或资料已解析完成（MATERIAL_ALREADY_READY）"
            ),
        },
        **_AUTH_ERRORS,
    },
)
async def retry_material_parse(
    material_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    background_tasks: BackgroundTasks,
) -> JobStatus:
    job, _material, reset = await service.retry_parse(
        session, user=user, material_id=material_id
    )
    if reset:
        worker.schedule_material_parse(
            background_tasks, settings=settings, material_id=material_id, storage=storage
        )
    return JobStatus.model_validate(job)


@materials_router.get(
    "/materials/{material_id}/outline",
    status_code=status.HTTP_200_OK,
    response_model=MaterialOutline,
    summary="资料大纲与知识点",
    description=(
        "课程成员（教师或学生）可读，含归档课程。资料未就绪返回 409 MATERIAL_NOT_READY，"
        "解析失败返回 502 AI_JOB_FAILED（details.job_id 指向解析任务）。"
    ),
    responses={
        200: {"description": "查询成功"},
        404: {
            "model": ErrorResponse,
            "description": "资料不存在、已删除或当前用户不是课程成员（RESOURCE_NOT_FOUND）",
        },
        409: {
            "model": ErrorResponse,
            "description": "资料尚未解析完成（MATERIAL_NOT_READY）",
        },
        502: {
            "model": ErrorResponse,
            "description": "解析任务失败（AI_JOB_FAILED）",
        },
        **_AUTH_ERRORS,
    },
)
async def get_material_outline(
    material_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> MaterialOutline:
    material, sections = await service.get_material_outline(
        session, user=user, material_id=material_id
    )
    return MaterialOutline(
        material_id=material.id,
        sections=[
            MaterialSection(
                id=section.id,
                order=section.order,
                title=section.title,
                source_type=source_type_for_content_type(material.content_type),
                location_start=section.location_start,
                location_end=section.location_end,
                knowledge_points=[
                    MaterialKnowledgePoint.model_validate(point) for point in points
                ],
            )
            for section, points in sections
        ],
    )
