"""Jobs 业务接口（供其他模块调用）。

第一版不实现 Worker：任务创建后如实保持 ``PENDING``，状态与进度不会自动推进。
``(type, resource_id)`` 唯一约束保证同一资源上同类任务只有一条，
因此"重复触发不会产生第二个任务"由数据库兜住，而不是靠先查后插。

本模块**不提交事务**：任务必须与业务记录在同一事务内落库
（例如课件上传：资料与解析任务要么都成功，要么都回滚）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ResourceNotFoundError
from app.core.time import utc_now
from app.modules.auth.models import User
from app.modules.jobs import repository as repo
from app.modules.jobs.models import Job, JobResourceType, JobStatusValue, JobType


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
    """读取任务状态（契约 4.7 / 第 10 节），可见性由关联资源决定。

    任务不存在，或当前用户不可见关联资源时，统一抛
    ``ResourceNotFoundError``（404），不区分「不存在」与「不可见」：

    - ``MATERIAL_PARSE``：按资料可见性（资料所属课程的成员）；
    - ``PRACTICE_GENERATE``：按练习可见性（教师可读任意状态，学生仅在该
      练习 ``PUBLISHED`` 时可见，见契约 7.4）；
    - ``SUBMISSION_GRADE``：按提交可见性（课程创建教师与提交本人可读，见契约 9.5）。
    """
    job = await repo.get_job_by_id(session, job_id)
    if job is None:
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

    if job.type is JobType.SUBMISSION_GRADE:
        from app.modules.grading import service as grading_service

        await grading_service.require_submission_viewer(
            session, user=user, submission_id=job.resource_id
        )
        return job

    # 其他任务类型的资源不可见逻辑尚未接入，统一按不可见处理
    raise ResourceNotFoundError()


async def retry_practice_generate_job(
    session: AsyncSession, *, user: User, job_id: uuid.UUID, now: datetime | None = None
) -> Job:
    """**已迁移**：练习生成任务的加锁与重置逻辑移到
    :mod:`app.modules.practice.service`（契约 10.1）。

    保留这个薄封装是为了让直接调用 jobs 服务的既有代码路径仍然可用：
    它等价于"加锁准备 + 写入"两步，并且与 HTTP 路由一样按
    ``课程 → 练习 → 任务`` 的顺序加锁。
    """
    from app.modules.practice import service as practice_service

    target = await practice_service.lock_retryable_job(
        session, user=user, job_id=job_id, now=now
    )
    return await practice_service.retry_practice_generate_job(
        session, target=target, now=now
    )


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
class RetryTarget:
    """重试接口的类型分派结果（契约 10.1）。

    关联资源的**可见性与角色检查先于类型分流**，因此这个对象只在全部检查
    通过后才产生；``practice`` / ``submission`` 分别对应两类可重试任务。
    """

    job_type: JobType
    practice: object | None = None
    submission: object | None = None


async def lock_retryable_job(
    session: AsyncSession,
    *,
    user: User,
    job_id: uuid.UUID,
    now: datetime | None = None,
) -> RetryTarget:
    """重试接口的统一前置：先做可见性/角色/归档检查，再按任务类型分流。

    - ``PRACTICE_GENERATE`` / ``MATERIAL_PARSE``：交给
      :func:`app.modules.practice.service.lock_retryable_job`（后者按 5.3 的
      约定让资料重试返回 ``409 JOB_NOT_RETRYABLE``）；
    - ``SUBMISSION_GRADE``：交给 Grading 模块，仅课程创建教师可重试
      （契约 9.6 / 10.1），状态分流与 ``POST /submissions/{id}/grade`` 共用。

    :raises ResourceNotFoundError: 任务或其关联资源不存在/不可见（404）。
    """
    job = await repo.get_job_by_id(session, job_id)
    if job is None:
        raise ResourceNotFoundError()

    if job.type is JobType.SUBMISSION_GRADE:
        from app.modules.grading import service as grading_service

        guard = await grading_service.lock_creator_submission(
            session, user=user, submission_id=job.resource_id
        )
        return RetryTarget(job_type=job.type, submission=guard.submission)

    from app.modules.practice import service as practice_service

    practice_target = await practice_service.lock_retryable_job(
        session, user=user, job_id=job_id, now=now
    )
    return RetryTarget(job_type=job.type, practice=practice_target)


async def retry_submission_grade_job(
    session: AsyncSession, *, target: RetryTarget, now: datetime | None = None
) -> Job:
    """重试提交批改：与 ``POST /submissions/{id}/grade`` 共用同一服务逻辑（契约 9.6）。"""
    from app.modules.grading import service as grading_service

    if target.submission is None:  # pragma: no cover - 分派保证存在
        raise ResourceNotFoundError()
    return await grading_service.request_grade(
        session, submission=target.submission, now=now  # type: ignore[arg-type]
    )
