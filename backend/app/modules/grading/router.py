"""Grading HTTP 路由（``docs/api-contract.md`` 第 9 节的八个接口）。

只做协议转换与响应组装，业务规则全部在 :mod:`app.modules.grading.service`。

写接口采用两阶段处理：守卫依赖先加锁并完成"成员可见性 → 角色 → 归档 → 状态"
检查（``deps.py``），路由再用 ``app.core.request_body`` 手工解析请求体，
因此错误优先级与契约 9.1 一致（非法请求体不会抢先于权限或归档错误返回）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, status

from app.core.deps import SettingsDep
from app.core.pagination import Page, PaginationDep
from app.core.request_body import (
    EMPTY_OBJECT_REQUEST_BODY,
    parse_required_object_body,
    validate_empty_object_body,
)
from app.core.schemas import ErrorResponse
from app.db.session import SessionDep
from app.modules.auth.permissions import CurrentUserDep
from app.modules.grading import service
from app.modules.grading.deps import (
    CreatorReviewDep,
    CreatorSubmissionDep,
    StudentAssignmentDep,
    StudentUploadDep,
)
from app.modules.grading.schemas import (
    GradeReviewDetailSchema,
    GradeReviewUpdateRequest,
    SubmissionDetailSchema,
    SubmissionSummarySchema,
    SubmissionUploadInitRequest,
    SubmissionUploadInitSchema,
)
from app.modules.jobs.schemas import JobStatus
from app.storage.deps import StorageDep

grading_router = APIRouter(tags=["grading"])

_AUTH_ERRORS: dict[int | str, dict] = {
    401: {
        "model": ErrorResponse,
        "description": "Access Token 缺失、无效或已过期（AUTH_TOKEN_EXPIRED）",
    }
}

_NOT_FOUND_UNIFIED = {
    "model": ErrorResponse,
    "description": (
        "资源不存在，或当前用户不可见（RESOURCE_NOT_FOUND；他人提交与未发布的"
        "批改一律按不存在处理）"
    ),
}

_STUDENT_ERRORS = {
    403: {
        "model": ErrorResponse,
        "description": "仅课程学生可调用；其他角色返回 ROLE_FORBIDDEN",
    },
    404: _NOT_FOUND_UNIFIED,
    409: {
        "model": ErrorResponse,
        "description": (
            "课程已归档（COURSE_ARCHIVED）、任务未发布或已关闭（ASSIGNMENT_NOT_OPEN）"
            "或已有正式提交（SUBMISSION_ALREADY_EXISTS）"
        ),
    },
    **_AUTH_ERRORS,
}

_TEACHER_ERRORS = {
    403: {
        "model": ErrorResponse,
        "description": (
            "学生调用为学生角色越权（ROLE_FORBIDDEN）；"
            "其他教师为非创建教师（COURSE_FORBIDDEN）"
        ),
    },
    404: _NOT_FOUND_UNIFIED,
    409: {
        "model": ErrorResponse,
        "description": (
            "课程已归档（COURSE_ARCHIVED）、提交未就绪（SUBMISSION_NOT_READY）、"
            "未完成复核（GRADE_NOT_REVIEWED）或成绩已发布（GRADE_ALREADY_PUBLISHED）"
        ),
    },
    **_AUTH_ERRORS,
}

_UPLOAD_INIT_REQUEST_BODY: dict = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/SubmissionUploadInitRequest"}
            }
        },
    }
}

_GRADE_REVIEW_REQUEST_BODY: dict = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/GradeReviewUpdateRequest"}
            }
        },
    }
}

#: 用原始 Request 手工解析、需要显式补进 OpenAPI 组件的请求模型
GRADING_REQUEST_MODELS = (SubmissionUploadInitRequest, GradeReviewUpdateRequest)


# --------------------------------------------------------------------------- #
# 9.2 初始化报告上传
# --------------------------------------------------------------------------- #
@grading_router.post(
    "/assignments/{assignment_id}/submissions/uploads",
    status_code=status.HTTP_201_CREATED,
    response_model=SubmissionUploadInitSchema,
    summary="初始化报告上传",
    description=(
        "仅课程学生可调用。任务必须已发布且仍允许提交；已存在正式提交时返回 "
        "`409 SUBMISSION_ALREADY_EXISTS`。重新初始化会复用仍处于 `UPLOADING` 的"
        "提交并签发**新的**上传会话与对象键，旧会话被标记为已替代。"
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
        **_STUDENT_ERRORS,
    },
    openapi_extra=_UPLOAD_INIT_REQUEST_BODY,
)
async def init_submission_upload(
    assignment_id: uuid.UUID,
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    guard: StudentAssignmentDep,
) -> SubmissionUploadInitSchema:
    # 守卫已按 课程 → Assignment → 当前 RubricVersion → Submission 加锁并检查
    payload = await parse_required_object_body(request, SubmissionUploadInitRequest)
    _submission, upload, presigned = await service.init_submission_upload(
        session,
        user=user,
        course=guard.course,
        assignment=guard.assignment,
        existing_submission=guard.submission,
        payload=payload,
        settings=settings,
        storage=storage,
    )
    return SubmissionUploadInitSchema(
        submission_id=upload.submission_id,
        upload_id=upload.id,
        upload_url=presigned.url,
        method=presigned.method,
        headers=presigned.headers,
        expires_at=presigned.expires_at,
        confirm_deadline_at=upload.confirm_deadline_at,
    )


# --------------------------------------------------------------------------- #
# 9.3 完成提交
# --------------------------------------------------------------------------- #
@grading_router.post(
    "/assignments/{assignment_id}/submissions/uploads/{upload_id}/complete",
    status_code=status.HTTP_201_CREATED,
    response_model=SubmissionDetailSchema,
    summary="完成提交",
    description=(
        "仅课程学生可调用。确认对象存在且大小、类型、SHA-256 与初始化声明一致后，"
        "写入提交时间、补交标志与**当时**的评分规则版本，状态置为 `SUBMITTED`。"
        "同一 upload 重复完成返回首次结果；请求体可省略或传 {}。"
    ),
    responses={
        201: {"description": "提交完成（含幂等重放）"},
        422: {
            "model": ErrorResponse,
            "description": (
                "请求结构不合法（VALIDATION_ERROR）；对象缺失、大小/类型/校验值不符"
                "或确认窗口已过（UPLOAD_INVALID）"
            ),
        },
        **_STUDENT_ERRORS,
    },
    openapi_extra=EMPTY_OBJECT_REQUEST_BODY,
)
async def complete_submission_upload(
    assignment_id: uuid.UUID,
    upload_id: uuid.UUID,
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
    guard: StudentUploadDep,
) -> SubmissionDetailSchema:
    await validate_empty_object_body(request)
    result = await service.complete_submission_upload(
        session,
        user=user,
        assignment=guard.assignment,
        upload=guard.upload,
        submission=guard.submission,
        rubric_version=guard.rubric_version,
        settings=settings,
        storage=storage,
    )
    return result.response


# --------------------------------------------------------------------------- #
# 9.4 提交列表
# --------------------------------------------------------------------------- #
@grading_router.get(
    "/assignments/{assignment_id}/submissions",
    status_code=status.HTTP_200_OK,
    response_model=Page[SubmissionSummarySchema],
    summary="提交列表",
    description=(
        "课程创建教师返回全班正式提交（`submitted_at` 非空），按 "
        "`submitted_at DESC, id DESC` 分页；学生只返回本人 0–1 条提交"
        "（未完成时返回空列表，刷新后仍可找回自己的记录）；"
        "其他教师返回 403 ROLE_FORBIDDEN。归档课程仍可读。"
    ),
    responses={
        200: {"description": "查询成功"},
        403: {
            "model": ErrorResponse,
            "description": "学生以外且非课程创建教师（ROLE_FORBIDDEN）",
        },
        404: _NOT_FOUND_UNIFIED,
        422: {
            "model": ErrorResponse,
            "description": "分页参数不合法（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
)
async def list_submissions(
    assignment_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    pagination: PaginationDep,
) -> Page[SubmissionSummarySchema]:
    items, total = await service.list_submissions(
        session, user=user, assignment_id=assignment_id, pagination=pagination
    )
    return Page[SubmissionSummarySchema](
        items=[service.submission_summary(item) for item in items],
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )


# --------------------------------------------------------------------------- #
# 9.5 提交详情
# --------------------------------------------------------------------------- #
@grading_router.get(
    "/submissions/{submission_id}",
    status_code=status.HTTP_200_OK,
    response_model=SubmissionDetailSchema,
    summary="提交详情",
    description=(
        "提交本人或课程创建教师可读；正式提交额外返回短时有效的 `download_url`"
        "（预签名 GET）与 `download_expires_at`。学生的提交在成绩发布前不含任何"
        "AI 分数或教师未发布分数。非成员、非本人与不存在的提交统一 404。"
    ),
    responses={
        200: {"description": "查询成功"},
        403: {
            "model": ErrorResponse,
            "description": "其他教师（非课程创建教师）返回 COURSE_FORBIDDEN",
        },
        404: _NOT_FOUND_UNIFIED,
        503: {
            "model": ErrorResponse,
            "description": "对象存储未配置或不可达（SERVICE_UNAVAILABLE）",
        },
        **_AUTH_ERRORS,
    },
)
async def get_submission(
    submission_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    settings: SettingsDep,
    storage: StorageDep,
) -> SubmissionDetailSchema:
    data = await service.get_submission_detail(
        session,
        user=user,
        submission_id=submission_id,
        settings=settings,
        storage=storage,
    )
    return service.submission_detail(data)


# --------------------------------------------------------------------------- #
# 9.6 触发或重试 AI 批改
# --------------------------------------------------------------------------- #
@grading_router.post(
    "/submissions/{submission_id}/grade",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobStatus,
    summary="触发或重试 AI 批改",
    description=(
        "仅课程创建教师可调用。`SUBMITTED` 首次触发创建唯一 `SUBMISSION_GRADE` 任务；"
        "`GRADING` 且任务在排队或租约有效时幂等返回原任务；`FAILED` 或租约过期的 "
        "`RUNNING` 复用原 job ID 重置为 `PENDING`；`REVIEW_REQUIRED`、`PUBLISHED` "
        "与未完成提交返回 409 SUBMISSION_NOT_READY。请求体可省略或传 {}。"
    ),
    responses={
        202: {"description": "已受理，返回批改任务"},
        404: _NOT_FOUND_UNIFIED,
        409: {
            "model": ErrorResponse,
            "description": (
                "课程已归档（COURSE_ARCHIVED）或提交不可批改（SUBMISSION_NOT_READY）"
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": "请求体为显式 null 或含未声明字段（VALIDATION_ERROR）",
        },
        **_TEACHER_ERRORS,
    },
    openapi_extra=EMPTY_OBJECT_REQUEST_BODY,
)
async def request_grade(
    submission_id: uuid.UUID,
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
    guard: CreatorSubmissionDep,
) -> JobStatus:
    # 守卫已按 课程 → Assignment → 提交版本 → Submission 加锁，请求体校验在其后
    await validate_empty_object_body(request)
    job = await service.request_grade(session, submission=guard.submission)
    return JobStatus.model_validate(job)


# --------------------------------------------------------------------------- #
# 9.7 批改详情
# --------------------------------------------------------------------------- #
@grading_router.get(
    "/submissions/{submission_id}/grade-review",
    status_code=status.HTTP_200_OK,
    response_model=GradeReviewDetailSchema,
    summary="批改详情",
    description=(
        "课程创建教师始终可读（含未复核、未发布）。学生仅在该提交 `PUBLISHED` 后"
        "可读本人批改，且响应中不包含 AI 原始建议分与差异（`ai_score`、"
        "`ai_comment`、`suggested_total_score`、`ai_summary` 为 null）。"
        "批改尚未生成时教师返回 409 SUBMISSION_NOT_READY，批改失败返回 502 AI_JOB_FAILED。"
    ),
    responses={
        200: {"description": "查询成功"},
        404: _NOT_FOUND_UNIFIED,
        409: {
            "model": ErrorResponse,
            "description": "批改尚未生成（SUBMISSION_NOT_READY）",
        },
        502: {
            "model": ErrorResponse,
            "description": "批改任务失败（AI_JOB_FAILED，details.job_id 指向任务）",
        },
        **_TEACHER_ERRORS,
    },
)
async def get_grade_review(
    submission_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> GradeReviewDetailSchema:
    data = await service.get_grade_review_detail(
        session, user=user, submission_id=submission_id
    )
    return service.grade_review_detail(data)


# --------------------------------------------------------------------------- #
# 9.8 教师复核
# --------------------------------------------------------------------------- #
@grading_router.patch(
    "/grade-reviews/{review_id}",
    status_code=status.HTTP_200_OK,
    response_model=GradeReviewDetailSchema,
    summary="教师复核",
    description=(
        "仅课程创建教师可调用。提交**完整快照**：`summary` 与 `items` 必填，"
        "items 必须恰好覆盖该提交所引用评分版本的全部评分项，不多不少、不重复。"
        "分数只接受 JSON number、最多两位小数且不超过该项满分，最终总分由分项求和。"
        "保存后写入复核人与复核时间，AI 原始字段保持不变。"
    ),
    responses={
        200: {"description": "复核已保存，返回批改详情"},
        404: _NOT_FOUND_UNIFIED,
        409: {
            "model": ErrorResponse,
            "description": (
                "课程已归档（COURSE_ARCHIVED）或成绩已发布（GRADE_ALREADY_PUBLISHED）"
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "请求结构、字段类型/长度不合法，或评分项未覆盖评分版本（VALIDATION_ERROR）"
            ),
        },
        **_TEACHER_ERRORS,
    },
    openapi_extra=_GRADE_REVIEW_REQUEST_BODY,
)
async def update_grade_review(
    review_id: uuid.UUID,
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
    guard: CreatorReviewDep,
) -> GradeReviewDetailSchema:
    payload = await parse_required_object_body(request, GradeReviewUpdateRequest)
    data = await service.update_grade_review(
        session,
        user=user,
        review=guard.review,
        submission=guard.submission,
        payload=payload,
    )
    return service.grade_review_detail(data)


# --------------------------------------------------------------------------- #
# 9.9 发布成绩
# --------------------------------------------------------------------------- #
@grading_router.post(
    "/grade-reviews/{review_id}/publish",
    status_code=status.HTTP_200_OK,
    response_model=GradeReviewDetailSchema,
    summary="发布成绩",
    description=(
        "仅课程创建教师可调用。必须已完成教师复核，否则返回 409 GRADE_NOT_REVIEWED；"
        "发布后写入 `published_at` 并把提交置为 `PUBLISHED`；重复发布幂等，"
        "不覆盖首次 `published_at`。请求体可省略或传 {}。"
    ),
    responses={
        200: {"description": "发布成功（含幂等重放）"},
        404: _NOT_FOUND_UNIFIED,
        409: {
            "model": ErrorResponse,
            "description": (
                "课程已归档（COURSE_ARCHIVED）、批改未生成（SUBMISSION_NOT_READY）"
                "或未完成复核（GRADE_NOT_REVIEWED）"
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": "请求体为显式 null 或含未声明字段（VALIDATION_ERROR）",
        },
        **_TEACHER_ERRORS,
    },
    openapi_extra=EMPTY_OBJECT_REQUEST_BODY,
)
async def publish_grade_review(
    review_id: uuid.UUID,
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
    guard: CreatorReviewDep,
) -> GradeReviewDetailSchema:
    await validate_empty_object_body(request)
    data = await service.publish_grade_review(
        session, user=user, review=guard.review, submission=guard.submission
    )
    return service.grade_review_detail(data)


__all__ = ["GRADING_REQUEST_MODELS", "grading_router"]
