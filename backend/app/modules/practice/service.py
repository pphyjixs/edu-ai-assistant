"""Practice 业务规则与事务边界（``docs/api-contract.md`` 第 7 节）。

关键约定：

- **检查顺序固定**：认证（401）→ 课程存在且为成员（404）→ 角色（403）→
  课程未归档（409）→ 业务状态（409）→ 请求校验（422）。同时违反多条规则时
  以该顺序为准。
- **可见性统一 404**：非成员、他人的资源、学生看不到的草稿/失败/生成中练习，
  一律 `404 RESOURCE_NOT_FOUND`，不区分"不存在"与"不可见"。
- **答案可见性**：课程教师（含创建者）可见标准答案、评分要点与解析；
  学生只在**本人提交后的答题结果**（7.7）里看到这些字段。
- **每生一次**：`(practice_set_id, student_id)` 唯一约束是最终防线，
  并发提交由 `IntegrityError` 兜底转成 `409 PRACTICE_ALREADY_ATTEMPTED`。
- **评分与答题记录同事务**：评分全部在内存中完成，写入失败不留半份记录。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    PracticeAlreadyAttemptedError,
    PracticeNotReadyError,
    ResourceNotFoundError,
    RoleForbiddenError,
    ValidationError,
)
from app.core.pagination import PaginationParams
from app.core.time import utc_now
from app.modules.auth.models import User, UserRole
from app.modules.courses import repository as courses_repo
from app.modules.courses import service as courses_service
from app.modules.courses.models import Course
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import JobStatusValue
from app.modules.materials.models import MaterialStatus
from app.modules.practice import repository as repo
from app.modules.practice import scoring
from app.modules.practice.models import (
    PracticeAttempt,
    PracticeAttemptAnswer,
    PracticeQuestion,
    PracticeQuestionType,
    PracticeSet,
    PracticeStatus,
)
from app.modules.practice.schemas import (
    PracticeAttemptSubmitRequest,
    PracticeGenerateRequest,
)

logger = logging.getLogger("app.practice.service")


async def _require_course_member(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
):
    """课程成员检查：课程不存在或非成员统一 404（契约 7.1）。"""
    return await courses_service.require_member_course(
        session, user=user, course_id=course_id
    )


async def _require_creator_teacher(
    session: AsyncSession, *, user: User, course
) -> None:
    """课程创建教师检查：学生 403 ROLE_FORBIDDEN，其他教师 403 COURSE_FORBIDDEN。"""
    if user.role is UserRole.STUDENT:
        raise RoleForbiddenError()
    await courses_service.require_course_teacher(
        session, user=user, course_id=course.id
    )


def _question_types_as_str(payload: PracticeGenerateRequest) -> list[str]:
    return [item.value for item in payload.question_types]


async def _lock_visible_course(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
) -> Course:
    """锁住课程行并做成员可见性检查（课程不存在或非成员统一 404）。"""
    course = await courses_repo.get_course_for_update(session, course_id)
    if course is None:
        raise ResourceNotFoundError()
    if not await courses_service.is_course_member(
        session, user=user, course_id=course.id
    ):
        raise ResourceNotFoundError()
    return course


async def lock_teacher_course(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
) -> Course:
    """**写接口第一阶段**：锁课程 → 成员（404）→ 创建教师（403）→ 未归档（409）。

    检查顺序与契约 7.1 一致；拿到的行锁保持到本次事务结束，因此后续的请求体
    校验与写入都发生在锁内。
    """
    course = await _lock_visible_course(session, user=user, course_id=course_id)
    await _require_creator_teacher(session, user=user, course=course)
    courses_service.require_course_active(course)
    return course


async def lock_set_for_publish(
    session: AsyncSession, *, user: User, set_id: uuid.UUID
) -> PracticeSet:
    """发布的前置阶段：读练习（404）→ 锁课程 → 创建教师 → 未归档 → 锁练习。

    锁顺序固定为 **课程 → 练习**；练习状态检查（`409 PRACTICE_NOT_READY`）
    也在这一阶段完成，因此请求体校验排在其后。
    """
    practice_set = await repo.get_set_by_id(session, set_id)
    if practice_set is None:
        raise ResourceNotFoundError()
    await lock_teacher_course(
        session, user=user, course_id=practice_set.course_id
    )

    locked = await repo.get_set_for_update(session, set_id)
    if locked is None:  # pragma: no cover - 并发删除的兜底
        raise ResourceNotFoundError()
    if locked.status not in (PracticeStatus.DRAFT, PracticeStatus.PUBLISHED):
        raise PracticeNotReadyError()
    return locked


async def lock_set_for_submit(
    session: AsyncSession, *, user: User, set_id: uuid.UUID
) -> PracticeSet:
    """提交的前置阶段：读练习（404）→ 锁课程 → 成员 → 学生（403）→ 未归档
    → 锁练习 → 已发布（409）。"""
    practice_set = await repo.get_set_by_id(session, set_id)
    if practice_set is None:
        raise ResourceNotFoundError()
    course = await _lock_visible_course(
        session, user=user, course_id=practice_set.course_id
    )
    if user.role is not UserRole.STUDENT:
        raise RoleForbiddenError()
    courses_service.require_course_active(course)

    locked = await repo.get_set_for_update(session, set_id)
    if locked is None:  # pragma: no cover - 并发删除的兜底
        raise ResourceNotFoundError()
    if locked.status is not PracticeStatus.PUBLISHED:
        raise PracticeNotReadyError()
    return locked


async def create_practice_set(
    session: AsyncSession,
    *,
    course: Course,
    user: User,
    payload: PracticeGenerateRequest,
    now: datetime | None = None,
) -> tuple[PracticeSet, object]:
    """生成的写入阶段（契约 7.2）：创建练习记录与任务。

    调用方必须先用 :func:`lock_teacher_course` 拿到课程行锁；资料可见性与就绪
    检查、练习与任务的写入都发生在该锁内。

    :raises ResourceNotFoundError: 所选资料不可见（404）。
    :raises MaterialNotReadyError: 所选资料未就绪或缺片段（409）。
    """
    from app.core.errors import MaterialNotReadyError

    # 资料可见性与就绪（契约 7.2）：不可见/已删除/不属于本课程统一 404
    rows = await repo.list_generation_materials(
        session, course_id=course.id, material_ids=payload.material_ids
    )
    if len(rows) != len(payload.material_ids):
        raise ResourceNotFoundError()

    not_ready = [
        row
        for row in rows
        if row.status is not MaterialStatus.READY or row.chunk_count == 0
    ]
    if not_ready:
        raise MaterialNotReadyError()

    created_at = now or utc_now()
    set_id = uuid.uuid4()
    practice_set = repo.create_set(
        session,
        set_id=set_id,
        course_id=course.id,
        teacher_id=user.id,
        title=f"练习 · {len(payload.material_ids)} 份资料",  # Worker 成功后会改成模型标题
        difficulty=payload.difficulty,
        requested_question_count=payload.question_count,
        question_types=_question_types_as_str(payload),
        now=created_at,
    )
    for index, material_id in enumerate(payload.material_ids, start=1):
        repo.add_set_material(
            session,
            practice_set_id=set_id,
            material_id=material_id,
            order=index,
        )
    job = await jobs_service.create_practice_generate_job(
        session, practice_set_id=set_id, now=created_at
    )
    await session.commit()
    return practice_set, job


async def list_published_sets(
    session: AsyncSession,
    *,
    user: User,
    course_id: uuid.UUID,
    pagination: PaginationParams,
) -> tuple[list[PracticeSet], int]:
    """课程已发布练习列表（契约 7.3）：课程成员可读，只列 ``PUBLISHED``。"""
    await _require_course_member(session, user=user, course_id=course_id)
    return await repo.list_published_sets(
        session,
        course_id=course_id,
        offset=pagination.offset,
        limit=pagination.limit,
    )


async def get_practice_set(
    session: AsyncSession, *, user: User, set_id: uuid.UUID
) -> tuple[PracticeSet, list[PracticeQuestion], bool]:
    """练习详情（契约 7.4）。

    返回 ``(练习, 题目, 答案是否可见)``：课程教师（含创建者）可见全部状态与
    答案；课程学生只能读已发布练习，且答案字段对其中空（由路由按可见性裁剪）。
    """
    practice_set = await repo.get_set_by_id(session, set_id)
    if practice_set is None:
        raise ResourceNotFoundError()
    await _require_course_member(
        session, user=user, course_id=practice_set.course_id
    )

    answers_visible = user.role is UserRole.TEACHER
    if not answers_visible and practice_set.status is not PracticeStatus.PUBLISHED:
        # 学生视角下草稿/生成中/失败/取消一律按不存在处理
        raise ResourceNotFoundError()

    questions = await repo.list_questions(session, practice_set_id=practice_set.id)
    return practice_set, questions, answers_visible


async def publish_practice_set(
    session: AsyncSession,
    *,
    practice_set: PracticeSet,
    now: datetime | None = None,
) -> PracticeSet:
    """发布的写入阶段（契约 7.5）：``DRAFT`` → ``PUBLISHED``，已发布幂等返回。

    调用方必须先用 :func:`lock_set_for_publish` 锁好课程与练习行；
    归档检查与状态检查都在那一阶段完成，因此这里的写入始终在锁内。
    """
    if practice_set.status is PracticeStatus.PUBLISHED:
        return practice_set  # 幂等：不写库、不改动 published_at

    published_at = now or utc_now()
    practice_set.status = PracticeStatus.PUBLISHED
    practice_set.published_at = published_at
    practice_set.updated_at = published_at
    await session.commit()
    return practice_set


def _invalid(message: str, field: str) -> ValidationError:
    return ValidationError(message, details={"errors": [{"field": field, "message": message}]})


def _validate_submitted_answers(
    questions: list[PracticeQuestion], payload: PracticeAttemptSubmitRequest
) -> dict[uuid.UUID, object]:
    """校验提交答案恰好覆盖全部题目且类型正确（契约 7.6）。

    :raises ValidationError: 缺失、重复、未知题目或答案类型不符（422）。
    """
    by_id = {question.id: question for question in questions}
    submitted_ids = {item.question_id for item in payload.answers}

    if submitted_ids - set(by_id):
        raise _invalid("提交包含未知题目", "answers")
    if set(by_id) - submitted_ids:
        raise _invalid("必须回答全部题目", "answers")

    validated: dict[uuid.UUID, object] = {}
    for item in payload.answers:
        question = by_id[item.question_id]
        value = item.answer

        if question.type is PracticeQuestionType.SINGLE_CHOICE:
            option_ids = {str(option.get("id")) for option in question.options}
            if not isinstance(value, str) or value not in option_ids:
                raise _invalid("单选答案必须是该题的选项 ID", "answers")
            validated[item.question_id] = value
            continue

        if question.type is PracticeQuestionType.TRUE_FALSE:
            if not isinstance(value, bool):
                raise _invalid("判断题答案必须是布尔值", "answers")
            validated[item.question_id] = value
            continue

        if not isinstance(value, str) or not value.strip():
            raise _invalid("简答题答案不能为空", "answers")
        validated[item.question_id] = value.strip()

    return validated


async def submit_attempt(
    session: AsyncSession,
    *,
    practice_set: PracticeSet,
    user: User,
    payload: PracticeAttemptSubmitRequest,
    now: datetime | None = None,
) -> tuple[PracticeAttempt, list[PracticeQuestion], list[PracticeAttemptAnswer]]:
    """提交答案并同步评分（契约 7.6）的写入阶段。

    调用方必须先用 :func:`lock_set_for_submit` 锁好课程与练习行：成员、角色、
    归档与练习状态检查都在那一阶段完成（先于请求体校验），评分与写入在本阶段
    的同一事务内完成。

    :raises ValidationError: 答案覆盖或类型不符（422）。
    :raises PracticeAlreadyAttemptedError: 重复提交（409）。
    """
    # 只保留标量：回滚会 expire ORM 对象，之后不能再访问 user.id
    student_id = user.id
    set_id = practice_set.id
    questions = await repo.list_questions(session, practice_set_id=set_id)
    if not questions:  # pragma: no cover - 已发布练习必然有题目
        raise PracticeNotReadyError()
    submitted = _validate_submitted_answers(questions, payload)

    existing = await repo.get_attempt_for_student(
        session, practice_set_id=set_id, student_id=student_id
    )
    if existing is not None:
        raise PracticeAlreadyAttemptedError()

    gradable = [
        scoring.GradableQuestion(
            question_id=question.id,
            order=question.order,
            type=question.type,
            correct_answer=question.correct_answer,
            grading_points=list(question.grading_points or []),
        )
        for question in questions
    ]
    total_score, graded = scoring.grade_attempt(gradable, submitted)

    submitted_at = now or utc_now()
    attempt = repo.add_attempt(
        session,
        attempt_id=uuid.uuid4(),
        practice_set_id=set_id,
        student_id=student_id,
        total_score=total_score,
        now=submitted_at,
    )
    answer_rows: list[PracticeAttemptAnswer] = []
    try:
        # flush 也必须包在 try 内：唯一约束冲突可能在 flush 阶段就抛出来
        await session.flush()
        for item in graded:
            answer_rows.append(
                repo.add_attempt_answer(
                    session,
                    answer_id=uuid.uuid4(),
                    attempt_id=attempt.id,
                    question_id=item.question_id,
                    question_order=item.order,
                    submitted_answer=item.submitted,
                    is_correct=item.is_correct,
                    score=item.score,
                    matched_points=item.matched_points,
                    now=submitted_at,
                )
            )
        await session.commit()
    except IntegrityError as exc:
        # 并发提交：唯一约束 (practice_set_id, student_id) 兜底。
        # 回滚会 expire ORM 对象，日志只用已保存的标量 ID。
        await session.rollback()
        logger.info("并发重复提交被唯一约束拦截（set=%s student=%s）", set_id, student_id)
        raise PracticeAlreadyAttemptedError() from exc

    # 提交后强制从库中重读：等权的每题分值是无限小数（100/3），库中按两位小数
    # 存储，响应必须与持久化数据一致（契约 7.7）。expire_all 后重新查询即可
    # 让 identity map 中的对象从数据库刷新，避免读到内存里的未舍入值。
    attempt_id_value = attempt.id
    session.expire_all()
    stored_attempt = await repo.get_attempt_by_id(session, attempt_id_value)
    stored_answers = await repo.list_attempt_answers(
        session, attempt_id=attempt_id_value
    )
    stored_questions = await repo.list_questions(session, practice_set_id=set_id)
    return (
        stored_attempt or attempt,
        stored_questions or questions,
        stored_answers or answer_rows,
    )


@dataclass(slots=True)
class RetryTarget:
    """重试目标：已按统一顺序加锁的任务与练习。"""

    job: object
    practice_set: PracticeSet


def _is_retryable(job, *, now: datetime) -> bool:
    """``FAILED``，或**租约已过期**的 ``RUNNING``（崩溃遗留）才可重试。"""
    if job.status is JobStatusValue.FAILED:
        return True
    if job.status is not JobStatusValue.RUNNING:
        return False
    return job.lease_expires_at is None or job.lease_expires_at <= now


async def lock_retryable_job(
    session: AsyncSession, *, user: User, job_id: uuid.UUID, now: datetime | None = None
) -> RetryTarget:
    """重试的前置阶段（契约 10.1）。

    检查顺序固定为 **任务存在 → 关联资源可见性 → 角色 → 课程归档 → 类型/状态**，
    与契约 7.1 / 10.1 一致：类型分流（如"资料重试改走资料接口"）排在权限与归档
    之后，因此**非成员不能借重试接口探测任务是否存在或其类型**。

    - ``PRACTICE_GENERATE``：锁顺序固定为 **课程 → 练习 → 任务**，可重试检查
      在持有任务行锁时完成；
    - ``MATERIAL_PARSE``：先按关联资料做成员可见性（404）→ 创建教师（403）→
      未归档（409），最后才返回 ``409 JOB_NOT_RETRYABLE``（资料重试经由
      ``POST /materials/{material_id}/parse``，契约 5.3）；
    - ``SUBMISSION_GRADE``：由 :func:`app.modules.jobs.service.lock_retryable_job`
      在类型分流阶段交给 Grading 模块处理（契约 9.6 与 10.1 共用同一逻辑），
      不会走到本函数。

    :raises ResourceNotFoundError: 任务/关联资源不存在或不可见（404）。
    :raises RoleForbiddenError / CourseForbiddenError: 非创建教师（403）。
    :raises CourseArchivedError: 课程已归档（409）。
    :raises JobNotRetryableError: 状态不可重试（409）。
    """
    from app.core.errors import JobNotRetryableError
    from app.modules.jobs import repository as jobs_repo
    from app.modules.jobs.models import JobType

    job = await jobs_repo.get_job_by_id(session, job_id)
    if job is None:
        raise ResourceNotFoundError()

    if job.type is JobType.SUBMISSION_GRADE:
        # 理论上不可达：jobs 分派层（app.modules.jobs.service.lock_retryable_job）
        # 已把该类型交给 Grading 模块。保留兜底，避免直接调用本函数时误走练习路径。
        raise ResourceNotFoundError()

    if job.type is JobType.MATERIAL_PARSE:
        # 关联资源是资料：先做可见性 → 角色 → 归档，再按类型分流（避免暴露任务存在性）
        await _require_material_member_for_retry(session, user=user, material_id=job.resource_id)
        raise JobNotRetryableError()

    practice_set = await repo.get_set_by_id(session, job.resource_id)
    if practice_set is None:
        raise ResourceNotFoundError()
    practice_set_id = practice_set.id
    await lock_teacher_course(session, user=user, course_id=practice_set.course_id)

    locked_set = await repo.get_set_for_update(session, practice_set_id)
    locked_job = await jobs_service.lock_practice_generate_job(
        session, practice_set_id=practice_set_id
    )
    if locked_set is None or locked_job is None:  # pragma: no cover - 并发删除兜底
        raise ResourceNotFoundError()
    if not _is_retryable(locked_job, now=now or utc_now()):
        raise JobNotRetryableError()
    return RetryTarget(job=locked_job, practice_set=locked_set)


async def _require_material_member_for_retry(
    session: AsyncSession, *, user: User, material_id: uuid.UUID
) -> None:
    """资料任务的可见性 → 角色 → 归档检查（供重试接口复用，契约 10.1）。

    顺序与 :func:`lock_teacher_course` 一致：资料（含已删除）不可见或非成员
    统一 404；学生 403 ``ROLE_FORBIDDEN``、非创建教师 403 ``COURSE_FORBIDDEN``；
    课程已归档 409 ``COURSE_ARCHIVED``。
    """
    from app.modules.materials import service as materials_service

    material = await materials_service.get_material_for_member(
        session, user=user, material_id=material_id
    )
    await lock_teacher_course(session, user=user, course_id=material.course_id)


async def retry_practice_generate_job(
    session: AsyncSession, *, target: RetryTarget, now: datetime | None = None
) -> object:
    """重试的写入阶段（契约 10.1）：清空旧题目并重置任务，复用原 ID。

    调用方必须先用 :func:`lock_retryable_job` 加锁并完成全部检查。
    """
    timestamp = now or utc_now()
    practice_set = target.practice_set
    job = target.job
    await repo.delete_questions(session, practice_set_id=practice_set.id)
    job.status = JobStatusValue.PENDING
    job.progress = 0
    job.error = None
    job.started_at = None
    job.finished_at = None
    job.run_token = None
    job.lease_expires_at = None
    practice_set.status = PracticeStatus.GENERATING
    practice_set.updated_at = timestamp
    await session.commit()
    return job


async def get_attempt_result(
    session: AsyncSession, *, user: User, attempt_id: uuid.UUID
) -> tuple[PracticeAttempt, PracticeSet, list[PracticeQuestion], list[PracticeAttemptAnswer]]:
    """答题结果（契约 7.7）：仅本人或课程创建教师可读，其他统一 404。"""
    attempt = await repo.get_attempt_by_id(session, attempt_id)
    if attempt is None:
        raise ResourceNotFoundError()
    practice_set = await repo.get_set_by_id(session, attempt.practice_set_id)
    if practice_set is None:  # pragma: no cover - 级联删除后不可见
        raise ResourceNotFoundError()

    is_owner = attempt.student_id == user.id
    is_creator = practice_set.teacher_id == user.id
    if not (is_owner or is_creator):
        raise ResourceNotFoundError()
    if not is_owner:
        # 创建教师同样需要是课程成员；非成员（例如被移出课程）按不可见处理
        await _require_course_member(
            session, user=user, course_id=practice_set.course_id
        )

    questions = await repo.list_questions(
        session, practice_set_id=practice_set.id
    )
    answers = await repo.list_attempt_answers(session, attempt_id=attempt.id)
    return attempt, practice_set, questions, answers


__all__ = [
    "RetryTarget",
    "create_practice_set",
    "get_attempt_result",
    "get_practice_set",
    "list_published_sets",
    "lock_retryable_job",
    "lock_set_for_publish",
    "lock_set_for_submit",
    "lock_teacher_course",
    "publish_practice_set",
    "retry_practice_generate_job",
    "submit_attempt",
]
