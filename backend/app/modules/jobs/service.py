"""Jobs 业务接口（供其他模块调用）。

职责边界（契约 10.0 / 10.2）：本模块只承担

1. 任务创建（各业务模块在自身事务内调用，唯一约束兜住重复触发）；
2. 查询与重试的**公开范围校验**（类型/资源类型必须配对，``AGENT_RUN`` 一律 404）；
3. **权限优先级**与**类型分派**——把请求交给 Materials / Practice / Grading
   各自已有的资源权限服务与状态机，不复制任何领域状态推进逻辑。

状态推进仍由各 Worker 负责（解析 5.5、练习 7.10、批改 9.11）。

本模块**不提交事务**：任务必须与业务记录在同一事务内落库
（例如课件上传：资料与解析任务要么都成功，要么都回滚）；
重试的状态重置由资源模块在自己的事务里完成。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import JobNotRetryableError, ResourceNotFoundError
from app.modules.auth.models import User
from app.modules.jobs import repository as repo
from app.modules.jobs.models import Job, JobResourceType, JobStatusValue, JobType
from app.modules.jobs.schemas import is_public_job

if TYPE_CHECKING:  # pragma: no cover - 仅供类型标注，避免模块级循环依赖
    from app.modules.grading.models import Submission
    from app.modules.practice.models import PracticeSet


def create_material_parse_job(
    session: AsyncSession, *, material_id: uuid.UUID, now: datetime
) -> Job:
    """为资料创建 ``MATERIAL_PARSE`` 任务（``PENDING`` / 进度 0）。"""
    return repo.add_job(
        session,
        job_id=uuid.uuid4(),
        job_type=JobType.MATERIAL_PARSE,
        resource_type=JobResourceType.MATERIAL,
        resource_id=material_id,
        status=JobStatusValue.PENDING,
        progress=0,
        now=now,
    )


async def create_practice_generate_job(
    session: AsyncSession, *, practice_set_id: uuid.UUID, now: datetime
) -> Job:
    """创建练习生成任务（与练习记录在同一事务写入，契约 7.2）。"""
    return repo.add_job(
        session,
        job_id=uuid.uuid4(),
        job_type=JobType.PRACTICE_GENERATE,
        resource_type=JobResourceType.PRACTICE_SET,
        resource_id=practice_set_id,
        now=now,
    )


def create_submission_grade_job(
    session: AsyncSession, *, submission_id: uuid.UUID, now: datetime
) -> Job:
    """创建提交批改任务（与提交状态变更在同一事务写入，契约 9.6）。"""
    return repo.add_job(
        session,
        job_id=uuid.uuid4(),
        job_type=JobType.SUBMISSION_GRADE,
        resource_type=JobResourceType.SUBMISSION,
        resource_id=submission_id,
        now=now,
    )


async def get_submission_grade_job(
    session: AsyncSession, *, submission_id: uuid.UUID
) -> Job | None:
    """取某提交的批改任务（一条提交至多一个）。"""
    return await repo.get_job_for_resource(
        session, job_type=JobType.SUBMISSION_GRADE, resource_id=submission_id
    )


async def lock_submission_grade_job(
    session: AsyncSession, *, submission_id: uuid.UUID
) -> Job | None:
    """锁住某提交的批改任务行（触发/重试与 Worker 回写的临界区）。"""
    return await repo.get_job_for_resource_for_update(
        session, job_type=JobType.SUBMISSION_GRADE, resource_id=submission_id
    )


async def get_practice_generate_job(
    session: AsyncSession, *, practice_set_id: uuid.UUID
) -> Job | None:
    """取练习的生成任务（一条练习至多一个）。"""
    return await repo.get_job_for_resource(
        session, job_type=JobType.PRACTICE_GENERATE, resource_id=practice_set_id
    )


async def lock_practice_generate_job(
    session: AsyncSession, *, practice_set_id: uuid.UUID
) -> Job | None:
    """锁住练习生成任务行（回写与重试的临界区）。"""
    return await repo.get_job_for_resource_for_update(
        session, job_type=JobType.PRACTICE_GENERATE, resource_id=practice_set_id
    )


async def get_material_parse_job(
    session: AsyncSession, *, material_id: uuid.UUID
) -> Job | None:
    """取某资料的解析任务。"""
    return await repo.get_job_for_resource(
        session, job_type=JobType.MATERIAL_PARSE, resource_id=material_id
    )


async def lock_material_parse_job(
    session: AsyncSession, *, material_id: uuid.UUID
) -> Job | None:
    """锁住某资料的解析任务行（删除/重试与 Worker 互斥的临界区入口）。"""
    return await repo.get_job_for_resource_for_update(
        session, job_type=JobType.MATERIAL_PARSE, resource_id=material_id
    )


def cancel_material_parse_job(
    session: AsyncSession, job: Job, *, now: datetime
) -> None:
    """取消未完成的解析任务（删除资料的事务内调用）。

    仅 ``PENDING``/``RUNNING`` 会被取消；终态任务保持原样，
    回写端会因资料已删除而放弃（契约 5.5 第 5 步）。
    """
    if job.status in (JobStatusValue.PENDING, JobStatusValue.RUNNING):
        job.status = JobStatusValue.CANCELLED
        job.finished_at = now


async def get_job_for_viewer(
    session: AsyncSession, *, user: User, job_id: uuid.UUID
) -> Job:
    """读取任务状态（契约 4.7 / 10.1），可见性由关联资源决定。

    任务不存在、不属于公开 Jobs 范围（``AGENT_RUN`` 或类型与资源类型不配对）、
    或当前用户不可见关联资源时，统一抛 ``ResourceNotFoundError``（404），
    不区分「不存在」与「不可见」：

    - ``MATERIAL_PARSE``：按资料可见性（资料所属课程的成员，含归档课程；
      资料删除后不可读）；
    - ``PRACTICE_GENERATE``：按练习可见性（教师可读任意状态，学生仅在该
      练习 ``PUBLISHED`` 时可见，见契约 7.4）；
    - ``SUBMISSION_GRADE``：按提交可见性（课程创建教师与提交本人可读，见契约 9.5）。

    分派是**显式**的：公开范围之外的组合在这里就返回 404，
    不会先去查 PracticeSet 之类的资源再依赖偶然的 404。
    """
    job = await repo.get_job_by_id(session, job_id)
    if job is None or not is_public_job(job.type, job.resource_type):
        raise ResourceNotFoundError()

    if job.type is JobType.MATERIAL_PARSE:
        from app.modules.materials import service as materials_service

        await materials_service.get_material_for_member(
            session, user=user, material_id=job.resource_id
        )
        return job

    if job.type is JobType.PRACTICE_GENERATE:
        from app.modules.practice import service as practice_service

        await practice_service.get_practice_set(
            session, user=user, set_id=job.resource_id
        )
        return job

    # is_public_job 已排除其他组合，这里只剩 SUBMISSION_GRADE
    from app.modules.grading import service as grading_service

    await grading_service.require_submission_viewer(
        session, user=user, submission_id=job.resource_id
    )
    return job


# ------------------------- 上下文 Agent Run（Agent）------------------------- #


def create_agent_run_job(
    session: AsyncSession, *, run_id: uuid.UUID, now: datetime
) -> Job:
    """为 Agent Run 创建 ``AGENT_RUN`` 任务（``PENDING`` / 进度 0）。

    与 Run 记录在同一事务内落库（文档 6.4）；状态、进度、错误、尝试次数与租约
    只存在于这张任务表，``agent_runs`` 不复制一套竞争的状态字段（文档 6.5）。
    """
    return repo.add_job(
        session,
        job_id=uuid.uuid4(),
        job_type=JobType.AGENT_RUN,
        resource_type=JobResourceType.AGENT_RUN,
        resource_id=run_id,
        status=JobStatusValue.PENDING,
        progress=0,
        now=now,
    )


async def lock_agent_run_job(session: AsyncSession, *, run_id: uuid.UUID) -> Job | None:
    """锁住某个 Run 对应的任务行（回写与取消的临界区）。"""
    return await repo.get_job_for_resource_for_update(
        session, job_type=JobType.AGENT_RUN, resource_id=run_id
    )


@dataclass(frozen=True, slots=True)
class PracticeRetryTarget:
    """练习生成任务的重试目标：已按 **课程 → 练习 → 任务** 加锁并判定可重试。"""

    job: Job
    practice_set: PracticeSet


@dataclass(frozen=True, slots=True)
class SubmissionRetryTarget:
    """提交批改任务的重试目标：守卫已按 **课程 → Assignment → 版本 → Submission** 加锁。"""

    job: Job
    submission: Submission


#: 重试分派的两种结果（契约 10.2）；``MATERIAL_PARSE`` 在分派阶段就以
#: ``409 JOB_NOT_RETRYABLE`` 结束，不会产生目标对象。
RetryTarget = PracticeRetryTarget | SubmissionRetryTarget


async def lock_retryable_job(
    session: AsyncSession,
    *,
    user: User,
    job_id: uuid.UUID,
    now: datetime | None = None,
) -> RetryTarget:
    """重试接口的统一前置：公开范围 → 可见性 → 角色 → 归档，再按类型分流。

    检查顺序固定（契约 10.2）：任务存在且属于公开范围（404）→ 关联资源可见性
    （404）→ 角色与资源管理权限（403）→ 课程归档（409）→ 状态（409）。

    - ``MATERIAL_PARSE``：资料成员可见性 + 创建教师 + 未归档之后，
      返回 ``409 JOB_NOT_RETRYABLE``（改走 ``POST /materials/{id}/parse``，5.3）；
    - ``PRACTICE_GENERATE``：交给 :mod:`app.modules.practice.service`，
      在持有任务行锁时判定可重试；
    - ``SUBMISSION_GRADE``：交给 :mod:`app.modules.grading.service`，
      仅课程创建教师可重试（9.6 / 10.2），状态判定在任务行锁内完成。

    :raises ResourceNotFoundError: 任务不存在、不属于公开范围或关联资源不可见（404）。
    :raises RoleForbiddenError / CourseForbiddenError: 角色不符（403）。
    :raises CourseArchivedError: 课程已归档（409）。
    :raises JobNotRetryableError: 资料任务（改走资料接口）或状态不可重试（409）。
    """
    job = await repo.get_job_by_id(session, job_id)
    if job is None or not is_public_job(job.type, job.resource_type):
        # AGENT_RUN 与未知组合与"不存在"完全一致：不给探测空间（契约 10.0）
        raise ResourceNotFoundError()

    if job.type is JobType.MATERIAL_PARSE:
        from app.modules.materials import service as materials_service

        material = await materials_service.get_material_for_member(
            session, user=user, material_id=job.resource_id
        )
        await materials_service.require_material_manager(
            session, user=user, material=material
        )
        raise JobNotRetryableError()

    if job.type is JobType.SUBMISSION_GRADE:
        from app.modules.grading import service as grading_service

        guard = await grading_service.lock_creator_submission(
            session, user=user, submission_id=job.resource_id
        )
        return SubmissionRetryTarget(job=job, submission=guard.submission)

    # is_public_job 已排除其他组合，这里只剩 PRACTICE_GENERATE
    from app.modules.practice import service as practice_service

    practice_target = await practice_service.lock_retryable_job(
        session, user=user, job_id=job_id, now=now
    )
    return PracticeRetryTarget(
        job=practice_target.job, practice_set=practice_target.practice_set
    )


async def retry_submission_grade_job(
    session: AsyncSession,
    *,
    target: SubmissionRetryTarget,
    now: datetime | None = None,
) -> Job:
    """重试提交批改：与 ``POST /submissions/{id}/grade`` 共用同一服务逻辑（契约 9.6）。"""
    from app.modules.grading import service as grading_service

    return await grading_service.request_grade(
        session, submission=target.submission, now=now
    )
