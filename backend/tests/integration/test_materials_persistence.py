"""课件上传持久化验收（真实 PostgreSQL + Alembic 迁移建表）。

对应计划第 2 步的验收：

- 迁移可升级、可回退，且结构与 ORM 一致 —— 由 ``test_migrations.py`` 覆盖；
- **重启应用后上传会话及任务仍可读取** —— 本文件的重点。

“重启”用 ``dispose_engines()`` + 重新 ``get_session_factory()`` 模拟：
进程内的引擎与会话工厂缓存被清空，随后的查询走**全新引擎与连接**，
等价于应用重启后重新建立连接，能验证数据确实落在库里而不是进程内存里。
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.db.session import dispose_engines, get_session_factory
from app.modules.auth.models import User, UserRole
from app.modules.courses.models import Course, CourseStatus
from app.modules.jobs.models import Job, JobResourceType, JobStatusValue, JobType
from app.modules.materials.models import (
    Material,
    MaterialStatus,
    MaterialUploadSession,
)

#: 本文件只验证持久化，不校验密码，因此不放真实 Argon2id 哈希（也避免拖慢用例）
_PASSWORD_HASH_PLACEHOLDER = "not-a-real-argon2id-hash"


async def _create_teacher(session: AsyncSession, suffix: str) -> uuid.UUID:
    user = User(
        email=f"teacher-{suffix}@example.com",
        email_normalized=f"teacher-{suffix}@example.com",
        password_hash=_PASSWORD_HASH_PLACEHOLDER,
        display_name="张老师",
        role=UserRole.TEACHER,
    )
    session.add(user)
    await session.flush()
    return user.id


async def _create_course(
    session: AsyncSession, teacher_id: uuid.UUID, invite_code: str
) -> uuid.UUID:
    course = Course(
        name="软件工程实验",
        teacher_id=teacher_id,
        status=CourseStatus.ACTIVE,
        invite_code=invite_code,
    )
    session.add(course)
    await session.flush()
    return course.id


async def _create_upload(
    session: AsyncSession, course_id: uuid.UUID, teacher_id: uuid.UUID
) -> MaterialUploadSession:
    """创建一次上传会话（10 分钟 PUT 有效期 + 24 小时确认窗口）。"""
    now = utc_now()
    upload = MaterialUploadSession(
        course_id=course_id,
        teacher_id=teacher_id,
        object_key=f"courses/{course_id}/materials/{uuid.uuid4().hex}.pdf",
        filename="chapter-1.pdf",
        content_type="application/pdf",
        size=1048576,
        sha256="ab" * 32,
        upload_url_expires_at=now + timedelta(minutes=10),
        confirm_deadline_at=now + timedelta(hours=24),
    )
    session.add(upload)
    await session.flush()
    return upload


async def test_upload_session_material_and_job_survive_restart(
    db_isolation: None,
    pg_session_factory,
    pg_test_url: str,
    make_settings,
) -> None:
    """写入上传会话、资料与解析任务后，“重启”应用仍能原样读回。"""
    now = utc_now()

    async with pg_session_factory() as session:
        teacher_id = await _create_teacher(session, "restart")
        course_id = await _create_course(session, teacher_id, "RS0000000001")
        upload = await _create_upload(session, course_id, teacher_id)

        material = Material(
            course_id=course_id,
            upload_id=upload.id,
            filename=upload.filename,
            content_type=upload.content_type,
            size=upload.size,
            sha256=upload.sha256,
            storage_key=upload.object_key,
            status=MaterialStatus.PROCESSING,
            uploaded_by=teacher_id,
        )
        session.add(material)
        await session.flush()

        job = Job(
            type=JobType.MATERIAL_PARSE,
            resource_type=JobResourceType.MATERIAL,
            resource_id=material.id,
        )
        session.add(job)

        upload.completed_material_id = material.id
        upload.completed_at = utc_now()
        await session.commit()

        expected = {
            "upload_id": upload.id,
            "material_id": material.id,
            "job_id": job.id,
            "course_id": course_id,
            "object_key": upload.object_key,
            "confirm_deadline_at": upload.confirm_deadline_at,
            "upload_url_expires_at": upload.upload_url_expires_at,
        }

    # 模拟重启：清空进程内的引擎与会话工厂缓存，用全新引擎重新连接
    await dispose_engines()
    restarted = get_session_factory(make_settings(database_url=pg_test_url))

    async with restarted() as session:
        upload = await session.get(MaterialUploadSession, expected["upload_id"])
        material = await session.get(Material, expected["material_id"])
        job = await session.get(Job, expected["job_id"])

    assert upload is not None and material is not None and job is not None

    # 上传会话：课程、发起教师、对象键、预期大小、类型、哈希、两个期限与完成结果
    assert upload.course_id == expected["course_id"] == material.course_id
    assert upload.teacher_id == teacher_id
    assert upload.object_key == expected["object_key"]
    assert upload.filename == "chapter-1.pdf"
    assert upload.content_type == "application/pdf"
    assert upload.size == 1048576
    assert upload.sha256 == "ab" * 32
    assert upload.upload_url_expires_at == expected["upload_url_expires_at"]
    assert upload.confirm_deadline_at == expected["confirm_deadline_at"]
    assert upload.completed_material_id == material.id
    assert upload.completed_at is not None
    assert upload.completed_at >= now

    # 资料：完成确认后为 PROCESSING（第一版没有 Worker，不会自动推进）
    assert material.status is MaterialStatus.PROCESSING
    assert material.storage_key == expected["object_key"]
    assert material.uploaded_by == teacher_id
    assert material.error_message is None
    assert material.created_at.tzinfo is not None

    # 任务：如实保持 PENDING，进度为 0，未开始也未结束
    assert job.type is JobType.MATERIAL_PARSE
    assert job.status is JobStatusValue.PENDING
    assert job.progress == 0
    assert job.resource_type is JobResourceType.MATERIAL
    assert job.resource_id == material.id
    assert job.error is None
    assert job.started_at is None
    assert job.finished_at is None


async def test_duplicate_material_for_same_upload_is_rejected(
    db_isolation: None, pg_session_factory
) -> None:
    """一次上传只产生一条资料：并发重复完成由 uq_materials_upload_id 兜住。"""
    async with pg_session_factory() as session:
        teacher_id = await _create_teacher(session, "dup-material")
        course_id = await _create_course(session, teacher_id, "DM0000000001")
        upload = await _create_upload(session, course_id, teacher_id)

        session.add(
            Material(
                course_id=course_id,
                upload_id=upload.id,
                filename="chapter-1.pdf",
                content_type="application/pdf",
                size=upload.size,
                sha256=upload.sha256,
                storage_key=upload.object_key,
                status=MaterialStatus.PROCESSING,
                uploaded_by=teacher_id,
            )
        )
        await session.flush()

        session.add(
            Material(
                course_id=course_id,
                upload_id=upload.id,
                filename="chapter-1.pdf",
                content_type="application/pdf",
                size=upload.size,
                sha256=upload.sha256,
                storage_key=f"{upload.object_key}.dup",
                status=MaterialStatus.PROCESSING,
                uploaded_by=teacher_id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()

        await session.rollback()


async def test_duplicate_parse_job_for_same_material_is_rejected(
    db_isolation: None, pg_session_factory
) -> None:
    """一条资料只有一个 MATERIAL_PARSE 任务：重复完成不会创建第二个任务。"""
    async with pg_session_factory() as session:
        teacher_id = await _create_teacher(session, "dup-job")
        course_id = await _create_course(session, teacher_id, "DJ0000000001")
        upload = await _create_upload(session, course_id, teacher_id)
        material = Material(
            course_id=course_id,
            upload_id=upload.id,
            filename="chapter-1.pdf",
            content_type="application/pdf",
            size=upload.size,
            sha256=upload.sha256,
            storage_key=upload.object_key,
            status=MaterialStatus.PROCESSING,
            uploaded_by=teacher_id,
        )
        session.add(material)
        await session.flush()

        session.add(
            Job(
                type=JobType.MATERIAL_PARSE,
                resource_type=JobResourceType.MATERIAL,
                resource_id=material.id,
            )
        )
        await session.flush()

        session.add(
            Job(
                type=JobType.MATERIAL_PARSE,
                resource_type=JobResourceType.MATERIAL,
                resource_id=material.id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()

        await session.rollback()
