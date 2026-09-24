"""Dashboard 集成测试（``docs/api-contract.md`` 第 11 节）。

跑在真实 PostgreSQL 上：教师/学生作用域隔离、归档课程/其他用户/已删除资料不泄漏、
待批改状态矩阵、排序与 5 项截断、失败资料统计、学生正式提交排除与 ``UPLOADING``
保留待办、最近反馈仅含本人已发布结果、相同时间戳用 ID 稳定排序、空数据返回零与
空数组，以及「查询数量不随课程数增长」与「只读、不加行锁」的服务层断言。

数据直接通过 ORM 写入（绕过上传/Worker 流程），令牌用 :func:`create_access_token`
直接签发，便于精确控制状态与时间戳。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from app.core.config import Settings
from app.modules.assignments.models import (
    Assignment,
    AssignmentRubricVersion,
    AssignmentStatus,
)
from app.modules.auth.models import User, UserRole
from app.modules.auth.security import create_access_token
from app.modules.courses.models import Course, CourseMember, CourseRole, CourseStatus
from app.modules.dashboard import service as dashboard_service
from app.modules.grading.models import GradeReview, Submission, SubmissionStatus
from app.modules.materials.models import (
    Material,
    MaterialStatus,
    MaterialUploadSession,
)

TEACHER_URL = "/api/v1/dashboard/teacher"
STUDENT_URL = "/api/v1/dashboard/student"

PDF_MIME = "application/pdf"


def _uid(n: int) -> uuid.UUID:
    """确定性 UUID，用于排序断言（字节序与 Postgres 的 uuid 比较一致）。"""
    return uuid.UUID(f"00000000-0000-0000-0000-{n:012d}")


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(settings: Settings, user_id: uuid.UUID, role: str) -> str:
    token, _ = create_access_token(user_id=user_id, role=role, settings=settings)
    return token


async def _seed_wrapper(session_factory, seed_fn):
    async with session_factory() as session:
        await seed_fn(session)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 种子数据（直接 ORM 写入）
# --------------------------------------------------------------------------- #
async def _add_user(session, *, email: str, role: UserRole, name: str, uid: int) -> User:
    user = User(
        id=_uid(uid),
        email=email,
        email_normalized=email.lower(),
        password_hash="not-used-in-seed",
        display_name=name,
        role=role,
    )
    session.add(user)
    await session.flush()
    return user


async def _add_course(
    session, *, teacher_id: uuid.UUID, uid: int, status: CourseStatus = CourseStatus.ACTIVE
) -> Course:
    course = Course(
        id=_uid(uid),
        name=f"课程{uid}",
        teacher_id=teacher_id,
        status=status,
        invite_code=uuid.uuid4().hex[:12].upper(),
    )
    session.add(course)
    await session.flush()
    return course


async def _add_member(
    session, *, course_id: uuid.UUID, user_id: uuid.UUID, uid: int
) -> None:
    session.add(
        CourseMember(
            id=_uid(uid),
            course_id=course_id,
            user_id=user_id,
            course_role=CourseRole.STUDENT,
        )
    )


async def _add_assignment(
    session,
    *,
    course_id: uuid.UUID,
    teacher_id: uuid.UUID,
    uid: int,
    title: str,
    status: AssignmentStatus,
    due_at: datetime | None = None,
    allow_late: bool = False,
    published_at: datetime | None = None,
) -> Assignment:
    assignment = Assignment(
        id=_uid(uid),
        course_id=course_id,
        created_by=teacher_id,
        title=title,
        status=status,
        due_at=due_at,
        allow_late_submission=allow_late,
        published_at=published_at,
    )
    session.add(assignment)
    await session.flush()
    return assignment


async def _add_rubric_version(
    session,
    *,
    assignment_id: uuid.UUID,
    teacher_id: uuid.UUID,
    uid: int,
    total_score: float,
) -> AssignmentRubricVersion:
    version = AssignmentRubricVersion(
        id=_uid(uid),
        assignment_id=assignment_id,
        version=1,
        total_score=total_score,
        created_by=teacher_id,
    )
    session.add(version)
    await session.flush()
    return version


async def _add_submission(
    session,
    *,
    assignment_id: uuid.UUID,
    course_id: uuid.UUID,
    student_id: uuid.UUID,
    uid: int,
    status: SubmissionStatus,
    submitted_at: datetime | None = None,
    rubric_version_id: uuid.UUID | None = None,
    is_late: bool = False,
    error_message: str | None = None,
) -> Submission:
    submission = Submission(
        id=_uid(uid),
        assignment_id=assignment_id,
        course_id=course_id,
        student_id=student_id,
        status=status,
        rubric_version_id=rubric_version_id,
        filename="report.pdf",
        content_type=PDF_MIME,
        size=10,
        sha256="a" * 64,
        object_key=f"obj-{uid}",
        is_late=is_late,
        submitted_at=submitted_at,
        error_message=error_message,
    )
    session.add(submission)
    await session.flush()
    return submission


async def _add_review(
    session,
    *,
    submission_id: uuid.UUID,
    uid: int,
    final_total_score: float,
    published_at: datetime | None = None,
) -> GradeReview:
    review = GradeReview(
        id=_uid(uid),
        submission_id=submission_id,
        ai_summary="ai",
        teacher_summary="final",
        suggested_total_score=final_total_score,
        final_total_score=final_total_score,
        published_at=published_at,
    )
    session.add(review)
    await session.flush()
    return review


async def _add_material(
    session,
    *,
    course_id: uuid.UUID,
    uploaded_by: uuid.UUID,
    uid: int,
    status: MaterialStatus,
    filename: str = "课件.pdf",
    error_message: str | None = None,
    deleted_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> Material:
    now = datetime.now(timezone.utc)
    upload = MaterialUploadSession(
        id=_uid(uid + 100000),
        course_id=course_id,
        teacher_id=uploaded_by,
        object_key=f"key-{uid}",
        filename=filename,
        content_type=PDF_MIME,
        size=100,
        sha256="a" * 64,
        upload_url_expires_at=now,
        confirm_deadline_at=now + timedelta(hours=24),
    )
    session.add(upload)
    await session.flush()
    material = Material(
        id=_uid(uid),
        course_id=course_id,
        upload_id=upload.id,
        filename=filename,
        content_type=PDF_MIME,
        size=100,
        sha256="a" * 64,
        storage_key=f"storage-{uid}",
        status=status,
        uploaded_by=uploaded_by,
        error_message=error_message,
        deleted_at=deleted_at,
        updated_at=updated_at or now,
    )
    session.add(material)
    await session.flush()
    return material


async def _seed_two_users(session) -> None:
    await _add_user(session, email="t@example.com", role=UserRole.TEACHER, name="教师", uid=1)
    await _add_user(session, email="s@example.com", role=UserRole.STUDENT, name="学生", uid=2)
    await session.commit()


# --------------------------------------------------------------------------- #
# 角色与空数据
# --------------------------------------------------------------------------- #
def test_teacher_endpoint_rejects_student(api_client, make_settings, pg_session_factory) -> None:
    _run(_seed_wrapper(pg_session_factory, _seed_two_users))
    settings = make_settings()
    student_token = _token(settings, _uid(2), "STUDENT")

    response = api_client.get(TEACHER_URL, headers=_auth(student_token))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ROLE_FORBIDDEN"


def test_student_endpoint_rejects_teacher(api_client, make_settings, pg_session_factory) -> None:
    _run(_seed_wrapper(pg_session_factory, _seed_two_users))
    settings = make_settings()
    teacher_token = _token(settings, _uid(1), "TEACHER")

    response = api_client.get(STUDENT_URL, headers=_auth(teacher_token))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ROLE_FORBIDDEN"


def test_teacher_dashboard_empty(api_client, make_settings, pg_session_factory) -> None:
    _run(_seed_wrapper(pg_session_factory, _seed_two_users))
    settings = make_settings()
    teacher_token = _token(settings, _uid(1), "TEACHER")

    response = api_client.get(TEACHER_URL, headers=_auth(teacher_token))
    assert response.status_code == 200
    body = response.json()
    assert body["active_course_count"] == 0
    assert body["pending_grading_count"] == 0
    assert body["failed_material_count"] == 0
    assert body["recent_submissions"] == []
    assert body["failed_materials"] == []


def test_student_dashboard_empty(api_client, make_settings, pg_session_factory) -> None:
    _run(_seed_wrapper(pg_session_factory, _seed_two_users))
    settings = make_settings()
    student_token = _token(settings, _uid(2), "STUDENT")

    response = api_client.get(STUDENT_URL, headers=_auth(student_token))
    assert response.status_code == 200
    body = response.json()
    assert body["active_course_count"] == 0
    assert body["pending_assignment_count"] == 0
    assert body["processing_material_count"] == 0
    assert body["failed_material_count"] == 0
    assert body["pending_assignments"] == []
    assert body["recent_feedback"] == []
    assert body["material_statuses"] == []


# --------------------------------------------------------------------------- #
# 教师：状态矩阵、失败资料、作用域隔离、排序截断
# --------------------------------------------------------------------------- #
async def _seed_teacher_data(session) -> None:
    t1 = await _add_user(session, email="t1@x.com", role=UserRole.TEACHER, name="教师1", uid=1)
    c1 = await _add_course(session, teacher_id=t1.id, uid=100, status=CourseStatus.ACTIVE)

    a1 = await _add_assignment(
        session, course_id=c1.id, teacher_id=t1.id, uid=300, title="任务",
        status=AssignmentStatus.PUBLISHED,
    )
    rv = await _add_rubric_version(
        session, assignment_id=a1.id, teacher_id=t1.id, uid=400, total_score=100
    )
    now = datetime.now(timezone.utc)
    # 一名学生对同一任务只能提交一份，因此用 6 名学生分别覆盖 6 种状态
    statuses = [
        (SubmissionStatus.SUBMITTED, now - timedelta(minutes=5)),
        (SubmissionStatus.GRADING, now - timedelta(minutes=4)),
        (SubmissionStatus.REVIEW_REQUIRED, now - timedelta(minutes=3)),
        (SubmissionStatus.FAILED, now - timedelta(minutes=2)),
        (SubmissionStatus.PUBLISHED, now - timedelta(minutes=1)),
        (SubmissionStatus.UPLOADING, None),
    ]
    for i, (status, submitted_at) in enumerate(statuses):
        student = await _add_user(
            session, email=f"s{i}@x.com", role=UserRole.STUDENT, name=f"学生{i}", uid=2 + i
        )
        await _add_submission(
            session, assignment_id=a1.id, course_id=c1.id, student_id=student.id,
            uid=500 + i, status=status, submitted_at=submitted_at,
            rubric_version_id=rv.id if submitted_at is not None else None,
        )
    # 失败资料 + 已删除失败资料 + 就绪资料
    await _add_material(session, course_id=c1.id, uploaded_by=t1.id, uid=600,
                        status=MaterialStatus.FAILED, error_message="解析失败")
    await _add_material(session, course_id=c1.id, uploaded_by=t1.id, uid=601,
                        status=MaterialStatus.FAILED, error_message="已删除", deleted_at=now)
    await _add_material(session, course_id=c1.id, uploaded_by=t1.id, uid=602,
                        status=MaterialStatus.READY)
    await session.commit()


def test_teacher_pending_grading_matrix_and_failed_materials(
    api_client, make_settings, pg_session_factory
) -> None:
    _run(_seed_wrapper(pg_session_factory, _seed_teacher_data))
    settings = make_settings()
    teacher_token = _token(settings, _uid(1), "TEACHER")

    response = api_client.get(TEACHER_URL, headers=_auth(teacher_token))
    assert response.status_code == 200
    body = response.json()

    assert body["active_course_count"] == 1
    # SUBMITTED / GRADING / REVIEW_REQUIRED / FAILED 共 4，PUBLISHED 与 UPLOADING 不计
    assert body["pending_grading_count"] == 4
    # 只有一份未删除的 FAILED 资料
    assert body["failed_material_count"] == 1
    # 正式提交 5 份（UPLOADING 排除），按 submitted_at DESC；最近的是 PUBLISHED
    assert [item["submission_id"] for item in body["recent_submissions"]] == [
        str(_uid(504)), str(_uid(503)), str(_uid(502)), str(_uid(501)), str(_uid(500))
    ]
    # 失败资料只有 uid=600（uid=601 已删除）
    assert [item["material_id"] for item in body["failed_materials"]] == [str(_uid(600))]


async def _seed_scope_isolation(session) -> None:
    t1 = await _add_user(session, email="t1@x.com", role=UserRole.TEACHER, name="教师1", uid=1)
    t2 = await _add_user(session, email="t2@x.com", role=UserRole.TEACHER, name="教师2", uid=9)
    await _add_course(session, teacher_id=t1.id, uid=100, status=CourseStatus.ACTIVE)
    await _add_course(session, teacher_id=t1.id, uid=101, status=CourseStatus.ARCHIVED)
    await _add_course(session, teacher_id=t2.id, uid=200, status=CourseStatus.ACTIVE)
    await session.commit()


def test_teacher_scope_isolation(api_client, make_settings, pg_session_factory) -> None:
    _run(_seed_wrapper(pg_session_factory, _seed_scope_isolation))
    settings = make_settings()
    t1_token = _token(settings, _uid(1), "TEACHER")
    t2_token = _token(settings, _uid(9), "TEACHER")

    t1_body = api_client.get(TEACHER_URL, headers=_auth(t1_token)).json()
    t2_body = api_client.get(TEACHER_URL, headers=_auth(t2_token)).json()

    # 教师1 只有 1 个活动课程（归档课程不算）
    assert t1_body["active_course_count"] == 1
    # 教师2 只看到自己的 1 个课程
    assert t2_body["active_course_count"] == 1


async def _seed_many_submissions(session) -> None:
    t1 = await _add_user(session, email="t1@x.com", role=UserRole.TEACHER, name="教师", uid=1)
    c1 = await _add_course(session, teacher_id=t1.id, uid=100)
    a1 = await _add_assignment(session, course_id=c1.id, teacher_id=t1.id, uid=300,
                               title="任务", status=AssignmentStatus.PUBLISHED)
    rv = await _add_rubric_version(session, assignment_id=a1.id, teacher_id=t1.id, uid=400, total_score=100)
    now = datetime.now(timezone.utc)
    for i in range(7):
        student = await _add_user(
            session, email=f"s{i}@x.com", role=UserRole.STUDENT, name=f"学生{i}", uid=2 + i
        )
        await _add_submission(session, assignment_id=a1.id, course_id=c1.id, student_id=student.id,
                              uid=500 + i, status=SubmissionStatus.SUBMITTED,
                              submitted_at=now + timedelta(minutes=i), rubric_version_id=rv.id)
    await session.commit()


def test_teacher_recent_submissions_truncated_to_five(
    api_client, make_settings, pg_session_factory
) -> None:
    _run(_seed_wrapper(pg_session_factory, _seed_many_submissions))
    settings = make_settings()
    token = _token(settings, _uid(1), "TEACHER")
    body = api_client.get(TEACHER_URL, headers=_auth(token)).json()

    assert len(body["recent_submissions"]) == 5
    # 最新 5 份：uid 506..502，按 submitted_at DESC
    assert [item["submission_id"] for item in body["recent_submissions"]] == [
        str(_uid(506)), str(_uid(505)), str(_uid(504)), str(_uid(503)), str(_uid(502))
    ]


# --------------------------------------------------------------------------- #
# 学生：待完成任务、最近反馈、资料状态
# --------------------------------------------------------------------------- #
async def _seed_student_pending(session) -> None:
    t = await _add_user(session, email="t@x.com", role=UserRole.TEACHER, name="教师", uid=1)
    s = await _add_user(session, email="s@x.com", role=UserRole.STUDENT, name="学生", uid=2)
    c = await _add_course(session, teacher_id=t.id, uid=100)
    await _add_member(session, course_id=c.id, user_id=s.id, uid=700)
    now = datetime.now(timezone.utc)
    # 待完成：无截止 / 未来截止 / 过期可补交
    await _add_assignment(session, course_id=c.id, teacher_id=t.id, uid=301,
                          title="无截止", status=AssignmentStatus.PUBLISHED, published_at=now)
    await _add_assignment(session, course_id=c.id, teacher_id=t.id, uid=302,
                          title="未来截止", status=AssignmentStatus.PUBLISHED,
                          due_at=now + timedelta(days=1), published_at=now)
    await _add_assignment(session, course_id=c.id, teacher_id=t.id, uid=303,
                          title="过期可补交", status=AssignmentStatus.PUBLISHED,
                          due_at=now - timedelta(days=1), allow_late=True, published_at=now)
    # 不待完成：过期禁止补交 / DRAFT
    await _add_assignment(session, course_id=c.id, teacher_id=t.id, uid=304,
                          title="过期禁补交", status=AssignmentStatus.PUBLISHED,
                          due_at=now - timedelta(days=1), allow_late=False, published_at=now)
    await _add_assignment(session, course_id=c.id, teacher_id=t.id, uid=305,
                          title="草稿", status=AssignmentStatus.DRAFT)
    # 已正式提交 → 不待完成
    a_done = await _add_assignment(session, course_id=c.id, teacher_id=t.id, uid=306,
                                   title="已提交", status=AssignmentStatus.PUBLISHED,
                                   due_at=now + timedelta(days=1), published_at=now)
    rv = await _add_rubric_version(session, assignment_id=a_done.id, teacher_id=t.id, uid=401, total_score=100)
    await _add_submission(session, assignment_id=a_done.id, course_id=c.id, student_id=s.id,
                          uid=510, status=SubmissionStatus.SUBMITTED, submitted_at=now, rubric_version_id=rv.id)
    # 仅 UPLOADING → 仍待完成
    a_uploading = await _add_assignment(session, course_id=c.id, teacher_id=t.id, uid=307,
                                        title="仅上传", status=AssignmentStatus.PUBLISHED,
                                        due_at=now + timedelta(days=1), published_at=now)
    await _add_submission(session, assignment_id=a_uploading.id, course_id=c.id, student_id=s.id,
                          uid=511, status=SubmissionStatus.UPLOADING, submitted_at=None)
    await session.commit()


def test_student_pending_assignments_and_uploading_preserved(
    api_client, make_settings, pg_session_factory
) -> None:
    _run(_seed_wrapper(pg_session_factory, _seed_student_pending))
    settings = make_settings()
    token = _token(settings, _uid(2), "STUDENT")
    body = api_client.get(STUDENT_URL, headers=_auth(token)).json()

    assert body["active_course_count"] == 1
    # 待完成：无截止 / 未来截止 / 过期可补交 / 仅上传 = 4
    assert body["pending_assignment_count"] == 4
    pending_ids = {item["assignment_id"] for item in body["pending_assignments"]}
    assert pending_ids == {str(_uid(301)), str(_uid(302)), str(_uid(303)), str(_uid(307))}


async def _seed_feedback(session) -> None:
    t = await _add_user(session, email="t@x.com", role=UserRole.TEACHER, name="教师", uid=1)
    s = await _add_user(session, email="s@x.com", role=UserRole.STUDENT, name="学生", uid=2)
    other = await _add_user(session, email="o@x.com", role=UserRole.STUDENT, name="他人", uid=3)
    c = await _add_course(session, teacher_id=t.id, uid=100)
    await _add_member(session, course_id=c.id, user_id=s.id, uid=700)
    await _add_member(session, course_id=c.id, user_id=other.id, uid=701)
    a = await _add_assignment(session, course_id=c.id, teacher_id=t.id, uid=300,
                              title="任务", status=AssignmentStatus.PUBLISHED)
    rv = await _add_rubric_version(session, assignment_id=a.id, teacher_id=t.id, uid=400, total_score=100)
    now = datetime.now(timezone.utc)
    # 本人已发布 + 他人已发布（同一任务，两名学生）
    sub_own = await _add_submission(session, assignment_id=a.id, course_id=c.id, student_id=s.id,
                                    uid=500, status=SubmissionStatus.PUBLISHED, submitted_at=now, rubric_version_id=rv.id)
    sub_other = await _add_submission(session, assignment_id=a.id, course_id=c.id, student_id=other.id,
                                      uid=501, status=SubmissionStatus.PUBLISHED, submitted_at=now, rubric_version_id=rv.id)
    await _add_review(session, submission_id=sub_own.id, uid=600, final_total_score=88.5, published_at=now)
    await _add_review(session, submission_id=sub_other.id, uid=601, final_total_score=90.0, published_at=now)
    # 本人另一任务上的未发布批改（不应出现）
    a2 = await _add_assignment(session, course_id=c.id, teacher_id=t.id, uid=301,
                               title="任务二", status=AssignmentStatus.PUBLISHED)
    rv2 = await _add_rubric_version(session, assignment_id=a2.id, teacher_id=t.id, uid=401, total_score=100)
    sub_own_unpub = await _add_submission(session, assignment_id=a2.id, course_id=c.id, student_id=s.id,
                                          uid=502, status=SubmissionStatus.REVIEW_REQUIRED,
                                          submitted_at=now, rubric_version_id=rv2.id)
    await _add_review(session, submission_id=sub_own_unpub.id, uid=602, final_total_score=70.0, published_at=None)
    await session.commit()


def test_student_recent_feedback_only_own_published(
    api_client, make_settings, pg_session_factory
) -> None:
    _run(_seed_wrapper(pg_session_factory, _seed_feedback))
    settings = make_settings()
    token = _token(settings, _uid(2), "STUDENT")
    body = api_client.get(STUDENT_URL, headers=_auth(token)).json()

    assert len(body["recent_feedback"]) == 1
    feedback = body["recent_feedback"][0]
    assert feedback["submission_id"] == str(_uid(500))
    assert feedback["final_total_score"] == 88.5
    assert feedback["total_score"] == 100.0


async def _seed_materials(session) -> None:
    t = await _add_user(session, email="t@x.com", role=UserRole.TEACHER, name="教师", uid=1)
    s = await _add_user(session, email="s@x.com", role=UserRole.STUDENT, name="学生", uid=2)
    c = await _add_course(session, teacher_id=t.id, uid=100)
    await _add_member(session, course_id=c.id, user_id=s.id, uid=700)
    await _add_material(session, course_id=c.id, uploaded_by=t.id, uid=600, status=MaterialStatus.PROCESSING)
    await _add_material(session, course_id=c.id, uploaded_by=t.id, uid=601, status=MaterialStatus.FAILED, error_message="失败")
    await _add_material(session, course_id=c.id, uploaded_by=t.id, uid=602, status=MaterialStatus.READY)
    await session.commit()


def test_student_material_statuses(api_client, make_settings, pg_session_factory) -> None:
    _run(_seed_wrapper(pg_session_factory, _seed_materials))
    settings = make_settings()
    token = _token(settings, _uid(2), "STUDENT")
    body = api_client.get(STUDENT_URL, headers=_auth(token)).json()

    assert body["processing_material_count"] == 1
    assert body["failed_material_count"] == 1
    statuses = {(item["material_id"], item["status"]) for item in body["material_statuses"]}
    assert statuses == {(str(_uid(600)), "PROCESSING"), (str(_uid(601)), "FAILED")}


async def _seed_same_timestamp(session) -> None:
    t = await _add_user(session, email="t@x.com", role=UserRole.TEACHER, name="教师", uid=1)
    c = await _add_course(session, teacher_id=t.id, uid=100)
    a = await _add_assignment(session, course_id=c.id, teacher_id=t.id, uid=300,
                              title="任务", status=AssignmentStatus.PUBLISHED)
    rv = await _add_rubric_version(session, assignment_id=a.id, teacher_id=t.id, uid=400, total_score=100)
    same = datetime.now(timezone.utc)
    for i in (1, 2, 3):
        student = await _add_user(
            session, email=f"s{i}@x.com", role=UserRole.STUDENT, name=f"学生{i}", uid=2 + i
        )
        await _add_submission(session, assignment_id=a.id, course_id=c.id, student_id=student.id,
                              uid=500 + i, status=SubmissionStatus.SUBMITTED,
                              submitted_at=same, rubric_version_id=rv.id)
    await session.commit()


def test_same_timestamp_orders_by_id_desc(api_client, make_settings, pg_session_factory) -> None:
    _run(_seed_wrapper(pg_session_factory, _seed_same_timestamp))
    settings = make_settings()
    token = _token(settings, _uid(1), "TEACHER")
    body = api_client.get(TEACHER_URL, headers=_auth(token)).json()

    assert [item["submission_id"] for item in body["recent_submissions"]] == [
        str(_uid(503)), str(_uid(502)), str(_uid(501))
    ]


# --------------------------------------------------------------------------- #
# 服务层：固定查询数量（防 N+1）与只读、不加锁
# --------------------------------------------------------------------------- #
class _CountingSession:
    def __init__(self, real):
        self._real = real
        self.calls = 0
        self.statements: list[str] = []

    async def execute(self, statement, *args, **kwargs):
        self.calls += 1
        self.statements.append(str(statement))
        return await self._real.execute(statement, *args, **kwargs)

    async def scalar(self, statement, *args, **kwargs):
        self.calls += 1
        self.statements.append(str(statement))
        return await self._real.scalar(statement, *args, **kwargs)


def _assert_read_only(statements: list[str]) -> None:
    assert statements, "应当有查询被执行"
    for stmt in statements:
        assert stmt.lstrip().upper().startswith("SELECT"), f"出现非只读语句：{stmt}"
        assert "FOR UPDATE" not in stmt.upper(), f"出现行锁：{stmt}"


def test_teacher_query_count_is_fixed(db_isolation, pg_session_factory) -> None:
    async def _run_async():
        async with pg_session_factory() as session:
            t = await _add_user(session, email="t@x.com", role=UserRole.TEACHER, name="教师", uid=1)
            s = await _add_user(session, email="s@x.com", role=UserRole.STUDENT, name="学生", uid=2)
            for course_idx in (100, 101, 102, 103):
                c = await _add_course(session, teacher_id=t.id, uid=course_idx)
                a = await _add_assignment(session, course_id=c.id, teacher_id=t.id, uid=course_idx + 200,
                                          title="任务", status=AssignmentStatus.PUBLISHED)
                rv = await _add_rubric_version(session, assignment_id=a.id, teacher_id=t.id,
                                               uid=course_idx + 300, total_score=100)
                await _add_submission(session, assignment_id=a.id, course_id=c.id, student_id=s.id,
                                      uid=course_idx + 400, status=SubmissionStatus.SUBMITTED,
                                      submitted_at=datetime.now(timezone.utc), rubric_version_id=rv.id)
            await session.commit()

        async with pg_session_factory() as session:
            counter = _CountingSession(session)
            await dashboard_service.get_teacher_dashboard(counter, user=t)
            return counter.calls, counter.statements

    calls, statements = asyncio.run(_run_async())
    # 固定 5 条查询：活动课程数 / 待批改数 / 失败资料数 / 最近提交 / 失败资料列表
    assert calls == 5
    _assert_read_only(statements)


def test_student_query_count_is_fixed(db_isolation, pg_session_factory) -> None:
    async def _run_async():
        async with pg_session_factory() as session:
            t = await _add_user(session, email="t@x.com", role=UserRole.TEACHER, name="教师", uid=1)
            s = await _add_user(session, email="s@x.com", role=UserRole.STUDENT, name="学生", uid=2)
            for course_idx in (100, 101, 102, 103):
                c = await _add_course(session, teacher_id=t.id, uid=course_idx)
                await _add_member(session, course_id=c.id, user_id=s.id, uid=course_idx + 400)
                await _add_assignment(session, course_id=c.id, teacher_id=t.id, uid=course_idx + 200,
                                      title="任务", status=AssignmentStatus.PUBLISHED)
                await _add_material(session, course_id=c.id, uploaded_by=t.id, uid=course_idx + 300,
                                    status=MaterialStatus.PROCESSING)
            await session.commit()

        async with pg_session_factory() as session:
            counter = _CountingSession(session)
            await dashboard_service.get_student_dashboard(counter, user=s)
            return counter.calls, counter.statements

    calls, statements = asyncio.run(_run_async())
    # 固定 6 条查询：活动课程数 / 待完成任务数 / 资料状态计数 / 待完成任务 / 反馈 / 资料状态
    assert calls == 6
    _assert_read_only(statements)
