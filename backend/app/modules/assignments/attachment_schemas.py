"""作业附件的请求与响应模型（``docs/api-contract.md`` 8.15）。

与课件上传（第 4 节）保持同一套约定：

- 请求模型只做**结构**校验（未声明字段、显式 ``null``），长度、白名单、
  大小与 sha256 这些业务规则交给服务层判定并返回 ``422 UPLOAD_INVALID``，
  避免它们被框架降级成笼统的 ``VALIDATION_ERROR``；
- 完成确认**没有请求字段**，只接受省略请求体或 ``{}``。
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, model_validator

from app.core.time import UtcTimestamp


class _StrictRequest(BaseModel):
    """请求基类：拒绝未声明字段与显式 ``null``（与课件、报告上传一致）。"""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: object) -> object:
        if isinstance(data, dict):
            null_fields = sorted(str(key) for key, value in data.items() if value is None)
            if null_fields:
                raise ValueError(f"字段不能为 null：{'、'.join(null_fields)}")
        return data


class AttachmentUploadInitRequest(_StrictRequest):
    """初始化附件上传（契约 8.15），字段与课件上传完全一致。"""

    filename: str
    content_type: str
    size: int
    sha256: str


class AttachmentUploadInitResponse(BaseModel):
    """初始化响应：与课件上传同形的三段式凭据。"""

    upload_id: uuid.UUID
    upload_url: str
    #: 固定为 PUT
    method: str
    #: 直传时必须逐字附带的请求头
    headers: dict[str, str]
    #: 预签名 PUT 的到期时间
    expires_at: UtcTimestamp
    #: 完成确认的截止时间
    confirm_deadline_at: UtcTimestamp


class AssignmentAttachmentSchema(BaseModel):
    """作业附件（对课程成员可见，含临时下载地址）。"""

    id: uuid.UUID
    assignment_id: uuid.UUID
    filename: str
    content_type: str
    size: int
    #: 客户端上传时声明的摘要，便于核对文件是否被改动
    sha256: str
    uploaded_by: uuid.UUID
    #: 上传者显示名；上传者已注销时为 null（前端此时不显示上传者）
    uploaded_by_name: str | None = None
    #: 短时有效的预签名 GET 地址
    download_url: str
    #: 上述地址的失效时间
    download_expires_at: UtcTimestamp
    created_at: UtcTimestamp
