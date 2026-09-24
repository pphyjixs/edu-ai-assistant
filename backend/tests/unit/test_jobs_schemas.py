"""Jobs 响应 Schema、公开枚举与范围校验的单元测试（契约 10.0）。

不接触数据库：用普通对象模拟 ORM 行，验证

- ``JobStatus`` 的字段集合、必填性与内部枚举到公开枚举的转换；
- ``progress`` 的 0–100 约束与 ``error`` 的 500 字符上限；
- 公开枚举只含三类通用任务，内部枚举仍含 ``AGENT_RUN``；
- 公开范围判定（类型与资源类型必须配对）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from app.modules.jobs import schemas as jobs_schemas
from app.modules.jobs.models import (
    JOB_ERROR_MAX_LENGTH,
    JobResourceType,
    JobStatusValue,
    JobType,
)
from app.modules.jobs.schemas import JobStatus, is_public_job, public_job_type
from pydantic import ValidationError as PydanticValidationError

#: 契约 10.0 的响应字段（顺序无关，集合必须完全一致）
EXPECTED_FIELDS = {
    "id",
    "type",
    "status",
    "progress",
    "resource_type",
    "resource_id",
    "error",
    "failure_stage",
    "created_at",
    "started_at",
    "finished_at",
}

#: 内部调度字段：不得出现在任何响应里
INTERNAL_FIELDS = {"attempts", "run_token", "lease_expires_at"}


class FakeJob:
    """模拟 ORM 的 jobs 行（``JobStatus.model_validate`` 依赖属性访问）。"""

    def __init__(self, **overrides: object) -> None:
        created = datetime(2026, 9, 18, 8, 30, tzinfo=timezone.utc)
        self.id = uuid.uuid4()
        self.type = JobType.MATERIAL_PARSE
        self.status = JobStatusValue.PENDING
        self.progress = 0
        self.resource_type = JobResourceType.MATERIAL
        self.resource_id = uuid.uuid4()
        self.error = None
        self.created_at = created
        self.started_at = None
        self.finished_at = None
        for key, value in overrides.items():
            setattr(self, key, value)


def _validate(**overrides: object) -> JobStatus:
    return JobStatus.model_validate(FakeJob(**overrides))


def test_response_fields_match_contract() -> None:
    assert set(JobStatus.model_fields) == EXPECTED_FIELDS
    assert not (INTERNAL_FIELDS & set(JobStatus.model_fields))


def test_internal_enum_is_converted_to_public_enum() -> None:
    """ORM 的内部枚举必须能转成公开枚举（否则响应会 500）。"""
    payload = _validate(type=JobType.PRACTICE_GENERATE, resource_type=JobResourceType.PRACTICE_SET)

    assert payload.type is jobs_schemas.JobType.PRACTICE_GENERATE
    assert payload.resource_type is jobs_schemas.JobResourceType.PRACTICE_SET
    assert isinstance(payload.type, jobs_schemas.JobType)


def test_agent_run_is_not_a_public_value() -> None:
    """防御性：AGENT_RUN 不属于公开枚举（服务层已先行 404）。"""
    with pytest.raises(PydanticValidationError):
        _validate(type=JobType.AGENT_RUN, resource_type=JobResourceType.AGENT_RUN)


@pytest.mark.parametrize("progress", [0, 1, 99, 100])
def test_progress_within_range_is_accepted(progress: int) -> None:
    assert _validate(progress=progress).progress == progress


@pytest.mark.parametrize("progress", [-1, 101, 1000])
def test_progress_out_of_range_is_rejected(progress: int) -> None:
    with pytest.raises(PydanticValidationError):
        _validate(progress=progress)


def test_error_length_boundary() -> None:
    assert len(_validate(error="x" * JOB_ERROR_MAX_LENGTH).error or "") == JOB_ERROR_MAX_LENGTH
    with pytest.raises(PydanticValidationError):
        _validate(error="x" * (JOB_ERROR_MAX_LENGTH + 1))


def test_timestamps_serialize_as_utc_iso8601() -> None:
    payload = _validate(started_at=datetime(2026, 9, 18, 8, 30, 2, tzinfo=timezone.utc))
    dumped = payload.model_dump(mode="json")

    assert dumped["created_at"].endswith("Z")
    assert dumped["started_at"].endswith("Z")
    assert dumped["finished_at"] is None


def test_public_enums_exclude_agent_run() -> None:
    assert {member.value for member in jobs_schemas.JobType} == {
        "MATERIAL_PARSE",
        "PRACTICE_GENERATE",
        "SUBMISSION_GRADE",
    }
    assert {member.value for member in jobs_schemas.JobResourceType} == {
        "MATERIAL",
        "PRACTICE_SET",
        "SUBMISSION",
    }
    # 数据库内部枚举仍含 AGENT_RUN（Agent Worker 依赖）
    assert "AGENT_RUN" in {member.value for member in JobType}
    assert "AGENT_RUN" in {member.value for member in JobResourceType}


@pytest.mark.parametrize(
    ("job_type", "resource_type", "expected"),
    [
        (JobType.MATERIAL_PARSE, JobResourceType.MATERIAL, True),
        (JobType.PRACTICE_GENERATE, JobResourceType.PRACTICE_SET, True),
        (JobType.SUBMISSION_GRADE, JobResourceType.SUBMISSION, True),
        (JobType.AGENT_RUN, JobResourceType.AGENT_RUN, False),
        # 类型与资源类型不配对：同样不属于公开范围
        (JobType.PRACTICE_GENERATE, JobResourceType.SUBMISSION, False),
        (JobType.MATERIAL_PARSE, JobResourceType.AGENT_RUN, False),
    ],
)
def test_public_scope_detection(
    job_type: JobType, resource_type: JobResourceType, expected: bool
) -> None:
    assert is_public_job(job_type, resource_type) is expected


def test_public_job_type_maps_internal_enum() -> None:
    assert public_job_type(JobType.SUBMISSION_GRADE) is jobs_schemas.JobType.SUBMISSION_GRADE
    assert public_job_type(JobType.AGENT_RUN) is None
