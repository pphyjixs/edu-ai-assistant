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

import hashlib
import json
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
    """可选的调用选项，包括本次手动选择的个人 Skill。"""

    model_config = ConfigDict(extra="forbid")

    output_language: StrictStr | None = Field(
        default=None, max_length=OUTPUT_LANGUAGE_MAX_LENGTH
    )
    selected_skill_ids: list[uuid.UUID] = Field(default_factory=list, max_length=3)
    selected_skill_names: list[StrictStr] = Field(default_factory=list, max_length=3)


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

    ``steps`` 与 ``artifacts``（开发方案 7.2）是**带默认值的新字段**，因此旧前端
    继续解析时不会因为缺少字段而报错；它们只暴露名称、状态与稳定错误码，
    **不返回**工具原始参数、课件摘录、Skill 正文或内部错误详情。
    """

    id: uuid.UUID
    session_id: uuid.UUID
    action: AgentRunAction
    status: JobStatusValue
    progress: int
    input_message_id: uuid.UUID
    output_message_id: uuid.UUID | None
    error: str | None
    #: 失败阶段码（``DOWNLOAD`` / ``OUTLINE_GENERATION`` / ``TOOL_CALL`` …）；成功时为 null
    failure_stage: str | None = None
    #: 本次回答的依据充分度：FULL（完整）/ PARTIAL（只有部分依据）/ NONE（无依据）
    evidence_level: str | None = None
    created_at: UtcTimestamp
    started_at: UtcTimestamp | None
    finished_at: UtcTimestamp | None
    #: 本次实际注入的来源（不含原文快照），便于前端展示与排错
    sources: list[AgentRunSourceSchema] = Field(default_factory=list)
    #: 本次执行的步骤：工具调用与 Skill 加载，前端据此显示"正在检索课程资料"等状态
    steps: list[AgentRunStepSchema] = Field(default_factory=list)
    #: 本次 Run 产出的业务结果（例如创建好的练习），前端渲染成可点击卡片
    artifacts: list[AgentRunArtifactSchema] = Field(default_factory=list)


class AgentActiveRunSchema(BaseModel):
    """``GET /chat-sessions/{session_id}/active-run`` 的响应。

    刻意**不用 404** 表示「没有进行中的 Run」：页面打开时这是常规情况，
    用 200 + ``run: null`` 可以让前端一条路径处理，也避免把正常状态记进
    错误监控（评审文档「一、#11」）。
    """

    run: AgentRunSchema | None = None


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


class AgentRunStepSchema(BaseModel):
    """执行步骤的展示视图（开发方案 7.2）。

    只暴露「第几步、哪一类、工具/Skill 名、是否成功、稳定错误码」，
    不暴露参数与结果明细。
    """

    order: int
    kind: str
    name: str
    status: str
    error_code: str | None = None


class AgentRunArtifactSchema(BaseModel):
    """Run 产出的业务结果（开发方案 7.2）。"""

    kind: str
    id: uuid.UUID
    job_id: uuid.UUID | None = None
    status: str
    href: str


# ---------------------------- 模型输出校验 ---------------------------- #


class GeneratedAgentCitation(BaseModel):
    """模型给出的引用：用提示词里的来源编号 ``ref`` 指向注入的上下文块。"""

    model_config = ConfigDict(extra="forbid")

    ref: StrictStr
    quote: StrictStr


class GeneratedAgentAnswer(BaseModel):
    """模型输出的结构约束；语义校验在 generation_ai 中完成。

    ``evidence_level`` 与 ``missing_information`` 是**可选**的：模型偶尔会漏，
    服务端会自行兜底计算依据等级，不会因为少一个字段就整段作废
    （评审文档「一、#1.4」：部分命中时要保留已确认的部分）。
    """

    model_config = ConfigDict(extra="forbid")

    answer: StrictStr
    citations: list[GeneratedAgentCitation] = Field(default_factory=list)
    #: 模型自评的依据充分度；服务端会结合引用校验结果做最终判定
    evidence_level: Literal["FULL", "PARTIAL", "NONE"] | None = None
    #: 模型认为资料无法覆盖的部分，用于向用户说明缺口
    missing_information: list[StrictStr] = Field(default_factory=list)


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

#: ``action`` ↔ ``context`` 允许矩阵（评审文档「一、#8」）。
#:
#: ``None`` 表示"没有 context 字段"，即课程范围——它只对会话式的动作有意义。
#: 其余组合一律在创建 Run 时就返回 ``422 AGENT_CONTEXT_UNSUPPORTED``：
#: 让注定无意义的请求当场失败，好过生成一段跑题的回答。
#: ``CHECK_SUBMISSION`` 只允许 ``SUBMISSION``；该上下文尚未实现，
#: 因此当前会稳定返回"未实现"而不是悄悄退化成课程问答。
ACTION_CONTEXT_MATRIX: dict[AgentRunAction, frozenset[AgentEntityType | None]] = {
    AgentRunAction.ASK: frozenset(
        {
            None,
            AgentEntityType.COURSE,
            AgentEntityType.MATERIAL,
            AgentEntityType.MATERIAL_SECTION,
            AgentEntityType.ASSIGNMENT,
        }
    ),
    AgentRunAction.SUMMARIZE_CONTEXT: frozenset(
        {
            None,
            AgentEntityType.COURSE,
            AgentEntityType.MATERIAL,
            AgentEntityType.MATERIAL_SECTION,
            AgentEntityType.ASSIGNMENT,
        }
    ),
    AgentRunAction.BREAK_DOWN_ASSIGNMENT: frozenset({AgentEntityType.ASSIGNMENT}),
    AgentRunAction.CHECK_SUBMISSION: frozenset({AgentEntityType.SUBMISSION}),
}


def allowed_contexts(action: AgentRunAction) -> str:
    """把矩阵里允许的上下文渲染成可读文案，用于 422 的提示。"""
    names = sorted(
        "COURSE" if item is None else item.value
        for item in ACTION_CONTEXT_MATRIX[action]
    )
    return "、".join(names)


def request_fingerprint(request: AgentRunCreateRequest) -> str:
    """``client_request_id`` 对应的请求指纹（评审文档「一、#12」）。

    同一个幂等键复用于**不同**请求时，服务端必须报冲突而不是把上一次的结果
    当成这一次的答案，否则用户改完问题重发会拿到与问题无关的旧回答。
    指纹只包含语义字段，不包含 ``client_request_id`` 本身。
    """
    context = request.context
    canonical = {
        "input": request.input,
        "action": request.action.value,
        "entity_type": context.entity_type.value if context else None,
        "entity_id": str(context.entity_id) if context and context.entity_id else None,
        "section_id": str(context.section_id) if context and context.section_id else None,
        "selected_text": context.selected_text if context else None,
        "options": request.options.model_dump(mode="json", exclude_none=True) if request.options else {},
    }
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

ActionLiteral = Literal[
    "ASK",
    "SUMMARIZE_CONTEXT",
    "BREAK_DOWN_ASSIGNMENT",
    "CHECK_SUBMISSION",
]
