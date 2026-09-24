"""Dashboard 单元测试（``docs/api-contract.md`` 第 11 节）。

不接触数据库：覆盖待完成任务口径、教师待批改状态集合，以及两种响应
Schema 的字段、枚举、UTC 时间、两位小数与列表上限。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from app.modules.dashboard.repository import PENDING_GRADING_STATUSES
from app.modules.dashboard.schemas import (
    StudentDashboard,
    StudentMaterialStatus,
    StudentPendingAssignment,
    StudentRecentFeedback,
    TeacherDashboard,
    TeacherRecentSubmission,
)
from app.modules.dashboard.service import is_open_for_submission
from app.modules.grading.models import SubmissionStatus
from app.modules.materials.models import MaterialStatus
from pydantic import ValidationError

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)

#: 契约 §11.1 允许的状态集合（正式提交可能出现，排除 UPLOADING）
TEACHER_SUBMISSION_STATUSES = {
    "SUBMITTED",
    "GRADING",
    "REVIEW_REQUIRED",
    "PUBLISHED",
    "FAILED",
}

#: 契约 §11.2 允许的资料处理状态集合（排除 UPLOADING / UPLOADED / READY）
STUDENT_MATERIAL_STATUSES = {"PROCESSING", "FAILED"}


def test_is_open_no_due_date() -> None:
    assert is_open_for_submission(due_at=None, allow_late_submission=False, now=NOW) is True


def test_is_open_future_due_date() -> None:
    assert (
        is_open_for_submission(
            due_at=NOW + timedelta(hours=1), allow_late_submission=False, now=NOW
        )
        is True
    )


def test_is_open_exact_due_moment_is_closed() -> None:
    # now == due_at 视为已经截止（契约 8.10 / 11.2）
    assert (
        is_open_for_submission(due_at=NOW, allow_late_submission=False, now=NOW)
        is False
    )


def test_is_open_past_due_with_late_allowed() -> None:
    assert (
        is_open_for_submission(
            due_at=NOW - timedelta(hours=1), allow_late_submission=True, now=NOW
        )
        is True
    )


def test_is_open_past_due_without_late() -> None:
    assert (
        is_open_for_submission(
            due_at=NOW - timedelta(hours=1), allow_late_submission=False, now=NOW
        )
        is False
    )


def test_pending_grading_statuses_are_the_four_unpublished() -> None:
    assert set(PENDING_GRADING_STATUSES) == {
        SubmissionStatus.SUBMITTED,
        SubmissionStatus.GRADING,
        SubmissionStatus.REVIEW_REQUIRED,
        SubmissionStatus.FAILED,
    }
    # 已发布成绩与未完成上传都不算「待批改」
    assert SubmissionStatus.PUBLISHED not in PENDING_GRADING_STATUSES
    assert SubmissionStatus.UPLOADING not in PENDING_GRADING_STATUSES


def test_teacher_dashboard_fields() -> None:
    assert set(TeacherDashboard.model_fields) == {
        "active_course_count",
        "pending_grading_count",
        "failed_material_count",
        "recent_submissions",
        "failed_materials",
    }


def test_student_dashboard_fields() -> None:
    assert set(StudentDashboard.model_fields) == {
        "active_course_count",
        "pending_assignment_count",
        "processing_material_count",
        "failed_material_count",
        "pending_assignments",
        "recent_feedback",
        "material_statuses",
    }


def test_recent_lists_are_capped_at_five() -> None:
    teacher = TeacherDashboard.model_json_schema()
    assert teacher["properties"]["recent_submissions"]["maxItems"] == 5
    assert teacher["properties"]["failed_materials"]["maxItems"] == 5

    student = StudentDashboard.model_json_schema()
    assert student["properties"]["pending_assignments"]["maxItems"] == 5
    assert student["properties"]["recent_feedback"]["maxItems"] == 5
    assert student["properties"]["material_statuses"]["maxItems"] == 5


def test_status_fields_are_narrowed_to_contract() -> None:
    """状态字段是内联受限枚举，与契约 §11 完全一致（不复用完整领域枚举）。

    同时验证所有允许值可构造、被排除的取值一律拒绝。
    """
    submission_schema = TeacherRecentSubmission.model_json_schema()
    submission_status = submission_schema["properties"]["status"]
    assert "$ref" not in submission_status
    assert set(submission_status["enum"]) == TEACHER_SUBMISSION_STATUSES

    material_schema = StudentMaterialStatus.model_json_schema()
    material_status = material_schema["properties"]["status"]
    assert "$ref" not in material_status
    assert set(material_status["enum"]) == STUDENT_MATERIAL_STATUSES

    submission_base = {
        "submission_id": "00000000-0000-0000-0000-000000000001",
        "assignment_id": "00000000-0000-0000-0000-000000000002",
        "course_id": "00000000-0000-0000-0000-000000000003",
        "student_id": "00000000-0000-0000-0000-000000000004",
        "student_name": "学生甲",
        "is_late": False,
        "submitted_at": NOW,
    }
    for status in TEACHER_SUBMISSION_STATUSES:
        # 字符串与领域枚举成员都必须可构造
        TeacherRecentSubmission(status=status, **submission_base)
        TeacherRecentSubmission(status=SubmissionStatus(status), **submission_base)
    for excluded in ("UPLOADING", SubmissionStatus.UPLOADING):
        with pytest.raises(ValidationError):
            TeacherRecentSubmission(status=excluded, **submission_base)

    material_base = {
        "material_id": "00000000-0000-0000-0000-000000000001",
        "course_id": "00000000-0000-0000-0000-000000000002",
        "filename": "课件.pdf",
        "error_message": None,
        "updated_at": NOW,
    }
    for status in STUDENT_MATERIAL_STATUSES:
        StudentMaterialStatus(status=status, **material_base)
        StudentMaterialStatus(status=MaterialStatus(status), **material_base)
    for excluded in (
        "UPLOADING",
        "UPLOADED",
        "READY",
        MaterialStatus.UPLOADING,
        MaterialStatus.UPLOADED,
        MaterialStatus.READY,
    ):
        with pytest.raises(ValidationError):
            StudentMaterialStatus(status=excluded, **material_base)


def test_time_fields_are_utc_date_time() -> None:
    schema = TeacherRecentSubmission.model_json_schema()
    assert schema["properties"]["submitted_at"] == {
        "type": "string",
        "format": "date-time",
        "title": "Submitted At",
    }


def test_scores_are_numbers_with_two_decimals() -> None:
    schema = StudentRecentFeedback.model_json_schema(mode="serialization")
    for name in ("final_total_score", "total_score"):
        prop = schema["properties"][name]
        assert prop["type"] == "number"
        assert prop["multipleOf"] == 0.01
        assert prop["minimum"] == 0


def test_score_serializes_to_json_number() -> None:
    feedback = StudentRecentFeedback(
        review_id="00000000-0000-0000-0000-000000000001",
        submission_id="00000000-0000-0000-0000-000000000002",
        assignment_id="00000000-0000-0000-0000-000000000003",
        course_id="00000000-0000-0000-0000-000000000004",
        final_total_score=85.5,
        total_score=100.0,
        published_at=NOW,
    )
    dumped = feedback.model_dump(mode="json")
    assert dumped["final_total_score"] == 85.5
    assert dumped["total_score"] == 100.0
    assert dumped["published_at"].endswith("Z")


def test_time_serializes_with_z_suffix() -> None:
    pending = StudentPendingAssignment(
        assignment_id="00000000-0000-0000-0000-000000000001",
        course_id="00000000-0000-0000-0000-000000000002",
        title="实验一",
        due_at=NOW,
        allow_late_submission=False,
        published_at=NOW,
    )
    assert pending.model_dump(mode="json")["due_at"] == "2026-09-23T12:00:00Z"


def test_recent_submission_list_rejects_more_than_five() -> None:
    item = TeacherRecentSubmission(
        submission_id="00000000-0000-0000-0000-000000000001",
        assignment_id="00000000-0000-0000-0000-000000000002",
        course_id="00000000-0000-0000-0000-000000000003",
        student_id="00000000-0000-0000-0000-000000000004",
        student_name="学生甲",
        status=SubmissionStatus.SUBMITTED,
        is_late=False,
        submitted_at=NOW,
    )
    with pytest.raises(ValidationError):
        TeacherDashboard(
            active_course_count=1,
            pending_grading_count=0,
            failed_material_count=0,
            recent_submissions=[item] * 6,
            failed_materials=[],
        )
