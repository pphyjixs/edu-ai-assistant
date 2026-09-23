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


class MaterialAlreadyReadyError(ApiError):
    """资料已解析完成，无需再次解析（契约 5.3 重试解析的就绪分流）。"""

    code = ErrorCode.MATERIAL_ALREADY_READY


class AssignmentNotOpenError(ApiError):
    code = ErrorCode.ASSIGNMENT_NOT_OPEN


class AttachmentAlreadyExistsError(ApiError):
    """同一份任务下已有同名附件（契约 8.15），需先删除旧附件。"""

    code = ErrorCode.ATTACHMENT_ALREADY_EXISTS


class SubmissionAlreadyExistsError(ApiError):
    """同一学生对同一任务已有正式提交（契约 9.2）。"""

    code = ErrorCode.SUBMISSION_ALREADY_EXISTS


class SubmissionNotReadyError(ApiError):
    """提交未完成、批改未生成，或已有复核结果时重复触发（契约 9.6 / 9.9）。"""

    code = ErrorCode.SUBMISSION_NOT_READY


class GradeNotReviewedError(ApiError):
    code = ErrorCode.GRADE_NOT_REVIEWED


class GradeAlreadyPublishedError(ApiError):
    """成绩已发布，不能继续修改复核结果（契约 9.8）。"""

    code = ErrorCode.GRADE_ALREADY_PUBLISHED


class AiJobFailedError(ApiError):
    code = ErrorCode.AI_JOB_FAILED


class ChatConflictError(ApiError):
    """会话在回答生成期间被并发修改（契约 6.1 的乐观锁冲突）。"""

    code = ErrorCode.CHAT_CONFLICT


class PracticeNotReadyError(ApiError):
    """练习尚未生成成功（发布或提交时的状态分流，契约 7.5 / 7.6）。"""

    code = ErrorCode.PRACTICE_NOT_READY


class PracticeAlreadyAttemptedError(ApiError):
    """同一学生对同一练习重复提交（契约 7.6）。"""

    code = ErrorCode.PRACTICE_ALREADY_ATTEMPTED


class JobNotRetryableError(ApiError):
    """任务当前状态不可重试（契约 10.2）。"""

    code = ErrorCode.JOB_NOT_RETRYABLE


class AgentRunInProgressError(ApiError):
    """同一会话已有未结束的 Agent Run（文档 6.5：第一版每会话最多一个）。"""

    code = ErrorCode.AGENT_RUN_IN_PROGRESS


class AgentContextUnsupportedError(ApiError):
    """该 ``entity_type`` 或 action/context 组合尚未实现（文档 6.3 / 6.10）。"""

    code = ErrorCode.AGENT_CONTEXT_UNSUPPORTED


class AgentContextNotReadyError(ApiError):
    """目标资料尚未解析完成，无法注入上下文（文档 6.6 / 6.10）。"""

    code = ErrorCode.AGENT_CONTEXT_NOT_READY


class AgentRunNotCancellableError(ApiError):
    """Run 已结束，无法取消（文档 6.2 / 6.10）。"""

    code = ErrorCode.AGENT_RUN_NOT_CANCELLABLE


class AgentIdempotencyConflictError(ApiError):
    """同一个 ``client_request_id`` 被复用于不同请求（评审文档「一、#12」）。

    必须报冲突而不是返回上一次的结果：否则用户改完问题重发会拿到上一个问题的答案。
    """

    code = ErrorCode.AGENT_IDEMPOTENCY_CONFLICT


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
