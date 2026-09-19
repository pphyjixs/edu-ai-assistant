"""Auth 模块请求与响应模型。

字段名、状态码与响应结构以 ``docs/api-contract.md`` 第 2 节为准。
时间字段统一使用 :data:`app.core.time.UtcTimestamp`，输出为 ``...Z`` 结尾的
ISO 8601 UTC 字符串。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from email_validator import EmailNotValidError, validate_email
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from app.core.time import UtcTimestamp
from app.modules.auth.models import DISPLAY_NAME_MAX_LENGTH, UserRole

#: 密码长度下限（契约 2.1：8–128 个字符）
PASSWORD_MIN_LENGTH = 8

#: 密码长度上限；超限直接拒绝，不截断
PASSWORD_MAX_LENGTH = 128


def validate_raw_email(value: str) -> str:
    """校验邮箱格式，但 **原样返回** 输入值。

    刻意不用 pydantic 的 ``EmailStr``：它会调用 email-validator 并把域名规范化成
    小写，而契约 2.1 要求"保存原始邮箱用于展示"，比较值才做域名小写化。
    这里只借用 email-validator 做语法/域名校验，返回值保持用户提交的大小写。
    """
    try:
        validate_email(value, check_deliverability=False)
    except EmailNotValidError as exc:
        # 只给出统一文案，不回显用户输入
        raise ValueError("邮箱格式不正确") from exc
    return value


#: 邮箱输入类型：校验通过后保留原始大小写
RawEmail = Annotated[str, AfterValidator(validate_raw_email)]


class _StrictRequest(BaseModel):
    """请求基类：拒绝未声明的字段，避免前端拼错字段名后静默失效。"""

    model_config = ConfigDict(extra="forbid")


class RegisterRequest(_StrictRequest):
    """注册请求（契约 2.1）。"""

    email: RawEmail
    # 不 strip：契约要求密码区分大小写且不自动去除首尾空格
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)
    display_name: str = Field(min_length=1, max_length=DISPLAY_NAME_MAX_LENGTH)
    role: UserRole

    @field_validator("display_name")
    @classmethod
    def _reject_blank_display_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("姓名不能为空或全为空白字符")
        return cleaned


class LoginRequest(_StrictRequest):
    """登录请求（契约 2.1，JSON 提交）。"""

    email: RawEmail
    # 登录不做长度校验：规则变化不应把老用户挡在门外，
    # 由密码校验决定成败，且失败信息不区分原因。
    password: str


class RefreshRequest(_StrictRequest):
    """刷新请求（契约 2.4）：不要求有效的 Access Token。"""

    refresh_token: str = Field(min_length=1)


class LogoutRequest(_StrictRequest):
    """注销请求（契约 2.5）：指定要撤销的当前会话。"""

    refresh_token: str = Field(min_length=1)


class UserSummary(BaseModel):
    """登录响应中的用户概要，字段与契约 2.1 的示例一致。"""

    id: uuid.UUID
    display_name: str
    role: UserRole


class UserProfile(BaseModel):
    """当前用户资料；用于 ``GET /users/me`` 与注册响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    display_name: str
    role: UserRole
    created_at: UtcTimestamp


class LoginResponse(BaseModel):
    """登录响应（契约 2.1）。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserSummary


class RefreshResponse(BaseModel):
    """刷新响应（契约 2.4）：只返回新的 Access Token。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
