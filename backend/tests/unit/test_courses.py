"""Courses 单元测试：请求校验、邀请码生成与权限判断。

不接触数据库；涉及 repository 的用例用 monkeypatch 替换查询函数，
只验证 service 的判断逻辑本身。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.errors import CourseArchivedError, CourseForbiddenError
from app.modules.courses import repository as repo
from app.modules.courses import service
from app.modules.courses.models import (
    COURSE_DESCRIPTION_MAX_LENGTH,
    INVITE_CODE_LENGTH,
    CourseRole,
    CourseStatus,
)
from app.modules.courses.schemas import (
    CourseCreateRequest,
    CourseJoinRequest,
    CourseUpdateRequest,
)


def _async(value):
    """把常量包装成 repository 查询函数的形状。"""

    async def _inner(*_args, **_kwargs):
        return value

    return _inner


def _course(*, teacher_id: uuid.UUID, status: CourseStatus = CourseStatus.ACTIVE):
    return SimpleNamespace(
        id=uuid.uuid4(),
        teacher_id=teacher_id,
        status=status,
        invite_code="AB12CD34EF56",
    )


# --------------------------------------------------------------------------- #
# 邀请码生成
# --------------------------------------------------------------------------- #
def test_invite_code_is_twelve_uppercase_alphanumeric() -> None:
    code = service.generate_invite_code()

    assert len(code) == INVITE_CODE_LENGTH
    assert code.isalnum() and code.isupper()


def test_invite_codes_are_random() -> None:
    codes = {service.generate_invite_code() for _ in range(200)}

    assert len(codes) == 200, "随机邀请码不应重复"


# --------------------------------------------------------------------------- #
# 创建课程请求
# --------------------------------------------------------------------------- #
def test_create_request_strips_name() -> None:
    payload = CourseCreateRequest(name="  软件工程实验  ", description="说明")

    assert payload.name == "软件工程实验"
    assert payload.description == "说明"


def test_create_request_defaults_description_to_empty_string() -> None:
    assert CourseCreateRequest(name="课程").description == ""


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_create_request_rejects_blank_name(name: str) -> None:
    with pytest.raises(ValidationError):
        CourseCreateRequest(name=name)


def test_create_request_rejects_too_long_name() -> None:
    with pytest.raises(ValidationError):
        CourseCreateRequest(name="x" * 101)

    # 去除空白后刚好 100 个字符是允许的
    assert CourseCreateRequest(name=f"  {'x' * 100}  ").name == "x" * 100


def test_create_request_rejects_too_long_description() -> None:
    with pytest.raises(ValidationError):
        CourseCreateRequest(name="课程", description="x" * (COURSE_DESCRIPTION_MAX_LENGTH + 1))


def test_create_request_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        CourseCreateRequest(name="课程", teacher_id=str(uuid.uuid4()))


def test_create_request_rejects_explicit_null() -> None:
    with pytest.raises(ValidationError):
        CourseCreateRequest(name="课程", description=None)


# --------------------------------------------------------------------------- #
# 修改课程请求
# --------------------------------------------------------------------------- #
def test_update_request_requires_at_least_one_field() -> None:
    with pytest.raises(ValidationError):
        CourseUpdateRequest()


def test_update_request_accepts_single_field() -> None:
    assert CourseUpdateRequest(name="新名称").name == "新名称"
    assert CourseUpdateRequest(description="").description == ""


def test_update_request_strips_name_when_provided() -> None:
    assert CourseUpdateRequest(name="  新名称 ").name == "新名称"


def test_update_request_rejects_null_and_unknown_field() -> None:
    with pytest.raises(ValidationError):
        CourseUpdateRequest(name=None)

    with pytest.raises(ValidationError):
        CourseUpdateRequest(status="ARCHIVED")


def test_update_request_allows_clearing_description() -> None:
    """description 传空字符串表示清空，与"省略"含义不同。"""
    payload = CourseUpdateRequest(description="")

    assert payload.description == ""
    assert payload.name is None


# --------------------------------------------------------------------------- #
# 加入课程请求
# --------------------------------------------------------------------------- #
def test_join_request_rejects_empty_invite_code() -> None:
    with pytest.raises(ValidationError):
        CourseJoinRequest(invite_code="")


def test_join_request_accepts_opaque_code() -> None:
    payload = CourseJoinRequest(invite_code="AB12CD34EF56")

    assert payload.invite_code == "AB12CD34EF56"
    assert CourseJoinRequest(invite_code="x" * 65).invite_code == "x" * 65


# --------------------------------------------------------------------------- #
# 权限判断
# --------------------------------------------------------------------------- #
def test_visible_invite_code_only_for_creator_of_active_course() -> None:
    teacher_id = uuid.uuid4()
    active = _course(teacher_id=teacher_id)
    archived = _course(teacher_id=teacher_id, status=CourseStatus.ARCHIVED)
    teacher = SimpleNamespace(id=teacher_id)
    student = SimpleNamespace(id=uuid.uuid4())

    assert service.visible_invite_code(active, user=teacher) == active.invite_code
    assert service.visible_invite_code(active, user=student) is None
    # 归档后连创建教师也看不到
    assert service.visible_invite_code(archived, user=teacher) is None


def test_require_course_active_rejects_archived() -> None:
    service.require_course_active(_course(teacher_id=uuid.uuid4()))

    with pytest.raises(CourseArchivedError):
        service.require_course_active(
            _course(teacher_id=uuid.uuid4(), status=CourseStatus.ARCHIVED)
        )


async def test_require_course_member_raises_404_when_missing(monkeypatch) -> None:
    monkeypatch.setattr(repo, "get_course_by_id", _async(None))

    with pytest.raises(Exception) as excinfo:
        await service.require_course_member(
            session=None, user=SimpleNamespace(id=uuid.uuid4()), course_id=uuid.uuid4()
        )

    assert excinfo.value.code.value == "RESOURCE_NOT_FOUND"


async def test_require_course_member_raises_403_for_non_member(monkeypatch) -> None:
    course = _course(teacher_id=uuid.uuid4())
    monkeypatch.setattr(repo, "get_course_by_id", _async(course))
    monkeypatch.setattr(repo, "get_member", _async(None))

    with pytest.raises(CourseForbiddenError):
        await service.require_course_member(
            session=None, user=SimpleNamespace(id=uuid.uuid4()), course_id=course.id
        )


async def test_require_course_member_passes_for_member(monkeypatch) -> None:
    course = _course(teacher_id=uuid.uuid4())
    user_id = uuid.uuid4()
    monkeypatch.setattr(repo, "get_course_by_id", _async(course))
    monkeypatch.setattr(
        repo,
        "get_member",
        _async(SimpleNamespace(course_id=course.id, user_id=user_id)),
    )

    loaded = await service.require_course_member(
        session=None, user=SimpleNamespace(id=user_id), course_id=course.id
    )

    assert loaded is course


async def test_require_course_teacher_rejects_other_teacher(monkeypatch) -> None:
    """平台角色是教师，也不代表能管理别人的课程。"""
    course = _course(teacher_id=uuid.uuid4())
    monkeypatch.setattr(repo, "get_course_by_id", _async(course))

    with pytest.raises(CourseForbiddenError):
        await service.require_course_teacher(
            session=None, user=SimpleNamespace(id=uuid.uuid4()), course_id=course.id
        )


async def test_require_course_teacher_passes_for_creator(monkeypatch) -> None:
    teacher_id = uuid.uuid4()
    course = _course(teacher_id=teacher_id)
    monkeypatch.setattr(repo, "get_course_by_id", _async(course))

    loaded = await service.require_course_teacher(
        session=None, user=SimpleNamespace(id=teacher_id), course_id=course.id
    )

    assert loaded is course


def test_member_role_values_match_contract() -> None:
    assert {role.value for role in CourseRole} == {"TEACHER", "STUDENT"}
    assert {status.value for status in CourseStatus} == {"ACTIVE", "ARCHIVED"}
