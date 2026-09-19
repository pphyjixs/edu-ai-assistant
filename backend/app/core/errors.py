"""统一错误结构与业务异常。

响应结构固定为 ``docs/api-contract.md`` 第 1 节的约定：

.. code-block:: json

    {"error": {"code": "...", "message": "...", "details": {}, "request_id": "..."}}

约定：

- ``message`` 面向终端用户，不包含异常堆栈、SQL 或内部路径。
- ``details`` 只放可安全展示的结构化信息（字段级校验错误、缺失配置项名称等）。
- 任何异常都不得把密码、令牌或密钥写入响应或日志。
"""

from __future__ import annotations

from typing import Any

from app.core.error_codes import ErrorCode, default_message, default_status_code
from app.core.request_context import get_request_id_or_placeholder

#: details 的取值类型，限制为可 JSON 序列化的结构
ErrorDetails = dict[str, Any]


def build_error_body(
    code: ErrorCode,
    message: str,
    details: ErrorDetails | None = None,
) -> dict[str, ErrorDetails]:
    """按契约组装错误响应体，并补全当前请求的 request ID。

    供异常处理器在无法实例化 :class:`ApiError` 的场景（如框架级 HTTP 错误）复用。
    """
    return {
        "error": {
            "code": code.value,
            "message": message,
            "details": dict(details or {}),
            "request_id": get_request_id_or_placeholder(),
        }
    }


class ApiError(Exception):
    """所有可预期业务错误的基类。

    业务模块只需继承本类并声明 :attr:`code`，或在抛出时覆盖状态码，
    不需要关心响应封装与 request ID。
    """

    #: 稳定错误码，子类必须覆盖
    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(
        self,
        message: str | None = None,
        *,
        details: ErrorDetails | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or default_message(self.code)
        self.details: ErrorDetails = dict(details or {})
        self.status_code = status_code or default_status_code(self.code)
        super().__init__(self.message)

    def to_response_body(self) -> dict[str, ErrorDetails]:
        """构造响应体，自动补全当前请求的 request ID。"""
        return build_error_body(self.code, self.message, self.details)


class InvalidCredentialsError(ApiError):
    code = ErrorCode.AUTH_INVALID_CREDENTIALS


class TokenExpiredError(ApiError):
    code = ErrorCode.AUTH_TOKEN_EXPIRED


class EmailTakenError(ApiError):
    """注册邮箱已被占用（含并发注册被数据库唯一约束拦下的情况）。"""

    code = ErrorCode.AUTH_EMAIL_TAKEN


class TooManyAttemptsError(ApiError):
    """同一规范化邮箱的登录失败次数超过限流阈值。"""

    code = ErrorCode.AUTH_TOO_MANY_ATTEMPTS


class RoleForbiddenError(ApiError):
    code = ErrorCode.ROLE_FORBIDDEN


class CourseForbiddenError(ApiError):
    code = ErrorCode.COURSE_FORBIDDEN


class CourseArchivedError(ApiError):
    """课程已归档，不能执行修改、加入或重置邀请码等写入操作。"""

    code = ErrorCode.COURSE_ARCHIVED


class ResourceNotFoundError(ApiError):
    code = ErrorCode.RESOURCE_NOT_FOUND


class InviteCodeInvalidError(ApiError):
    code = ErrorCode.INVITE_CODE_INVALID


class UploadInvalidError(ApiError):
    code = ErrorCode.UPLOAD_INVALID


class RubricScoreMismatchError(ApiError):
    code = ErrorCode.RUBRIC_SCORE_MISMATCH


class MaterialNotReadyError(ApiError):
    code = ErrorCode.MATERIAL_NOT_READY


class AssignmentNotOpenError(ApiError):
    code = ErrorCode.ASSIGNMENT_NOT_OPEN


class GradeNotReviewedError(ApiError):
    code = ErrorCode.GRADE_NOT_REVIEWED


class AiJobFailedError(ApiError):
    code = ErrorCode.AI_JOB_FAILED


class ValidationError(ApiError):
    """请求参数未通过校验，details 内为 ``errors`` 字段级说明。"""

    code = ErrorCode.VALIDATION_ERROR


class MethodNotAllowedError(ApiError):
    code = ErrorCode.METHOD_NOT_ALLOWED


class InternalError(ApiError):
    """未预期的服务端错误；对外只暴露通用文案。"""

    code = ErrorCode.INTERNAL_ERROR


class ServiceUnavailableError(ApiError):
    """依赖未就绪，仅由 ``/health/ready`` 使用。"""

    code = ErrorCode.SERVICE_UNAVAILABLE
