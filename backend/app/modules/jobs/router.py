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
        "通用 Jobs 接口只覆盖三类任务：MATERIAL_PARSE（按资料可见性，"
        "资料删除后不可读）、PRACTICE_GENERATE（创建教师可读全部状态，"
        "学生仅在该练习已发布后可读）、SUBMISSION_GRADE（课程创建教师与提交本人可读）。"
        "任务不存在、不属于公开范围（含 AGENT_RUN）或当前用户不可见关联资源时，"
        "统一返回 404 RESOURCE_NOT_FOUND，不区分「不存在」与「不可见」；"
        "归档课程中的历史任务仍可读取。响应不包含 attempts、run_token、"
        "lease_expires_at 等内部调度字段。"
    ),
    responses={
        404: {
            "model": ErrorResponse,
            "description": "任务不存在、不属于公开范围，或当前用户无权查看（RESOURCE_NOT_FOUND）",
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
        "检查顺序固定为：认证 → 任务存在且属于公开范围（含 AGENT_RUN 一律 404）→ "
        "关联资源可见性 → 角色/管理权限 → 课程归档 → 任务与资源状态 → 请求体结构 → 写入。"
        "MATERIAL_PARSE 在完成资料可见性、角色与归档检查后返回 409 JOB_NOT_RETRYABLE"
        "（改走 POST /materials/{id}/parse，见 5.3）；PRACTICE_GENERATE 仅 FAILED 或"
        "租约已过期的 RUNNING 可重置，其余状态 409 JOB_NOT_RETRYABLE；"
        "SUBMISSION_GRADE 与 POST /submissions/{id}/grade 共用逻辑（9.6），"
        "排队或有效运行中幂等返回，已有复核结果或已发布返回 409 SUBMISSION_NOT_READY。"
        "重试复用原资源与 job ID，清除运行令牌、租约、错误与旧产物，"
        "重置为 PENDING / progress=0。请求体可省略或传 {}。"
    ),
    responses={
        202: {"description": "已重置，返回同一个任务"},
        403: {
            "model": ErrorResponse,
            "description": "学生调用为 ROLE_FORBIDDEN；非创建教师为 COURSE_FORBIDDEN",
        },
        404: {
            "model": ErrorResponse,
            "description": "任务不存在、不属于公开范围或不可见（RESOURCE_NOT_FOUND）",
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "课程已归档（COURSE_ARCHIVED）、状态不可重试（JOB_NOT_RETRYABLE）"
                "或提交不可批改（SUBMISSION_NOT_READY）"
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": "请求体为显式 null、非法 JSON/UTF-8 或含未声明字段（VALIDATION_ERROR）",
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
    if isinstance(target, service.SubmissionRetryTarget):
        job = await service.retry_submission_grade_job(session, target=target)
    else:
        job = await practice_service.retry_practice_generate_job(
            session, job=target.job, practice_set=target.practice_set
        )
    return JobStatus.model_validate(job)
