"""Dashboard HTTP 路由（``docs/api-contract.md`` 第 11 节）。

两个只读聚合接口，只做协议转换与依赖注入：

- ``GET /dashboard/teacher``：仅 ``TEACHER``，学生访问 ``403 ROLE_FORBIDDEN``；
- ``GET /dashboard/student``：仅 ``STUDENT``，教师访问 ``403 ROLE_FORBIDDEN``。

均不接受请求体与查询参数，每个列表固定返回最近 5 项，无分页。
业务规则全部在 :mod:`app.modules.dashboard.service`。
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.core.schemas import ErrorResponse
from app.db.session import SessionDep
from app.modules.auth.permissions import StudentDep, TeacherDep
from app.modules.dashboard import service
from app.modules.dashboard.schemas import StudentDashboard, TeacherDashboard

dashboard_router = APIRouter(tags=["dashboard"])

#: 两个接口共有的认证错误
_AUTH_ERRORS: dict[int, dict[str, object]] = {
    401: {
        "model": ErrorResponse,
        "description": "Access Token 缺失、无效或已过期（AUTH_TOKEN_EXPIRED）",
    },
}


@dashboard_router.get(
    "/dashboard/teacher",
    status_code=status.HTTP_200_OK,
    response_model=TeacherDashboard,
    summary="教师工作台摘要",
    description=(
        "仅教师可访问；学生访问返回 403 ROLE_FORBIDDEN。"
        "统计范围只包含该教师创建且仍为 ACTIVE 的课程，不包含归档课程、"
        "其他教师的课程与已删除资料。返回活动课程数、待批改正式提交数"
        "（SUBMITTED / GRADING / REVIEW_REQUIRED / FAILED）、失败资料数，"
        "以及最近 5 份正式提交（按 submitted_at DESC, id DESC）与最近 5 份"
        "失败资料（按 updated_at DESC, id DESC）。UPLOADING 提交不进入列表。"
    ),
    responses={
        403: {
            "model": ErrorResponse,
            "description": "学生访问返回 ROLE_FORBIDDEN",
        },
        **_AUTH_ERRORS,
    },
)
async def get_teacher_dashboard(
    user: TeacherDep,
    session: SessionDep,
) -> TeacherDashboard:
    return await service.get_teacher_dashboard(session, user=user)


@dashboard_router.get(
    "/dashboard/student",
    status_code=status.HTTP_200_OK,
    response_model=StudentDashboard,
    summary="学生工作台摘要",
    description=(
        "仅学生可访问；教师访问返回 403 ROLE_FORBIDDEN。"
        "统计范围只包含本人加入且仍为 ACTIVE 的课程。返回活动课程数、"
        "待完成任务数、处理中与失败资料数，以及最近 5 个待完成任务"
        "（按 due_at ASC NULLS LAST, published_at DESC, id DESC）、最近 5 条"
        "已发布反馈（按 published_at DESC, id DESC）与最近 5 份处理中或失败资料"
        "（按 updated_at DESC, id DESC）。待完成任务指状态 PUBLISHED、无截止或"
        "未到截止或允许补交、且本人尚无正式提交的任务；仅有 UPLOADING 记录仍算待完成。"
    ),
    responses={
        403: {
            "model": ErrorResponse,
            "description": "教师访问返回 ROLE_FORBIDDEN",
        },
        **_AUTH_ERRORS,
    },
)
async def get_student_dashboard(
    user: StudentDep,
    session: SessionDep,
) -> StudentDashboard:
    return await service.get_student_dashboard(session, user=user)


__all__ = ["dashboard_router"]
