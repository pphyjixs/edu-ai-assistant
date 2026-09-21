"""Practice HTTP 路由（``docs/api-contract.md`` 第 7 节的六个接口）。

只做协议转换与依赖注入，业务规则全部在 :mod:`app.modules.practice.service`。

**答案可见性**在响应组装时按视角裁剪：课程教师可见标准答案、评分要点与解析；
学生视角下这三个字段为 ``null`` / 空数组，学生只能通过答题结果（7.7）看到答案。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, status

from app.core.pagination import Page, PaginationDep
from app.core.request_body import (
    EMPTY_OBJECT_REQUEST_BODY,
    validate_empty_object_body,
)
from app.core.schemas import ErrorResponse
from app.db.session import SessionDep
from app.modules.auth.permissions import CurrentUserDep
from app.modules.jobs.schemas import JobStatus
from app.modules.practice import repository as repo
from app.modules.practice import service
from app.modules.practice.models import PracticeAttempt, PracticeQuestion, PracticeSet
from app.modules.practice.schemas import (
    PracticeAttemptAnswerSchema,
    PracticeAttemptResultSchema,
    PracticeAttemptSubmitRequest,
    PracticeGenerateRequest,
    PracticeGradingPointSchema,
    PracticeOptionSchema,
    PracticeQuestionSchema,
    PracticeSetSchema,
    PracticeSetSummarySchema,
)

practice_router = APIRouter(tags=["practice"])

_AUTH_ERRORS: dict[int | str, dict] = {
    401: {
        "model": ErrorResponse,
        "description": "Access Token 缺失、无效或已过期（AUTH_TOKEN_EXPIRED）",
    }
}

_NOT_FOUND_UNIFIED = {
    "model": ErrorResponse,
    "description": (
        "资源不存在，或当前用户不可见（RESOURCE_NOT_FOUND；"
        "学生视角下草稿/生成中/失败/取消的练习一律按不存在处理）"
    ),
}

_FORBIDDEN = {
    "model": ErrorResponse,
    "description": "学生调用教师接口为 ROLE_FORBIDDEN；非创建教师为 COURSE_FORBIDDEN",
}

_ARCHIVED = {
    "model": ErrorResponse,
    "description": "课程已归档，不能生成、发布、重试或提交（COURSE_ARCHIVED）",
}

#: 无请求字段的接口（发布练习）使用 core 的可选对象请求体声明与手工校验


# --------------------------------------------------------------------------- #
# 响应组装
# --------------------------------------------------------------------------- #
def _question_schema(
    question: PracticeQuestion, *, answers_visible: bool
) -> PracticeQuestionSchema:
    """题目响应；学生视角下答案、评分要点与解析不可见。"""
    return PracticeQuestionSchema(
        id=question.id,
        order=question.order,
        type=question.type,
        prompt=question.prompt,
        options=[
            PracticeOptionSchema(id=str(option.get("id")), text=str(option.get("text")))
            for option in (question.options or [])
        ],
        knowledge_point=question.knowledge_point,
        correct_answer=question.correct_answer if answers_visible else None,
        grading_points=(
            [
                PracticeGradingPointSchema(
                    point=str(item.get("point")), accepted=list(item.get("accepted", []))
                )
                for item in (question.grading_points or [])
            ]
            if answers_visible
            else []
        ),
        explanation=question.explanation if answers_visible else None,
    )


def _summary_schema(practice_set: PracticeSet) -> PracticeSetSummarySchema:
    return PracticeSetSummarySchema(
        id=practice_set.id,
        course_id=practice_set.course_id,
        title=practice_set.title,
        status=practice_set.status,
        difficulty=practice_set.difficulty,
        question_count=practice_set.question_count,
        question_types=list(practice_set.question_types),
        created_at=practice_set.created_at,
        updated_at=practice_set.updated_at,
        published_at=practice_set.published_at,
    )


def _detail_schema(
    practice_set: PracticeSet,
    questions: list[PracticeQuestion],
    *,
    answers_visible: bool,
) -> PracticeSetSchema:
    summary = _summary_schema(practice_set)
    return PracticeSetSchema(
        **summary.model_dump(),
        questions=[
            _question_schema(question, answers_visible=answers_visible)
            for question in questions
        ],
    )


def _attempt_result_schema(
    attempt: PracticeAttempt,
    questions: list[PracticeQuestion],
    answers: list,
) -> PracticeAttemptResultSchema:
    by_id = {question.id: question for question in questions}
    items: list[PracticeAttemptAnswerSchema] = []
    for answer in answers:
        question = by_id.get(answer.question_id)
        if question is None:  # pragma: no cover - 题目随练习存在
            continue
        items.append(
            PracticeAttemptAnswerSchema(
                question_id=answer.question_id,
                question_order=answer.question_order,
                type=question.type,
                prompt=question.prompt,
                submitted_answer=answer.submitted_answer,
                is_correct=answer.is_correct,
                score=float(answer.score),
                correct_answer=question.correct_answer,
                explanation=question.explanation,
                knowledge_point=question.knowledge_point,
            )
        )
    return PracticeAttemptResultSchema(
        id=attempt.id,
        practice_set_id=attempt.practice_set_id,
        student_id=attempt.student_id,
        total_score=float(attempt.total_score),
        submitted_at=attempt.submitted_at,
        answers=items,
    )


# --------------------------------------------------------------------------- #
# 7.2 生成练习
# --------------------------------------------------------------------------- #
@practice_router.post(
    "/courses/{course_id}/practice-sets/generate",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobStatus,
    summary="生成练习",
    description=(
        "仅课程创建教师可调用。创建练习记录（GENERATING）与 PRACTICE_GENERATE "
        "任务（同一事务），题目由独立 Worker 生成。所选资料必须属于本课程、"
        "未删除、READY 且有检索片段。"
    ),
    responses={
        202: {"description": "已受理，返回练习生成任务"},
        403: _FORBIDDEN,
        404: {
            "model": ErrorResponse,
            "description": "非成员，或所选资料不存在/已删除/不属于本课程（RESOURCE_NOT_FOUND）",
        },
        409: {
            "model": ErrorResponse,
            "description": "课程已归档（COURSE_ARCHIVED）或资料未就绪（MATERIAL_NOT_READY）",
        },
        422: {
            "model": ErrorResponse,
            "description": "请求结构、数量边界或题型取值不合法（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
)
async def generate_practice_set(
    course_id: uuid.UUID,
    payload: PracticeGenerateRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> JobStatus:
    _practice_set, job = await service.create_practice_set(
        session, user=user, course_id=course_id, payload=payload
    )
    return JobStatus.model_validate(job)


# --------------------------------------------------------------------------- #
# 7.3 已发布练习列表
# --------------------------------------------------------------------------- #
@practice_router.get(
    "/courses/{course_id}/practice-sets",
    status_code=status.HTTP_200_OK,
    response_model=Page[PracticeSetSummarySchema],
    summary="课程已发布练习",
    description=(
        "课程成员（教师或学生）可读，只列出已发布练习，按发布时间的倒序分页；"
        "归档课程仍可读。课程不存在或非成员统一 404。"
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
async def list_practice_sets(
    course_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
    pagination: PaginationDep,
) -> Page[PracticeSetSummarySchema]:
    sets, total = await service.list_published_sets(
        session, user=user, course_id=course_id, pagination=pagination
    )
    return Page[PracticeSetSummarySchema](
        items=[_summary_schema(item) for item in sets],
        page=pagination.page,
        page_size=pagination.page_size,
        total=total,
    )


# --------------------------------------------------------------------------- #
# 7.4 练习详情
# --------------------------------------------------------------------------- #
@practice_router.get(
    "/practice-sets/{set_id}",
    status_code=status.HTTP_200_OK,
    response_model=PracticeSetSchema,
    summary="练习详情",
    description=(
        "课程教师可读全部状态；课程学生只能读已发布练习，其余状态统一 404。"
        "学生响应不含标准答案、评分要点与解析；教师响应包含。"
    ),
    responses={
        404: _NOT_FOUND_UNIFIED,
        **_AUTH_ERRORS,
    },
)
async def get_practice_set(
    set_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> PracticeSetSchema:
    practice_set, questions, answers_visible = await service.get_practice_set(
        session, user=user, set_id=set_id
    )
    return _detail_schema(
        practice_set, questions, answers_visible=answers_visible
    )


# --------------------------------------------------------------------------- #
# 7.5 发布练习
# --------------------------------------------------------------------------- #
@practice_router.post(
    "/practice-sets/{set_id}/publish",
    status_code=status.HTTP_200_OK,
    response_model=PracticeSetSchema,
    summary="发布练习",
    description=(
        "仅课程创建教师可调用。`DRAFT` 发布为 `PUBLISHED`；已发布幂等返回 200；"
        "生成中、失败或取消返回 409 PRACTICE_NOT_READY。请求体可省略或传 {}。"
    ),
    responses={
        200: {"description": "发布成功（含幂等重放）"},
        403: _FORBIDDEN,
        404: _NOT_FOUND_UNIFIED,
        409: {
            "model": ErrorResponse,
            "description": "课程已归档（COURSE_ARCHIVED）或练习未就绪（PRACTICE_NOT_READY）",
        },
        422: {
            "model": ErrorResponse,
            "description": "请求体为显式 null 或含未声明字段（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
    openapi_extra=EMPTY_OBJECT_REQUEST_BODY,
)
async def publish_practice_set(
    set_id: uuid.UUID,
    request: Request,
    user: CurrentUserDep,
    session: SessionDep,
) -> PracticeSetSchema:
    await validate_empty_object_body(request)
    practice_set = await service.publish_practice_set(
        session, user=user, set_id=set_id
    )
    questions = await repo.list_questions(session, practice_set_id=set_id)
    return _detail_schema(practice_set, questions, answers_visible=True)


# --------------------------------------------------------------------------- #
# 7.6 提交答案
# --------------------------------------------------------------------------- #
@practice_router.post(
    "/practice-sets/{set_id}/attempts",
    status_code=status.HTTP_201_CREATED,
    response_model=PracticeAttemptResultSchema,
    summary="提交答案",
    description=(
        "仅课程学生可提交已发布练习，且每套练习只能提交一次；答案必须恰好覆盖"
        "全部题目且类型与题型相符。评分与答题记录在同一事务写入。"
    ),
    responses={
        201: {"description": "提交成功，返回评分结果"},
        403: {
            "model": ErrorResponse,
            "description": "教师提交返回 ROLE_FORBIDDEN；非创建教师读取他人资源为 COURSE_FORBIDDEN",
        },
        404: _NOT_FOUND_UNIFIED,
        409: {
            "model": ErrorResponse,
            "description": (
                "课程已归档（COURSE_ARCHIVED）、练习未发布（PRACTICE_NOT_READY）"
                "或重复提交（PRACTICE_ALREADY_ATTEMPTED）"
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": "答案覆盖、类型或取值不合法（VALIDATION_ERROR）",
        },
        **_AUTH_ERRORS,
    },
)
async def submit_practice_attempt(
    set_id: uuid.UUID,
    payload: PracticeAttemptSubmitRequest,
    user: CurrentUserDep,
    session: SessionDep,
) -> PracticeAttemptResultSchema:
    attempt, questions, answers = await service.submit_attempt(
        session, user=user, set_id=set_id, payload=payload
    )
    return _attempt_result_schema(attempt, questions, answers)


# --------------------------------------------------------------------------- #
# 7.7 答题结果
# --------------------------------------------------------------------------- #
@practice_router.get(
    "/practice-attempts/{attempt_id}",
    status_code=status.HTTP_200_OK,
    response_model=PracticeAttemptResultSchema,
    summary="答题结果",
    description=(
        "仅本人或课程创建教师可读，其他用户与不存在统一 404。"
        "结果包含总分、每题提交答案、是否完全正确、得分、标准答案与解析。"
    ),
    responses={
        404: _NOT_FOUND_UNIFIED,
        **_AUTH_ERRORS,
    },
)
async def get_practice_attempt(
    attempt_id: uuid.UUID,
    user: CurrentUserDep,
    session: SessionDep,
) -> PracticeAttemptResultSchema:
    attempt, _practice_set, questions, answers = await service.get_attempt_result(
        session, user=user, attempt_id=attempt_id
    )
    return _attempt_result_schema(attempt, questions, answers)


__all__ = ["practice_router"]
