"""Jobs 数据访问层。

只做查询与写入，不提交事务（事务边界属于调用方的 service）。
业务模块不得自行读写 ``jobs`` 表，统一通过本模块的 service 接口。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.jobs.models import Job, JobResourceType, JobStatusValue, JobType


def add_job(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    job_type: JobType,
    resource_type: JobResourceType,
    resource_id: uuid.UUID,
    status: JobStatusValue = JobStatusValue.PENDING,
    progress: int = 0,
    now: datetime,
) -> Job:
    """暂存任务；``(type, resource_id)`` 唯一约束冲突在提交时由数据库抛出。"""
    job = Job(
        id=job_id,
        type=job_type,
        status=status,
        progress=progress,
        resource_type=resource_type,
        resource_id=resource_id,
        created_at=now,
    )
    session.add(job)
    return job


async def get_job_by_id(session: AsyncSession, job_id: uuid.UUID) -> Job | None:
    return await session.get(Job, job_id)


async def get_job_for_resource(
    session: AsyncSession,
    *,
    job_type: JobType,
    resource_id: uuid.UUID,
) -> Job | None:
    """按「任务类型 + 资源 ID」取任务。

    唯一约束保证最多一条，因此这里可以放心用 ``scalar_one_or_none``。
    """
    result = await session.execute(
        select(Job).where(Job.type == job_type, Job.resource_id == resource_id)
    )
    return result.scalar_one_or_none()


async def get_job_for_resource_for_update(
    session: AsyncSession,
    *,
    job_type: JobType,
    resource_id: uuid.UUID,
) -> Job | None:
    """锁住关联任务行。

    删除资料（取消未完成解析）与重试解析都要在任务行锁内判定并写入，
    与 Worker 的领取/回写互斥。
    """
    result = await session.execute(
        select(Job)
        .where(Job.type == job_type, Job.resource_id == resource_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()
