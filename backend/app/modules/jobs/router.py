"""Jobs HTTP 路由。

只做协议转换与依赖注入，业务规则全部在 :mod:`app.modules.jobs.service`。
路径、状态码与响应结构以 ``docs/api-contract.md`` 第 10 节与 4.7 节为准。

第一版只落地课件上传链路产生的 ``MATERIAL_PARSE`` 任务查询；
``/jobs/{job_id}/retry`` 属后续阶段（需接入 Worker）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.core.schemas import ErrorResponse
from app.db.session import SessionDep
from app.modules.auth.permissions import CurrentUserDep
from app.modules.jobs import service
from app.modules.jobs.schemas import JobStatus

jobs_router = APIRouter(tags=["jobs"])


@jobs_router.get(
    "/jobs/{job_id}",
    status_code=status.HTTP_200_OK,
    response_model=JobStatus,
    summary="查询任务状态",
    description=(
        "任务不存在，或当前用户不是任务关联资源所属课程的成员时，"
        "统一返回 404 RESOURCE_NOT_FOUND，不区分「不存在」与「不可见」。"
        "第一版只接入 MATERIAL_PARSE（课件解析）任务，其可见性等同于"
        "资料所属课程的成员；归档课程的任务仍可读。"
    ),
    responses={
        401: {
            "model": ErrorResponse,
            "description": "Access Token 缺失、无效或已过期（AUTH_TOKEN_EXPIRED）",
        },
        404: {
            "model": ErrorResponse,
            "description": "任务不存在，或当前用户无权查看（RESOURCE_NOT_FOUND）",
        },
    },
)
async def get_job(
    job_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> JobStatus:
    job = await service.get_job_for_viewer(session, user=user, job_id=job_id)
    return JobStatus.model_validate(job)
