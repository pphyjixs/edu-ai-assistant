"""Jobs HTTP 路由。

只做协议转换与依赖注入，业务规则全部在 :mod:`app.modules.jobs.service`。
路径、状态码与响应结构以 ``docs/api-contract.md`` 第 10 节与 4.7 节为准。

已接入 ``MATERIAL_PARSE``（课件解析）与 ``PRACTICE_GENERATE``（练习生成）
两类任务的查询；练习生成任务可通过 ``POST /jobs/{job_id}/retry`` 重试，
资料解析的重试仍经由 ``POST /materials/{material_id}/parse``（契约 5.3）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, status

from app.core.request_body import (
    EMPTY_OBJECT_REQUEST_BODY,
    validate_empty_object_body,
)
from app.core.schemas import ErrorResponse
from app.db.session import SessionDep
from app.modules.auth.permissions import CurrentUserDep
from app.modules.jobs import service
from app.modules.jobs.deps import RetryTargetDep
from app.modules.jobs.models import JobType
from app.modules.jobs.schemas import JobStatus
from app.modules.practice import service as practice_service

jobs_router = APIRouter(tags=["jobs"])

_AUTH_ERRORS: dict[int | str, dict] = {
    401: {
        "model": ErrorResponse,
        "description": "Access Token 缺失、无效或已过期（AUTH_TOKEN_EXPIRED）",
    }
}


@jobs_router.get(
    "/jobs/{job_id}",
    status_code=status.HTTP_200_OK,
    response_model=JobStatus,
    summary="查询任务状态",
    description=(
        "任务不存在，或当前用户不可见任务关联资源时，统一返回 404 "
        "RESOURCE_NOT_FOUND，不区分「不存在」与「不可见」。"
        "MATERIAL_PARSE 按资料可见性；PRACTICE_GENERATE 按练习可见性"
        "（教师可读任意状态，学生仅在该练习已发布时可见）；归档课程仍可读。"
    ),
    responses={
        404: {
            "model": ErrorResponse,
            "description": "任务不存在，或当前用户无权查看（RESOURCE_NOT_FOUND）",
        },
        **_AUTH_ERRORS,
    },
)
async def get_job(
    job_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> JobStatus:
    job = await service.get_job_for_viewer(session, user=user, job_id=job_id)
    return JobStatus.model_validate(job)


@jobs_router.post(
    "/jobs/{job_id}/retry",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobStatus,
    summary="重试异步任务",
    description=(
        "仅资源管理者（课程创建教师）可调用。只接受 FAILED，或租约已过期的 "
        "RUNNING（崩溃遗留）；复用原资源与 job ID，清除运行令牌、租约与错误，"
        "重置为 PENDING 供 Worker 重新领取。"
        "MATERIAL_PARSE 任务返回 409 JOB_NOT_RETRYABLE（改走资料重试接口，见 5.3）；"
        "SUBMISSION_GRADE 任务按 9.6 的分流重置（与 `POST /submissions/{id}/grade` 共用逻辑），"
        "已有复核结果时返回 409 SUBMISSION_NOT_READY。请求体可省略或传 {}。"
    ),
    responses={
        202: {"description": "已重置，返回同一个任务"},
        403: {
            "model": ErrorResponse,
            "description": "学生调用为 ROLE_FORBIDDEN；非创建教师为 COURSE_FORBIDDEN",
        },
        404: {
            "model": ErrorResponse,
            "description": "任务不存在或不可见（RESOURCE_NOT_FOUND）",
        },
        409: {
            "model": ErrorResponse,
            "description": "课程已归档（COURSE_ARCHIVED）或状态不可重试（JOB_NOT_RETRYABLE）",
        },
        422: {
            "model": ErrorResponse,
            "description": "请求体为显式 null 或含未声明字段（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
    openapi_extra=EMPTY_OBJECT_REQUEST_BODY,
)
async def retry_job(
    job_id: uuid.UUID,
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
    target: RetryTargetDep,
) -> JobStatus:
    # 依赖已按 课程 → 资源 → 任务 的顺序加锁并完成全部检查，请求体校验在其后
    await validate_empty_object_body(request)
    if target.job_type is JobType.SUBMISSION_GRADE:
        job = await service.retry_submission_grade_job(session, target=target)
    else:
        job = await practice_service.retry_practice_generate_job(
            session, target=target.practice
        )
    return JobStatus.model_validate(job)
