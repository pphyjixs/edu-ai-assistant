"""Jobs 业务接口（供其他模块调用）。

第一版不实现 Worker：任务创建后如实保持 ``PENDING``，状态与进度不会自动推进。
``(type, resource_id)`` 唯一约束保证同一资源上同类任务只有一条，
因此"重复触发不会产生第二个任务"由数据库兜住，而不是靠先查后插。

本模块**不提交事务**：任务必须与业务记录在同一事务内落库
（例如课件上传：资料与解析任务要么都成功，要么都回滚）。
"""

from __future__ import annotations

import uuid
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
    - ``SUBMISSION_GRADE``：本次未实现该资源，统一按不可见处理。
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

    # 其他任务类型的资源不可见逻辑尚未接入，统一按不可见处理
    raise ResourceNotFoundError()


async def retry_practice_generate_job(
    session: AsyncSession, *, user: User, job_id: uuid.UUID, now: datetime | None = None
) -> Job:
    """重试练习生成任务（契约 10.1）。

    仅课程创建教师可调用；只接受 ``FAILED`` 或**租约已过期**的 ``RUNNING``。
    复用原练习 ID 与 job ID，清除运行令牌、租约、错误与旧题目，重置为
    ``GENERATING`` / ``PENDING`` 供 Worker 重新领取。

    :raises ResourceNotFoundError: 任务不存在、不可见或类型未实现（404）。
    :raises JobNotRetryableError: ``MATERIAL_PARSE`` 或状态不可重试（409）。
    """
    from app.core.errors import JobNotRetryableError, RoleForbiddenError
    from app.modules.auth.models import UserRole
    from app.modules.courses import service as courses_service
    from app.modules.practice import repository as practice_repo
    from app.modules.practice.models import PracticeStatus

    job = await repo.get_job_by_id(session, job_id)
    if job is None:
        raise ResourceNotFoundError()
    if job.type is JobType.SUBMISSION_GRADE:
        raise ResourceNotFoundError()
    if job.type is JobType.MATERIAL_PARSE:
        # 资料解析的重试一律经由 POST /materials/{material_id}/parse（契约 5.3）
        raise JobNotRetryableError()

    practice_set = await practice_repo.get_set_by_id(session, job.resource_id)
    if practice_set is None:
        raise ResourceNotFoundError()
    course = await courses_service.require_member_course(
        session, user=user, course_id=practice_set.course_id
    )
    if user.role is UserRole.STUDENT:
        raise RoleForbiddenError()
    await courses_service.require_course_teacher(
        session, user=user, course_id=course.id
    )
    courses_service.require_course_active(course)

    locked_job = await repo.get_job_for_resource_for_update(
        session, job_type=JobType.PRACTICE_GENERATE, resource_id=job.resource_id
    )
    locked_set = await practice_repo.get_set_for_update(session, job.resource_id)
    if locked_job is None or locked_set is None:  # pragma: no cover - 并发删除兜底
        raise ResourceNotFoundError()

    timestamp = now or utc_now()
    lease_expired = (
        locked_job.lease_expires_at is None
        or locked_job.lease_expires_at <= timestamp
    )
    retryable = locked_job.status is JobStatusValue.FAILED or (
        locked_job.status is JobStatusValue.RUNNING and lease_expired
    )
    if not retryable:
        raise JobNotRetryableError()

    await practice_repo.delete_questions(session, practice_set_id=locked_set.id)
    locked_job.status = JobStatusValue.PENDING
    locked_job.progress = 0
    locked_job.error = None
    locked_job.started_at = None
    locked_job.finished_at = None
    locked_job.run_token = None
    locked_job.lease_expires_at = None
    locked_set.status = PracticeStatus.GENERATING
    locked_set.updated_at = timestamp
    await session.commit()
    return locked_job
