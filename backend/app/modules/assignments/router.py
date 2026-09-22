"""Assignments HTTP 路由（``docs/api-contract.md`` 第 8 节的六个接口）。

只做协议转换与响应组装，业务规则全部在 :mod:`app.modules.assignments.service`。

写接口采用两阶段处理：守卫依赖先加锁并完成"成员可见性 → 角色 → 归档 → 状态"
检查（`deps.py`），路由再用 `app.core.request_body` 手工解析请求体，
因此错误优先级与契约 8.1 一致（非法请求体不会抢先于权限或归档错误返回）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, status

from app.core.pagination import Page, PaginationDep
from app.core.request_body import (
    EMPTY_OBJECT_REQUEST_BODY,
    parse_required_object_body,
    validate_empty_object_body,
)
from app.core.schemas import ErrorResponse
from app.db.session import SessionDep
from app.modules.assignments import service
from app.modules.assignments.deps import (
    ClosableAssignmentDep,
    CreatorCourseDep,
    EditableAssignmentDep,
    PublishableAssignmentDep,
)
from app.modules.assignments.models import Assignment, AssignmentRubricItem
from app.modules.assignments.schemas import (
    AssignmentCreateRequest,
    AssignmentDetailSchema,
    AssignmentSummarySchema,
    AssignmentUpdateRequest,
    RubricItemSchema,
)
from app.modules.auth.permissions import CurrentUserDep

assignments_router = APIRouter(tags=["assignments"])

_AUTH_ERRORS: dict[int | str, dict] = {
    401: {
        "model": ErrorResponse,
        "description": "Access Token 缺失、无效或已过期（AUTH_TOKEN_EXPIRED）",
    }
}

_NOT_FOUND_UNIFIED = {
    "model": ErrorResponse,
    "description": (
        "课程/任务不存在，或当前用户不可见（RESOURCE_NOT_FOUND；"
        "学生视角下的草稿一律按不存在处理）"
    ),
}

_CREATE_REQUEST_BODY: dict = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/AssignmentCreateRequest"}
            }
        },
    }
}

_UPDATE_REQUEST_BODY: dict = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/AssignmentUpdateRequest"}
            }
        },
    }
}

#: 用原始 Request 手工解析、需要显式补进 OpenAPI 组件的请求模型
ASSIGNMENT_REQUEST_MODELS = (AssignmentCreateRequest, AssignmentUpdateRequest)


# --------------------------------------------------------------------------- #
# 响应组装
# --------------------------------------------------------------------------- #
def _rubric_item_schema(item: AssignmentRubricItem) -> RubricItemSchema:
    return RubricItemSchema(
        id=item.id,
        title=item.title,
        description=item.description,
        max_score=float(item.max_score),
        order=item.order,
    )


def _summary_schema(
    assignment: Assignment, *, rubric_version: int, total_score: float
) -> AssignmentSummarySchema:
    return AssignmentSummarySchema(
        id=assignment.id,
        course_id=assignment.course_id,
        title=assignment.title,
        total_score=total_score,
        due_at=assignment.due_at,
        allow_late_submission=assignment.allow_late_submission,
        status=assignment.status,
        rubric_version=rubric_version,
        published_at=assignment.published_at,
        closed_at=assignment.closed_at,
        created_at=assignment.created_at,
        updated_at=assignment.updated_at,
    )


def _detail_schema(
    assignment: Assignment,
    *,
    rubric_version: int,
    total_score: float,
    rubric_items: list[AssignmentRubricItem],
) -> AssignmentDetailSchema:
    return AssignmentDetailSchema(
        **_summary_schema(
            assignment, rubric_version=rubric_version, total_score=total_score
        ).model_dump(),
        description=assignment.description,
        rubric_items=[_rubric_item_schema(item) for item in rubric_items],
    )


def _detail_from(detail: service.AssignmentDetail) -> AssignmentDetailSchema:
    """详情响应：总分取当前评分版本（版本缺失时为 0）。"""
    return _detail_schema(
        detail.assignment,
        rubric_version=detail.rubric_version,
        total_score=float(detail.total_score),
        rubric_items=detail.rubric_items,
    )


async def _detail(
    session: SessionDep, *, assignment: Assignment
) -> AssignmentDetailSchema:
    """写入后组装详情响应（重新读取当前评分版本与评分项）。"""
    version, items = await service.get_rubric_snapshot(session, assignment=assignment)
    return _detail_schema(
        assignment,
        rubric_version=version.version if version else 0,
        total_score=float(version.total_score) if version else 0.0,
        rubric_items=items,
    )


# --------------------------------------------------------------------------- #
# 8.2 创建任务
# --------------------------------------------------------------------------- #
@assignments_router.post(
    "/courses/{course_id}/assignments",
    status_code=status.HTTP_201_CREATED,
    response_model=AssignmentDetailSchema,
    summary="创建实验任务",
    description=(
        "仅课程创建教师可调用。创建 `DRAFT` 任务，并在同一事务写入评分规则版本 1。"
        "评分项分值之和必须精确等于 `total_score`。"
    ),
    responses={
        201: {"description": "创建成功，返回任务详情"},
        403: {
            "model": ErrorResponse,
            "description": "学生调用为 ROLE_FORBIDDEN；非创建教师为 COURSE_FORBIDDEN",
        },
        404: _NOT_FOUND_UNIFIED,
        409: {
            "model": ErrorResponse,
            "description": "课程已归档（COURSE_ARCHIVED）",
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "请求结构、字段类型/长度不合法（VALIDATION_ERROR），"
                "或评分项之和不等于总分（RUBRIC_SCORE_MISMATCH）"
            ),
        },
        **_AUTH_ERRORS,
    },
    openapi_extra=_CREATE_REQUEST_BODY,
)
async def create_assignment(
    course_id: uuid.UUID,
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
    course: CreatorCourseDep,
) -> AssignmentDetailSchema:
    payload = await parse_required_object_body(request, AssignmentCreateRequest)
    assignment = await service.create_assignment(
        session, course=course, user=user, payload=payload
    )
    return await _detail(session, assignment=assignment)


# --------------------------------------------------------------------------- #
# 8.3 任务列表
# --------------------------------------------------------------------------- #
@assignments_router.get(
    "/courses/{course_id}/assignments",
    status_code=status.HTTP_200_OK,
    response_model=Page[AssignmentSummarySchema],
    summary="课程实验任务列表",
    description=(
        "课程成员可读；教师可见全部状态，学生只看到已发布/已关闭/已归档"
        "（草稿在 SQL 查询层排除）。按 `created_at DESC, id DESC` 排序。"
    ),
    responses={
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
async def list_assignments(
    course_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    pagination: PaginationDep,
) -> Page[AssignmentSummarySchema]:
    items, total = await service.list_assignments(
        session, user=user, course_id=course_id, pagination=pagination
    )
    return Page[AssignmentSummarySchema](
        items=[
            _summary_schema(
                item.assignment,
                rubric_version=item.rubric_version,
                total_score=float(item.total_score),
            )
            for item in items
        ],
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )


# --------------------------------------------------------------------------- #
# 8.4 任务详情
# --------------------------------------------------------------------------- #
@assignments_router.get(
    "/assignments/{assignment_id}",
    status_code=status.HTTP_200_OK,
    response_model=AssignmentDetailSchema,
    summary="实验任务详情",
    description=(
        "课程教师可读全部状态；学生只能读已发布/已关闭/已归档，其余统一 404。"
        "返回当前评分规则版本与按 `order` 升序的评分项。"
    ),
    responses={404: _NOT_FOUND_UNIFIED, **_AUTH_ERRORS},
)
async def get_assignment(
    assignment_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> AssignmentDetailSchema:
    detail = await service.get_assignment_detail(
        session, user=user, assignment_id=assignment_id
    )
    return _detail_from(detail)


# --------------------------------------------------------------------------- #
# 8.5 修改任务
# --------------------------------------------------------------------------- #
@assignments_router.patch(
    "/assignments/{assignment_id}",
    status_code=status.HTTP_200_OK,
    response_model=AssignmentDetailSchema,
    summary="修改实验任务",
    description=(
        "仅课程创建教师可调用，`DRAFT`/`PUBLISHED` 可修改。字段全部可省略，"
        "至少提供一个；`due_at: null` 清除截止时间；`rubric_items` 出现时完整替换"
        "评分规则。评分规则实际变化时追加新版本，相同内容不增加版本。"
    ),
    responses={
        200: {"description": "修改成功，返回任务详情"},
        403: {
            "model": ErrorResponse,
            "description": "学生调用为 ROLE_FORBIDDEN；非创建教师为 COURSE_FORBIDDEN",
        },
        404: _NOT_FOUND_UNIFIED,
        409: {
            "model": ErrorResponse,
            "description": (
                "课程已归档（COURSE_ARCHIVED）或任务不可修改（ASSIGNMENT_NOT_OPEN）"
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "请求结构、字段类型/长度不合法或空对象（VALIDATION_ERROR），"
                "或评分项之和不等于总分（RUBRIC_SCORE_MISMATCH）"
            ),
        },
        **_AUTH_ERRORS,
    },
    openapi_extra=_UPDATE_REQUEST_BODY,
)
async def update_assignment(
    assignment_id: uuid.UUID,
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
    locked: EditableAssignmentDep,
) -> AssignmentDetailSchema:
    payload = await parse_required_object_body(request, AssignmentUpdateRequest)
    assignment = await service.update_assignment(
        session, assignment=locked, user=user, payload=payload
    )
    return await _detail(session, assignment=assignment)


# --------------------------------------------------------------------------- #
# 8.6 发布任务
# --------------------------------------------------------------------------- #
@assignments_router.post(
    "/assignments/{assignment_id}/publish",
    status_code=status.HTTP_200_OK,
    response_model=AssignmentDetailSchema,
    summary="发布实验任务",
    description=(
        "仅课程创建教师可调用。`DRAFT` 发布为 `PUBLISHED`；已发布幂等返回，"
        "不改变 `published_at`；`CLOSED`/`ARCHIVED` 返回 409。请求体可省略或传 {}。"
    ),
    responses={
        200: {"description": "发布成功（含幂等重放）"},
        403: {
            "model": ErrorResponse,
            "description": "学生调用为 ROLE_FORBIDDEN；非创建教师为 COURSE_FORBIDDEN",
        },
        404: _NOT_FOUND_UNIFIED,
        409: {
            "model": ErrorResponse,
            "description": (
                "课程已归档（COURSE_ARCHIVED）或状态不可发布（ASSIGNMENT_NOT_OPEN）"
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": "请求体为显式 null 或含未声明字段（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
    openapi_extra=EMPTY_OBJECT_REQUEST_BODY,
)
async def publish_assignment(
    assignment_id: uuid.UUID,
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
    locked: PublishableAssignmentDep,
) -> AssignmentDetailSchema:
    await validate_empty_object_body(request)
    assignment = await service.publish_assignment(session, assignment=locked)
    return await _detail(session, assignment=assignment)


# --------------------------------------------------------------------------- #
# 8.7 关闭任务
# --------------------------------------------------------------------------- #
@assignments_router.post(
    "/assignments/{assignment_id}/close",
    status_code=status.HTTP_200_OK,
    response_model=AssignmentDetailSchema,
    summary="关闭实验任务",
    description=(
        "仅课程创建教师可调用。`PUBLISHED` 关闭为 `CLOSED`；已关闭幂等返回，"
        "不改变 `closed_at`；`DRAFT`/`ARCHIVED` 返回 409。请求体可省略或传 {}。"
    ),
    responses={
        200: {"description": "关闭成功（含幂等重放）"},
        403: {
            "model": ErrorResponse,
            "description": "学生调用为 ROLE_FORBIDDEN；非创建教师为 COURSE_FORBIDDEN",
        },
        404: _NOT_FOUND_UNIFIED,
        409: {
            "model": ErrorResponse,
            "description": (
                "课程已归档（COURSE_ARCHIVED）或状态不可关闭（ASSIGNMENT_NOT_OPEN）"
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": "请求体为显式 null 或含未声明字段（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
    openapi_extra=EMPTY_OBJECT_REQUEST_BODY,
)
async def close_assignment(
    assignment_id: uuid.UUID,
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
    locked: ClosableAssignmentDep,
) -> AssignmentDetailSchema:
    await validate_empty_object_body(request)
    assignment = await service.close_assignment(session, assignment=locked)
    return await _detail(session, assignment=assignment)


__all__ = ["ASSIGNMENT_REQUEST_MODELS", "assignments_router"]
