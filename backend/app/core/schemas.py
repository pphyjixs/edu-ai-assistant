"""跨模块共用的响应模型。

目前只有统一错误结构（``docs/api-contract.md`` 第 1 节）。把它声明成 Pydantic 模型
是为了让它出现在导出的 OpenAPI 里：前端据此生成错误类型，不必手写一份。

``code`` 使用 :class:`app.core.error_codes.ErrorCode` 枚举，生成的 TypeScript 会得到
字符串字面量联合类型，前端 switch 时能被穷尽检查覆盖。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.error_codes import ErrorCode

#: OpenAPI 里展示的错误示例，与契约第 1 节保持一致
_ERROR_EXAMPLE = {
    "error": {
        "code": ErrorCode.COURSE_FORBIDDEN.value,
        "message": "你没有访问该课程的权限",
        "details": {},
        "request_id": "3f6a1c2e9d8b4a0f9e7c5b3a1d2f4e60",
    }
}


class ErrorBody(BaseModel):
    """错误体的内容部分。"""

    #: 稳定错误码，前端按此分支，不解析 message
    code: ErrorCode
    #: 面向终端用户的中文提示，不含内部细节
    message: str
    #: 可安全展示的结构化补充信息，无补充时为空对象
    details: dict[str, Any] = Field(default_factory=dict)
    #: 与响应头 X-Request-ID 一致，便于用户报障时定位日志
    request_id: str


class ErrorResponse(BaseModel):
    """所有非 2xx 响应的统一结构。"""

    model_config = ConfigDict(json_schema_extra={"example": _ERROR_EXAMPLE})

    error: ErrorBody
