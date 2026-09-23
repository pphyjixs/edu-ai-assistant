"""稳定错误码定义。

业务错误码与 ``docs/api-contract.md`` 第 12 节逐条对应，前端可据 ``code``
做稳定分支判断；``HTTP_ERROR`` 系列是框架层补充，用于统一未在契约中单独
列出的协议错误。新增或重命名错误码属于契约变更，必须同步修改文档。
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """机器可读的错误标识。"""

    # ------------------------- 契约业务错误码 -------------------------
    AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"
    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
    #: 注册邮箱已被占用（由数据库唯一约束兜住并发注册后转译）
    AUTH_EMAIL_TAKEN = "AUTH_EMAIL_TAKEN"
    #: 同一规范化邮箱的登录失败次数超过限流阈值
    AUTH_TOO_MANY_ATTEMPTS = "AUTH_TOO_MANY_ATTEMPTS"
    ROLE_FORBIDDEN = "ROLE_FORBIDDEN"
    COURSE_FORBIDDEN = "COURSE_FORBIDDEN"
    #: 课程已归档，不能对其执行修改、加入或重置邀请码等写入操作
    COURSE_ARCHIVED = "COURSE_ARCHIVED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    INVITE_CODE_INVALID = "INVITE_CODE_INVALID"
    UPLOAD_INVALID = "UPLOAD_INVALID"
    RUBRIC_SCORE_MISMATCH = "RUBRIC_SCORE_MISMATCH"
    MATERIAL_NOT_READY = "MATERIAL_NOT_READY"
    #: 资料已解析完成，无需再次解析（重试解析接口，契约 5.3）
    MATERIAL_ALREADY_READY = "MATERIAL_ALREADY_READY"
    ASSIGNMENT_NOT_OPEN = "ASSIGNMENT_NOT_OPEN"
    #: 同一学生对同一任务已有正式提交，不能重复初始化上传（契约 9.2）
    SUBMISSION_ALREADY_EXISTS = "SUBMISSION_ALREADY_EXISTS"
    #: 提交未完成或批改未生成；或已有复核结果时重复触发（契约 9.6 / 9.9）
    SUBMISSION_NOT_READY = "SUBMISSION_NOT_READY"
    GRADE_NOT_REVIEWED = "GRADE_NOT_REVIEWED"
    #: 成绩已发布，不能继续修改复核结果（契约 9.8）
    GRADE_ALREADY_PUBLISHED = "GRADE_ALREADY_PUBLISHED"
    AI_JOB_FAILED = "AI_JOB_FAILED"
    #: 会话在回答生成期间被并发修改，本次发送未写入（问答接口，契约 6.1）
    CHAT_CONFLICT = "CHAT_CONFLICT"
    #: 练习尚未生成成功（发布非 DRAFT、提交未发布练习，契约 7.5 / 7.6）
    PRACTICE_NOT_READY = "PRACTICE_NOT_READY"
    #: 同一学生对同一练习重复提交（契约 7.6）
    PRACTICE_ALREADY_ATTEMPTED = "PRACTICE_ALREADY_ATTEMPTED"
    #: 任务当前状态不可重试（契约 10.2）
    JOB_NOT_RETRYABLE = "JOB_NOT_RETRYABLE"
    #: 同一会话已有未结束的 Agent Run（docs/local-development-agent-backend.md 6.5）
    AGENT_RUN_IN_PROGRESS = "AGENT_RUN_IN_PROGRESS"
    #: 该实体类型或 action 与 context 的组合尚未实现（6.3）
    AGENT_CONTEXT_UNSUPPORTED = "AGENT_CONTEXT_UNSUPPORTED"
    #: 目标资料尚未解析完成，无法注入上下文（6.10）
    AGENT_CONTEXT_NOT_READY = "AGENT_CONTEXT_NOT_READY"
    #: Run 已结束，无法取消（6.2 / 6.10）
    AGENT_RUN_NOT_CANCELLABLE = "AGENT_RUN_NOT_CANCELLABLE"

    # ---------------------------- 框架层错误码 ----------------------------
    #: 请求体或查询参数未通过 Pydantic 校验
    VALIDATION_ERROR = "VALIDATION_ERROR"
    #: HTTP 方法不被该路径支持
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    #: 未预期的服务端异常，响应与日志都不包含堆栈
    INTERNAL_ERROR = "INTERNAL_ERROR"
    #: 依赖未就绪：``/health/ready`` 探测失败，或问答接口的模型配置缺失（契约 6.7）
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


#: 面向用户的默认中文提示，不包含任何内部细节。
DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.AUTH_INVALID_CREDENTIALS: "账号或密码错误",
    ErrorCode.AUTH_TOKEN_EXPIRED: "登录状态已失效，请重新登录",
    ErrorCode.AUTH_EMAIL_TAKEN: "该邮箱已注册，请直接登录",
    ErrorCode.AUTH_TOO_MANY_ATTEMPTS: "登录失败次数过多，请稍后再试",
    ErrorCode.ROLE_FORBIDDEN: "当前角色无权执行该操作",
    ErrorCode.COURSE_FORBIDDEN: "你没有访问该课程的权限",
    ErrorCode.COURSE_ARCHIVED: "课程已归档，不能执行该操作",
    ErrorCode.RESOURCE_NOT_FOUND: "资源不存在或不可见",
    ErrorCode.INVITE_CODE_INVALID: "邀请码无效",
    ErrorCode.UPLOAD_INVALID: "上传未完成，或文件类型与校验信息不符",
    ErrorCode.RUBRIC_SCORE_MISMATCH: "评分项分值合计与任务总分不一致",
    ErrorCode.MATERIAL_NOT_READY: "课程资料尚未解析完成",
    ErrorCode.MATERIAL_ALREADY_READY: "课程资料已解析完成，无需再次解析",
    ErrorCode.ASSIGNMENT_NOT_OPEN: "任务未发布或已关闭",
    ErrorCode.SUBMISSION_ALREADY_EXISTS: "你已经提交过这份实验报告",
    ErrorCode.SUBMISSION_NOT_READY: "提交尚未完成或批改尚未生成，暂时无法执行该操作",
    ErrorCode.GRADE_NOT_REVIEWED: "尚未完成教师复核，不能发布",
    ErrorCode.GRADE_ALREADY_PUBLISHED: "成绩已发布，不能再修改复核结果",
    ErrorCode.AI_JOB_FAILED: "AI 或解析任务执行失败",
    ErrorCode.CHAT_CONFLICT: "会话已被更新，请重新发送",
    ErrorCode.PRACTICE_NOT_READY: "练习尚未生成完成，暂时无法执行该操作",
    ErrorCode.PRACTICE_ALREADY_ATTEMPTED: "你已经提交过这套练习",
    ErrorCode.JOB_NOT_RETRYABLE: "该任务当前不可重试",
    ErrorCode.AGENT_RUN_IN_PROGRESS: "这个会话还有一个进行中的任务，请等它结束",
    ErrorCode.AGENT_CONTEXT_UNSUPPORTED: "当前对象类型暂不支持这个操作",
    ErrorCode.AGENT_CONTEXT_NOT_READY: "目标资料尚未解析完成，暂时无法作为上下文",
    ErrorCode.AGENT_RUN_NOT_CANCELLABLE: "该任务已经结束，无法取消",
    ErrorCode.VALIDATION_ERROR: "请求参数不合法",
    ErrorCode.METHOD_NOT_ALLOWED: "请求方法不被支持",
    ErrorCode.INTERNAL_ERROR: "服务内部错误，请稍后重试",
    ErrorCode.SERVICE_UNAVAILABLE: "服务依赖未就绪",
}


#: 各错误码的推荐 HTTP 状态码。
DEFAULT_STATUS_CODES: dict[ErrorCode, int] = {
    ErrorCode.AUTH_INVALID_CREDENTIALS: 401,
    ErrorCode.AUTH_TOKEN_EXPIRED: 401,
    ErrorCode.AUTH_EMAIL_TAKEN: 409,
    ErrorCode.AUTH_TOO_MANY_ATTEMPTS: 429,
    ErrorCode.ROLE_FORBIDDEN: 403,
    ErrorCode.COURSE_FORBIDDEN: 403,
    ErrorCode.COURSE_ARCHIVED: 409,
    ErrorCode.RESOURCE_NOT_FOUND: 404,
    ErrorCode.INVITE_CODE_INVALID: 422,
    ErrorCode.UPLOAD_INVALID: 422,
    ErrorCode.RUBRIC_SCORE_MISMATCH: 422,
    ErrorCode.MATERIAL_NOT_READY: 409,
    ErrorCode.MATERIAL_ALREADY_READY: 409,
    ErrorCode.ASSIGNMENT_NOT_OPEN: 409,
    ErrorCode.SUBMISSION_ALREADY_EXISTS: 409,
    ErrorCode.SUBMISSION_NOT_READY: 409,
    ErrorCode.GRADE_NOT_REVIEWED: 409,
    ErrorCode.GRADE_ALREADY_PUBLISHED: 409,
    ErrorCode.AI_JOB_FAILED: 502,
    ErrorCode.CHAT_CONFLICT: 409,
    ErrorCode.PRACTICE_NOT_READY: 409,
    ErrorCode.PRACTICE_ALREADY_ATTEMPTED: 409,
    ErrorCode.JOB_NOT_RETRYABLE: 409,
    ErrorCode.AGENT_RUN_IN_PROGRESS: 409,
    ErrorCode.AGENT_CONTEXT_UNSUPPORTED: 422,
    ErrorCode.AGENT_CONTEXT_NOT_READY: 409,
    ErrorCode.AGENT_RUN_NOT_CANCELLABLE: 409,
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.INTERNAL_ERROR: 500,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
}


def default_message(code: ErrorCode) -> str:
    """返回错误码对应的默认提示文案。"""
    return DEFAULT_MESSAGES[code]


def default_status_code(code: ErrorCode) -> int:
    """返回错误码对应的默认 HTTP 状态码。"""
    return DEFAULT_STATUS_CODES[code]
