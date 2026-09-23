"""Materials 模块请求与响应模型。

字段与状态码以 ``docs/api-contract.md`` 第 4 节为准。请求模型只做**结构**校验
（字段存在性、类型、未声明字段、显式 ``null``），校验失败由框架转成
``422 VALIDATION_ERROR``；文件类型、MIME、大小、sha256 这些**业务规则**
在 :mod:`app.modules.materials.service` 里判定，抛 ``422 UPLOAD_INVALID``
并带稳定的 ``details.reason``（契约 4.8 划定的分界）。
"""

from __future__ import annotations

import enum
import uuid

from pydantic import BaseModel, ConfigDict, model_validator

from app.core.time import UtcTimestamp
from app.modules.jobs.schemas import JobStatus
from app.modules.materials.models import MaterialStatus

#: 扩展名（不含点，比较时忽略大小写）→ 规范 MIME（契约 4.2）
CANONICAL_CONTENT_TYPE_BY_EXTENSION: dict[str, str] = {
    ".pdf": "application/pdf",
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
}

#: 规范 MIME → 规范扩展名。对象键只使用这里的扩展名，不取用户文件名的写法
CANONICAL_EXTENSION_BY_CONTENT_TYPE: dict[str, str] = {
    content_type: extension
    for extension, content_type in CANONICAL_CONTENT_TYPE_BY_EXTENSION.items()
}

#: 支持的扩展名集合
SUPPORTED_EXTENSIONS = frozenset(CANONICAL_CONTENT_TYPE_BY_EXTENSION)

#: sha256 的十六进制长度
SHA256_HEX_LENGTH = 64


class _StrictUploadRequest(BaseModel):
    """上传请求基类：拒绝未声明字段与显式 ``null``（契约 4.3）。"""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: object) -> object:
        if isinstance(data, dict):
            null_fields = sorted(str(key) for key, value in data.items() if value is None)
            if null_fields:
                raise ValueError(f"字段不能为 null：{'、'.join(null_fields)}")
        return data


class MaterialUploadInitRequest(_StrictUploadRequest):
    """初始化上传请求（契约 4.3）。

    只声明类型与必填；长度、白名单、范围等业务规则由 service 判定并返回
    ``UPLOAD_INVALID``，避免它们被框架统一降级成 ``VALIDATION_ERROR``。
    """

    filename: str
    content_type: str
    size: int
    sha256: str


class MaterialUploadCompleteRequest(_StrictUploadRequest):
    """完成上传请求（契约 4.5）：**没有请求字段**。

    只接受省略请求体或空对象 ``{}``；带任何未声明字段时返回
    ``422 VALIDATION_ERROR``（与全项目"拒绝未声明字段"的约定一致）。
    """


class MaterialUploadInitResponse(BaseModel):
    """初始化上传响应（契约 4.3）。"""

    upload_id: uuid.UUID
    #: 预签名上传地址
    upload_url: str
    #: 固定为 PUT
    method: str
    #: 直传时必须逐字附带的请求头
    headers: dict[str, str]
    #: **PUT 地址的到期时间**（初始化 + 10 分钟）
    expires_at: UtcTimestamp
    #: 完成确认的截止时间（初始化 + 24 小时）
    confirm_deadline_at: UtcTimestamp


class MaterialDetail(BaseModel):
    """资料详情（契约 4.7）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    course_id: uuid.UUID
    filename: str
    content_type: str
    size: int
    status: MaterialStatus
    uploaded_by: uuid.UUID
    #: 失败原因的安全摘要；非 FAILED 时为 null
    error_message: str | None
    #: 解析失败的阶段码；非 FAILED 时为 null。前端据此说明「失败在读取文件 /
    #: 文本提取 / 大纲生成」哪一步，而不是只给一句笼统的失败（评审文档 #4.8）。
    failure_stage: str | None = None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


class MaterialUploadCompleteResponse(BaseModel):
    """完成上传响应（契约 4.5）：202 返回同一份资料与解析任务。"""

    material: MaterialDetail
    job: JobStatus


class MaterialSectionSourceType(str, enum.Enum):
    """章节定位的来源类型（契约 5.4），由资料的 MIME 推导。

    定位单位：PDF 页码、PPTX 幻灯片号、DOCX 段落序号，均从 1 开始。
    """

    PDF_PAGE = "PDF_PAGE"
    PPTX_SLIDE = "PPTX_SLIDE"
    DOCX_PARAGRAPH = "DOCX_PARAGRAPH"


#: 规范 MIME → 来源类型
SOURCE_TYPE_BY_CONTENT_TYPE: dict[str, MaterialSectionSourceType] = {
    "application/pdf": MaterialSectionSourceType.PDF_PAGE,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
        MaterialSectionSourceType.PPTX_SLIDE
    ),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        MaterialSectionSourceType.DOCX_PARAGRAPH
    ),
}


def source_type_for_content_type(content_type: str) -> MaterialSectionSourceType:
    """由规范 MIME 推导章节来源类型；未知类型按 PDF 页处理。"""
    return SOURCE_TYPE_BY_CONTENT_TYPE.get(
        content_type, MaterialSectionSourceType.PDF_PAGE
    )


class MaterialKnowledgePoint(BaseModel):
    """知识点（契约 5.4）：含可核对的原文摘录与定位。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    #: 章节内从 1 开始的顺序号
    order: int
    title: str
    description: str
    #: 解析时从资料对应位置抽取的原文摘录
    quote: str
    location_start: int
    location_end: int


class MaterialSection(BaseModel):
    """章节（契约 5.4）：按 order 升序，含知识点列表。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    #: 从 1 开始的顺序号
    order: int
    title: str
    source_type: MaterialSectionSourceType
    location_start: int
    location_end: int
    knowledge_points: list[MaterialKnowledgePoint]


class MaterialOutline(BaseModel):
    """大纲查询响应（契约 5.4）。"""

    material_id: uuid.UUID
    sections: list[MaterialSection]
