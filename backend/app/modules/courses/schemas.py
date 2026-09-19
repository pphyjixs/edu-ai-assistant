"""Courses 模块请求与响应模型。

字段名与状态码以 ``docs/api-contract.md`` 第 3 节为准：

- 请求拒绝未声明字段和显式 ``null``；
- 课程名称去除首尾空白后校验 1–100 字符；
- 详情用两个模型表达可见性：普通 :class:`CourseDetail` 不含邀请码，
  :class:`CourseDetailWithInviteCode` 要求非空邀请码。
"""

from __future__ import annotations

import uuid
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.time import UtcTimestamp
from app.modules.courses.models import (
    COURSE_DESCRIPTION_MAX_LENGTH,
    COURSE_NAME_MAX_LENGTH,
    CourseRole,
    CourseStatus,
)

class _StrictCourseRequest(BaseModel):
    """课程请求基类：拒绝未声明字段与显式 ``null``。

    契约 3.2：未声明字段和显式 ``null`` 都返回 ``422 VALIDATION_ERROR``，
    避免前端拼错字段名后请求被静默忽略。
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: object) -> object:
        if isinstance(data, dict):
            null_fields = sorted(str(key) for key, value in data.items() if value is None)
            if null_fields:
                raise ValueError(f"字段不能为 null：{'、'.join(null_fields)}")
        return data


def _clean_course_name(value: str) -> str:
    """去除首尾空白后校验 1–100 个字符，返回清洗后的名称。"""
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("课程名称不能为空或全为空白字符")
    if len(cleaned) > COURSE_NAME_MAX_LENGTH:
        raise ValueError(f"课程名称不能超过 {COURSE_NAME_MAX_LENGTH} 个字符")
    return cleaned


class CourseCreateRequest(_StrictCourseRequest):
    """创建课程请求（契约 3.2）。"""

    #: 由校验器清洗并校验长度，因此不在此处声明 min_length / max_length
    name: str
    description: str = Field(default="", max_length=COURSE_DESCRIPTION_MAX_LENGTH)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _clean_course_name(value)


class CourseUpdateRequest(_StrictCourseRequest):
    """修改课程请求（契约 3.2）。

    省略的字段保持原值；``description`` 传空字符串表示清空。
    至少需要提供一个字段，空对象返回 ``422``。
    """

    name: str | None = None
    description: str | None = Field(default=None, max_length=COURSE_DESCRIPTION_MAX_LENGTH)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _clean_course_name(value)

    @model_validator(mode="after")
    def _require_at_least_one_field(self) -> Self:
        if self.name is None and self.description is None:
            raise ValueError("修改请求至少需要包含 name 或 description 中的一项")
        return self


class CourseJoinRequest(_StrictCourseRequest):
    """加入课程请求（契约 3.2）。"""

    invite_code: str = Field(min_length=1)


class CourseSummary(BaseModel):
    """课程概要：列表与加入响应使用，**不含邀请码**。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    teacher_id: uuid.UUID
    status: CourseStatus
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


class CourseDetail(CourseSummary):
    """普通课程详情：成员和归档课程的响应中没有邀请码字段。"""


class CourseDetailWithInviteCode(CourseDetail):
    """创建教师查看未归档课程的详情，邀请码必填且不可为 null。"""

    invite_code: str


class CourseMemberSummary(BaseModel):
    """课程成员（契约 3.3）：只含 ID 与显示名称，不返回邮箱。"""

    user_id: uuid.UUID
    display_name: str
    course_role: CourseRole
    joined_at: UtcTimestamp


class InviteCodeResponse(BaseModel):
    """重置邀请码响应（契约 3.4）。"""

    invite_code: str
