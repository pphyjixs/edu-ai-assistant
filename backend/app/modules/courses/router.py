"""Courses HTTP 路由。

只做协议转换与依赖注入，业务规则全部在 :mod:`app.modules.courses.service`。
路径、状态码与响应结构以 ``docs/api-contract.md`` 第 3 节为准。

邀请码可见性由普通详情与含邀请码详情两个响应模型表达。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from app.core.pagination import Page, PaginationDep
from app.core.schemas import ErrorResponse
from app.db.session import SessionDep
from app.modules.auth.permissions import CurrentUserDep, StudentDep, TeacherDep
from app.modules.courses import service
from app.modules.courses.models import Course
from app.modules.courses.schemas import (
    CourseCreateRequest,
    CourseDetail,
    CourseDetailWithInviteCode,
    CourseJoinRequest,
    CourseMemberSummary,
    CourseSummary,
    CourseUpdateRequest,
    InviteCodeResponse,
)

courses_router = APIRouter(prefix="/courses", tags=["courses"])

#: 所有课程接口都可能返回的认证错误
_AUTH_ERRORS: dict[int, dict[str, object]] = {
    401: {
        "model": ErrorResponse,
        "description": "Access Token 缺失、无效或已过期（AUTH_TOKEN_EXPIRED）",
    },
}


def _course_detail(course: Course) -> CourseDetail:
    return CourseDetail.model_validate(course)


def _teacher_detail(course: Course, invite_code: str) -> CourseDetailWithInviteCode:
    return CourseDetailWithInviteCode(
        **CourseSummary.model_validate(course).model_dump(), invite_code=invite_code
    )


@courses_router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=CourseDetailWithInviteCode,
    summary="创建课程",
    description=(
        "仅教师可创建。创建教师自动成为课程内 TEACHER 成员；"
        "邀请码由服务端生成，冲突时自动重新生成。"
    ),
    responses={
        201: {"description": "创建成功，响应含当前邀请码"},
        403: {
            "model": ErrorResponse,
            "description": "当前角色无权创建课程（ROLE_FORBIDDEN）",
        },
        422: {
            "model": ErrorResponse,
            "description": "请求参数不合法（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
)
async def create_course(
    payload: CourseCreateRequest, teacher: TeacherDep, session: SessionDep
) -> CourseDetailWithInviteCode:
    course = await service.create_course(session, teacher=teacher, payload=payload)
    # 创建者即创建教师，且课程为 ACTIVE，邀请码必然可见
    return _teacher_detail(course, course.invite_code)


@courses_router.get(
    "",
    response_model=Page[CourseSummary],
    summary="我参加的课程",
    description="包含活动课程与归档课程，按创建时间倒序、ID 倒序分页返回；不含邀请码。",
    responses={
        200: {"description": "查询成功"},
        422: {
            "model": ErrorResponse,
            "description": "分页参数不合法（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
)
async def list_courses(
    current_user: CurrentUserDep,
    session: SessionDep,
    pagination: PaginationDep,
) -> Page[CourseSummary]:
    courses, total = await service.list_my_courses(
        session, user=current_user, pagination=pagination
    )
    return Page[CourseSummary](
        items=[CourseSummary.model_validate(course) for course in courses],
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )


@courses_router.post(
    "/join",
    status_code=status.HTTP_201_CREATED,
    response_model=CourseSummary,
    summary="使用邀请码加入课程",
    description=(
        "仅学生可加入。首次加入返回 201；已是成员返回 200 且不新增成员记录，"
        "并发重复加入同样保证唯一。旧邀请码返回 422 INVITE_CODE_INVALID，"
        "归档课程返回 409 COURSE_ARCHIVED（即使已经加入）。"
    ),
    responses={
        201: {"description": "首次加入成功"},
        200: {"description": "已是该课程成员，返回现有课程信息"},
        403: {
            "model": ErrorResponse,
            "description": "教师不能加入课程（ROLE_FORBIDDEN）",
        },
        409: {
            "model": ErrorResponse,
            "description": "课程已归档（COURSE_ARCHIVED）",
        },
        422: {
            "model": ErrorResponse,
            "description": "邀请码无效（INVITE_CODE_INVALID）或请求不合法",
        },
        **_AUTH_ERRORS,
    },
)
async def join_course(
    payload: CourseJoinRequest,
    student: StudentDep,
    session: SessionDep,
    response: Response,
) -> CourseSummary:
    course, created = await service.join_course(
        session, user=student, payload=payload
    )
    response.status_code = (
        status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )
    return CourseSummary.model_validate(course)


@courses_router.get(
    "/{course_id}",
    response_model=CourseDetailWithInviteCode | CourseDetail,
    summary="课程详情",
    description=(
        "课程成员可读；归档课程仍可读。"
        "invite_code 仅创建教师查看未归档课程时返回，其余情况省略该字段。"
    ),
    responses={
        200: {"description": "查询成功"},
        403: {
            "model": ErrorResponse,
            "description": "不是课程成员（COURSE_FORBIDDEN）",
        },
        404: {
            "model": ErrorResponse,
            "description": "课程不存在（RESOURCE_NOT_FOUND）",
        },
        422: {
            "model": ErrorResponse,
            "description": "课程 ID 不是合法 UUID（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
)
async def get_course(
    course_id: uuid.UUID, current_user: CurrentUserDep, session: SessionDep
) -> CourseDetailWithInviteCode | CourseDetail:
    course, invite_code = await service.get_course_detail(
        session, user=current_user, course_id=course_id
    )
    if invite_code is None:
        return _course_detail(course)
    return _teacher_detail(course, invite_code)


@courses_router.patch(
    "/{course_id}",
    response_model=CourseDetailWithInviteCode,
    summary="修改课程",
    description=(
        "仅创建教师可修改。省略的字段保持原值，description 传空字符串表示清空；"
        "空请求体与显式 null 均返回 422。"
    ),
    responses={
        200: {"description": "修改成功"},
        403: {
            "model": ErrorResponse,
            "description": "不是创建教师（COURSE_FORBIDDEN）",
        },
        404: {
            "model": ErrorResponse,
            "description": "课程不存在（RESOURCE_NOT_FOUND）",
        },
        409: {
            "model": ErrorResponse,
            "description": "课程已归档（COURSE_ARCHIVED）",
        },
        422: {
            "model": ErrorResponse,
            "description": "请求参数不合法（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
)
async def update_course(
    course_id: uuid.UUID,
    payload: CourseUpdateRequest,
    current_user: CurrentUserDep,
    session: SessionDep,
) -> CourseDetailWithInviteCode:
    course = await service.update_course(
        session, user=current_user, course_id=course_id, payload=payload
    )
    return _teacher_detail(course, course.invite_code)


@courses_router.post(
    "/{course_id}/archive",
    response_model=CourseDetail,
    summary="归档课程",
    description=(
        "仅创建教师可归档。归档后课程只读，不提供恢复接口；"
        "重复归档返回 200 和当前详情。归档响应不含邀请码。"
    ),
    responses={
        200: {"description": "归档成功或已归档"},
        403: {
            "model": ErrorResponse,
            "description": "不是创建教师（COURSE_FORBIDDEN）",
        },
        404: {
            "model": ErrorResponse,
            "description": "课程不存在（RESOURCE_NOT_FOUND）",
        },
        422: {
            "model": ErrorResponse,
            "description": "课程 ID 不是合法 UUID（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
)
async def archive_course(
    course_id: uuid.UUID, current_user: CurrentUserDep, session: SessionDep
) -> CourseDetail:
    course = await service.archive_course(
        session, user=current_user, course_id=course_id
    )
    # 归档课程一律省略邀请码
    return _course_detail(course)


@courses_router.post(
    "/{course_id}/invite-code",
    response_model=InviteCodeResponse,
    summary="重新生成邀请码",
    description="仅创建教师可重置；成功后旧码立即失效，归档课程返回 409 COURSE_ARCHIVED。",
    responses={
        200: {"description": "重置成功，返回新的邀请码"},
        403: {
            "model": ErrorResponse,
            "description": "不是创建教师（COURSE_FORBIDDEN）",
        },
        404: {
            "model": ErrorResponse,
            "description": "课程不存在（RESOURCE_NOT_FOUND）",
        },
        409: {
            "model": ErrorResponse,
            "description": "课程已归档（COURSE_ARCHIVED）",
        },
        422: {
            "model": ErrorResponse,
            "description": "课程 ID 不是合法 UUID（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
)
async def reset_invite_code(
    course_id: uuid.UUID, current_user: CurrentUserDep, session: SessionDep
) -> InviteCodeResponse:
    invite_code = await service.reset_invite_code(
        session, user=current_user, course_id=course_id
    )
    return InviteCodeResponse(invite_code=invite_code)


@courses_router.get(
    "/{course_id}/members",
    response_model=Page[CourseMemberSummary],
    summary="课程成员列表",
    description=(
        "仅创建教师可查看。包含创建教师与已加入学生，"
        "按加入时间正序、用户 ID 正序分页返回，不返回邮箱。"
    ),
    responses={
        200: {"description": "查询成功"},
        403: {
            "model": ErrorResponse,
            "description": "不是创建教师（COURSE_FORBIDDEN）",
        },
        404: {
            "model": ErrorResponse,
            "description": "课程不存在（RESOURCE_NOT_FOUND）",
        },
        422: {
            "model": ErrorResponse,
            "description": "分页参数或课程 ID 不合法（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
)
async def list_members(
    course_id: uuid.UUID,
    current_user: CurrentUserDep,
    session: SessionDep,
    pagination: PaginationDep,
) -> Page[CourseMemberSummary]:
    members, total = await service.list_course_members(
        session, user=current_user, course_id=course_id, pagination=pagination
    )
    return Page[CourseMemberSummary](
        items=members,
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )
