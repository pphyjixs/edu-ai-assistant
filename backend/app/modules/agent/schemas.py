"""Agent 模块的请求/响应模型与模型输出校验
（``docs/local-development-agent-backend.md`` 第 6.3 / 6.4 节）。

请求体一律拒绝未声明字段（``extra="forbid"``）；顶层字段拒绝显式 ``null``，
但 ``context`` **内部**的 ``section_id`` / ``selected_text`` 按文档示例允许为
``null``——它们是"没有这个信息"，不是"字段非法"。

严格类型：``input`` 与 ``client_request_id`` 只接受 JSON 字符串，
``entity_id`` / ``section_id`` 只接受 UUID 字符串。

Run 的状态不在这里定义——它来自 Job（见 :mod:`app.modules.agent.models` 的说明）。
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
)

from app.core.time import UtcTimestamp
from app.modules.agent.models import (
    CLIENT_REQUEST_ID_MAX_LENGTH,
    SELECTED_TEXT_MAX_LENGTH,
    AgentEntityType,
    AgentRunAction,
)
from app.modules.jobs.models import JobStatusValue

#: ``input`` 去空白后的长度边界（契约 6.3）
INPUT_MIN_LENGTH = 1
INPUT_MAX_LENGTH = 2_000
#: ``options.output_language`` 的长度上限
OUTPUT_LANGUAGE_MAX_LENGTH = 35


class _StrictRequest(BaseModel):
    """拒绝未声明字段的请求基类。

    刻意**不**拒绝"显式 null"：``context`` / ``options`` 在 OpenAPI 里声明为可空
    （``anyOf: [X, null]``），客户端据契约生成后会带上这两个字段，因此运行时必须
    接受 ``null`` 并等同于"不传"，否则契约与实现不一致。
    真正必填的字段由严格类型兜住：``StrictStr`` 与枚举都不接受 ``null``。
    """

    model_config = ConfigDict(extra="forbid")


class AgentRunOptions(BaseModel):
    """可选的调用选项。第一版只认 ``output_language``，其余字段一律拒绝。"""

    model_config = ConfigDict(extra="forbid")

    output_language: StrictStr | None = Field(
        default=None, max_length=OUTPUT_LANGUAGE_MAX_LENGTH
    )


class AgentRunRequestContext(BaseModel):
    """上下文对象标识（契约 6.3）。

    这里**不校验** ``entity_id`` 是否必填：是否必填取决于 ``entity_type``，
    由服务层按 ``entity_type`` 逐类判断，避免把业务规则散落在两个地方。
    """

    model_config = ConfigDict(extra="forbid")

    entity_type: AgentEntityType
    entity_id: uuid.UUID | None = None
    section_id: uuid.UUID | None = None
    selected_text: StrictStr | None = Field(
        default=None, max_length=SELECTED_TEXT_MAX_LENGTH
    )


class AgentRunCreateRequest(_StrictRequest):
    """创建 Run 的请求（契约 6.3）。"""

    input: StrictStr
    action: AgentRunAction
    context: AgentRunRequestContext | None = None
    options: AgentRunOptions | None = None
    client_request_id: StrictStr = Field(max_length=CLIENT_REQUEST_ID_MAX_LENGTH)

    @field_validator("input")
    @classmethod
    def _validate_input(cls, value: str) -> str:
        stripped = value.strip()
        if len(stripped) < INPUT_MIN_LENGTH:
            raise ValueError("input 不能为空或全为空白")
        if len(stripped) > INPUT_MAX_LENGTH:
            raise ValueError(f"input 最长 {INPUT_MAX_LENGTH} 个字符")
        return stripped

    @field_validator("client_request_id")
    @classmethod
    def _validate_client_request_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("client_request_id 不能为空")
        return stripped


class AgentRunSchema(BaseModel):
    """Run 的响应（契约 6.4）。

    ``status`` / ``progress`` / ``error`` 来自关联的 Job，而不是 ``agent_runs``。
    """

    id: uuid.UUID
    session_id: uuid.UUID
    action: AgentRunAction
    status: JobStatusValue
    progress: int
    input_message_id: uuid.UUID
    output_message_id: uuid.UUID | None
    error: str | None
    created_at: UtcTimestamp
    started_at: UtcTimestamp | None
    finished_at: UtcTimestamp | None
    #: 本次实际注入的来源（不含原文快照），便于前端展示与排错
    sources: list[AgentRunSourceSchema] = Field(default_factory=list)


class AgentRunSourceSchema(BaseModel):
    """来源快照的展示视图（不含 ``snapshot`` 原文）。"""

    order: int
    source_type: str
    source_id: uuid.UUID
    material_id: uuid.UUID | None
    chunk_id: uuid.UUID | None
    location_start: int | None
    location_end: int | None
    label: str


# ---------------------------- 模型输出校验 ---------------------------- #


class GeneratedAgentCitation(BaseModel):
    """模型给出的引用：用提示词里的来源编号 ``ref`` 指向注入的上下文块。"""

    model_config = ConfigDict(extra="forbid")

    ref: StrictStr
    quote: StrictStr


class GeneratedAgentAnswer(BaseModel):
    """模型输出的结构约束；语义校验在 generation_ai 中完成。"""

    model_config = ConfigDict(extra="forbid")

    answer: StrictStr
    citations: list[GeneratedAgentCitation] = Field(default_factory=list)


# ---------------------------- 内部领域枚举 ---------------------------- #

#: Run 的终态；与 Job 的终态一致
TERMINAL_STATUSES: frozenset[JobStatusValue] = frozenset(
    {
        JobStatusValue.SUCCEEDED,
        JobStatusValue.FAILED,
        JobStatusValue.CANCELLED,
    }
)

#: 已实现上下文解析的实体类型；其余按 AGENT_CONTEXT_UNSUPPORTED 处理
SUPPORTED_ENTITY_TYPES: frozenset[AgentEntityType] = frozenset(
    {
        AgentEntityType.COURSE,
        AgentEntityType.MATERIAL,
        AgentEntityType.MATERIAL_SECTION,
        AgentEntityType.ASSIGNMENT,
    }
)

#: 需要 ``entity_id`` 的实体类型
ENTITY_TYPES_REQUIRING_ID: frozenset[AgentEntityType] = frozenset(
    {
        AgentEntityType.MATERIAL,
        AgentEntityType.MATERIAL_SECTION,
        AgentEntityType.ASSIGNMENT,
        AgentEntityType.SUBMISSION,
        AgentEntityType.GRADE,
    }
)

#: 需要 ``section_id`` 的实体类型
ENTITY_TYPES_REQUIRING_SECTION: frozenset[AgentEntityType] = frozenset(
    {AgentEntityType.MATERIAL_SECTION}
)

ActionLiteral = Literal[
    "ASK",
    "SUMMARIZE_CONTEXT",
    "BREAK_DOWN_ASSIGNMENT",
    "CHECK_SUBMISSION",
]
