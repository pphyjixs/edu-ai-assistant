"""Chat 模块的请求/响应模型与模型输出校验模型。

``docs/api-contract.md`` 第 6 节固定了四个接口的形状：

- 对外：``ChatSession``（6.6）、``ChatMessage``（6.6）、``Citation``（6.6）、
  ``ChatQuestionRequest``（6.5）；列表复用第 1 节的 ``Page`` 包装。
- 对内：:class:`GeneratedAnswer` 是**模型输出**的校验形状——服务端只接受
  本次检索到的片段 ID，并核对摘录确实出现在片段原文中，任何越界都视为
  无效输出（``502 AI_JOB_FAILED``）。

请求体一律拒绝未声明字段（``extra="forbid"``）与显式 ``null``，与契约
6.5 / 6.7 的 ``422 VALIDATION_ERROR`` 对齐。
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.time import UtcTimestamp

#: 问题正文长度上限（契约 6.5：去除首尾空白后 1–2000 字符）
QUESTION_MAX_LENGTH = 2000

#: 无依据时的固定回答文案（契约 6.1）
NO_EVIDENCE_ANSWER = "课程资料中未找到依据"


class ChatQuestionRequest(BaseModel):
    """发送问题（契约 6.5）：只有 ``content`` 一个字段。"""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=QUESTION_MAX_LENGTH)

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        """去除首尾空白后必须非空且不超长；不自动改写大小写。"""
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("问题内容不能为空白")
        if len(trimmed) > QUESTION_MAX_LENGTH:
            raise ValueError(f"问题内容不能超过 {QUESTION_MAX_LENGTH} 个字符")
        return value


class ChatSessionSchema(BaseModel):
    """会话（契约 6.6）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    course_id: uuid.UUID
    created_at: UtcTimestamp
    last_message_at: UtcTimestamp


# 创建会话（契约 6.2）没有请求字段：请求体在路由层手工校验，
# 以便区分「省略请求体」与「显式 null」（后者必须 422）。


class Citation(BaseModel):
    """引用（契约 6.6）：取自命中片段，带可核对的原文摘录。"""

    model_config = ConfigDict(from_attributes=True)

    material_id: uuid.UUID
    material_name: str
    section_id: uuid.UUID | None
    section_title: str | None
    source_type: str
    location_start: int
    location_end: int
    #: 仅 PDF 有值（等于 location_start）；PPTX 与 DOCX 为 null
    page: int | None
    quote: str


class ChatMessageSchema(BaseModel):
    """消息（契约 6.6）。``citations`` 由服务端按消息聚合填充。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    grounded: bool | None
    citations: list[Citation] = Field(default_factory=list)
    created_at: UtcTimestamp


# --------------------------------------------------------------------------- #
# 模型输出（内部）：只允许引用本次检索到的片段
# --------------------------------------------------------------------------- #
class GeneratedCitation(BaseModel):
    """模型给出的单条引用：片段 ID + 原文摘录。"""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str = Field(min_length=1, max_length=64)
    quote: str = Field(min_length=1, max_length=1000)


class GeneratedAnswer(BaseModel):
    """模型输出的回答（Pydantic 校验的形状）。"""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=8000)
    citations: list[GeneratedCitation] = Field(default_factory=list, max_length=5)
