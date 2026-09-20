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


async def get_material_parse_job(
    session: AsyncSession, *, material_id: uuid.UUID
) -> Job | None:
    """取某资料的解析任务。"""
    return await repo.get_job_for_resource(
        session, job_type=JobType.MATERIAL_PARSE, resource_id=material_id
    )


async def get_job_for_viewer(
    session: AsyncSession, *, user: User, job_id: uuid.UUID
) -> Job:
    """读取任务状态（契约 4.7 / 第 10 节），可见性由关联资源决定。

    任务不存在，或当前用户不是任务关联资源所属课程的成员时，统一抛
    ``ResourceNotFoundError``（404），不区分「不存在」与「不可见」。

    第一版只落地课件上传链路，任务类型为 ``MATERIAL_PARSE``，
    其 ``resource_id`` 即资料 ID，可见性等同于资料所属课程的成员。
    其余类型（练习生成、报告批改）的关联资源尚未实现，暂按不可见处理。
    """
    job = await repo.get_job_by_id(session, job_id)
    if job is None:
        raise ResourceNotFoundError()

    if job.type is JobType.MATERIAL_PARSE:
        from app.modules.materials import service as materials_service

        material = await materials_service.get_material_for_member(
            session, user=user, material_id=job.resource_id
        )
        if material is None:  # pragma: no cover - get_material_for_member 内部已抛
            raise ResourceNotFoundError()
        return job

    # 其他任务类型的资源不可见逻辑尚未接入，统一按不可见处理
    raise ResourceNotFoundError()
