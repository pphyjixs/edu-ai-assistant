"""Agent 工具的类型契约（开发方案 5.2）。

三条硬约束：

1. **模型只管选工具、传参数**：工具的输入模型一律 ``extra="forbid"``，
   并且**不得**包含 ``user_id`` / ``course_id`` / 角色 / 数据库连接 / 任意 URL，
   这些只能由服务端从已鉴权的 Run 与会话推导（:class:`ToolContext`）。
2. **工具只调用领域 service/repository**：handler 不拼 SQL、不直接写表。
3. **有界输出**：给模型看的 JSON 有单条与累计字节上限，超限按条目边界截断
   并标 ``truncated``，绝不截成无效 JSON（开发方案 5.6）。

这里只放类型；注册、查找与执行在 :mod:`app.modules.agent.tool_registry` 与
:mod:`app.modules.agent.orchestration`。
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.modules.auth.models import UserRole

#: 工具名允许的长度（开发方案 5.2：只允许 ``[a-z0-9_]{1,64}``）
TOOL_NAME_MAX_LENGTH = 64

#: 单个工具返回给模型的 JSON 字节上限
TOOL_RESULT_MAX_BYTES = 12 * 1024

#: 一个 Run 内全部工具结果累计的字节上限
TOOL_RESULTS_TOTAL_MAX_BYTES = 32 * 1024

#: ``agent_run_steps`` 里 ``request_json + response_json`` 的序列化上限
STEP_PAYLOAD_MAX_BYTES = 32 * 1024


class ToolSideEffect(str, enum.Enum):
    """工具是否产生副作用。

    ``WRITE`` 的工具受额外的权限、显式意图与次数限制约束（开发方案 5.4）。
    """

    READ = "READ"
    WRITE = "WRITE"


class StepKind(str, enum.Enum):
    """执行步骤类型（对应 ``agent_run_steps.kind``）。"""

    TOOL_CALL = "TOOL_CALL"
    SKILL_LOAD = "SKILL_LOAD"


class StepStatus(str, enum.Enum):
    """执行步骤状态（对应 ``agent_run_steps.status``）。

    Worker 只在执行完成时落一条**终态**记录（``SUCCEEDED`` / ``FAILED``），
    因此审计上每次调用都有终态。
    """

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ToolErrorCode(str, enum.Enum):
    """稳定的工具错误码；写进 step 并回给模型，禁止携带堆栈或数据库细节。"""

    #: 模型调用了未注册的工具（不做模糊匹配，也不动态 import）
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    #: 参数不是合法 JSON 或未通过输入模型校验
    INVALID_TOOL_ARGUMENTS = "INVALID_TOOL_ARGUMENTS"
    #: 角色/课程权限不足
    FORBIDDEN = "FORBIDDEN"
    #: 目标不存在或对当前用户不可见（跨课程、已删除、未就绪统一走这里）
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    #: 业务状态不允许（例如资料未解析完成）
    INVALID_STATE = "INVALID_STATE"
    #: 本 Run 的写工具次数已用尽
    WRITE_LIMIT_REACHED = "WRITE_LIMIT_REACHED"
    #: 用户本轮输入没有明确表达写操作意图
    WRITE_INTENT_REQUIRED = "WRITE_INTENT_REQUIRED"
    #: ``load_skill`` 找不到该名称
    SKILL_NOT_FOUND = "SKILL_NOT_FOUND"
    #: 已加载 Skill 正文总量达到上限，无法再加载新的 Skill
    SKILL_BUDGET_EXCEEDED = "SKILL_BUDGET_EXCEEDED"
    #: 有界循环超限（轮次/工具次数）
    AGENT_LOOP_LIMIT = "AGENT_LOOP_LIMIT"
    #: 未归类的服务端错误（对外只有稳定码，不含内部细节）
    INTERNAL = "INTERNAL"


class ToolErrorPayload(BaseModel):
    """回给模型的错误说明。``message`` 必须可安全展示。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class AgentArtifactKind(str, enum.Enum):
    """Run 产出的业务结果类型（前端据此渲染可点击卡片）。"""

    PRACTICE_SET = "PRACTICE_SET"


class AgentArtifact(BaseModel):
    """工具产出的业务结果引用（开发方案 7.2）。

    只带路由与状态，不带工具原始参数、课件摘录或内部错误详情。
    """

    model_config = ConfigDict(extra="forbid")

    kind: AgentArtifactKind
    id: uuid.UUID
    job_id: uuid.UUID | None = None
    status: str
    href: str


class ToolEvidence(BaseModel):
    """工具返回的一条可引用证据（开发方案 5.3）。

    形状与 :class:`app.modules.agent.context.ContextBlock` 对齐，但不带 ``ref``——
    编号由 Evidence Ledger 统一分配，保证模型引用的编号在整个 Run 内唯一且稳定。
    """

    model_config = ConfigDict(extra="forbid")

    source_type: str
    source_id: uuid.UUID
    label: str
    text: str
    material_id: uuid.UUID | None = None
    chunk_id: uuid.UUID | None = None
    location_start: int | None = None
    location_end: int | None = None
    section_title: str | None = None
    material_name: str | None = None
    source_location_type: str | None = None
    #: 能否支撑一次"有依据"的回答（资料片段/章节大纲/作业要求都是可信依据）
    groundable: bool = True
    #: 能否成为用户可见引用（``MATERIAL`` / ``ASSIGNMENT``）；``None`` 表示只进提示词
    display_kind: str | None = None


class ToolResult(BaseModel):
    """一次工具执行的结果。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[ToolEvidence] = Field(default_factory=list)
    artifacts: list[AgentArtifact] = Field(default_factory=list)
    error: ToolErrorPayload | None = None
    #: ``load_skill`` 成功加载的 Skill 名称（由 orchestrator 合并进已加载集合）
    loaded_skills: list[str] = Field(default_factory=list)
    #: 给模型的 JSON 是否被按条目边界截断
    truncated: bool = False

    @classmethod
    def failure(
        cls, code: ToolErrorCode, message: str
    ) -> ToolResult:
        """构造一个失败结果（``ok=False`` + 稳定错误码）。"""
        return cls(ok=False, error=ToolErrorPayload(code=code.value, message=message))


@dataclass(frozen=True, slots=True)
class ToolContext:
    """一次 Run 内所有工具共享的服务端事实（开发方案 5.2）。

    **只由服务端依据 Run 与已鉴权会话构造**；模型无法通过参数覆盖其中任何字段。
    """

    run_id: uuid.UUID
    session_id: uuid.UUID
    course_id: uuid.UUID
    user_id: uuid.UUID
    user_role: UserRole
    #: 当前用户是否为课程创建教师（写工具与作业可见性的唯一依据）
    is_course_teacher: bool
    session_factory: async_sessionmaker[AsyncSession]
    settings: Settings
    #: 本轮用户输入：写工具的「显式意图」判定使用它，而不是历史消息
    question: str = ""


ToolHandler = Callable[[ToolContext, BaseModel], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """一个可调用工具的完整定义（开发方案 5.2）。"""

    name: str
    description: str
    input_model: type[BaseModel]
    side_effect: ToolSideEffect
    allowed_roles: frozenset[UserRole]
    handler: ToolHandler

    def json_schema(self) -> dict[str, Any]:
        """Chat Completions ``tools[].function.parameters`` 需要的 JSON Schema。"""
        schema = self.input_model.model_json_schema()
        # 输入模型都是 extra="forbid"，Pydantic 会生成 additionalProperties: false；
        # 这里兜底一次，保证描述与实现一致。
        schema.setdefault("additionalProperties", False)
        schema.pop("title", None)
        return schema

    def function_schema(self) -> dict[str, Any]:
        """OpenAI Chat Completions 原生的 function tool 定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.json_schema(),
            },
        }


#: 写工具要求用户本轮输入明确表达创建意图时的动词。
#:
#: 这是**动作守卫**而不是路由：选择哪个工具仍然完全由模型决定
#: （开发方案第 13 节禁止用关键词做后端硬路由）。守卫只回答
#: 「用户是不是真的要求创建」，避免把历史里出现过的"出题"当成这次的新指令。
WRITE_INTENT_PHRASES: tuple[str, ...] = (
    "生成",
    "创建",
    "出题",
    "出几道",
    "出题",
    "来几道",
    "来道",
    "做几道",
    "练习",
    "测验",
    "小测",
    "题目",
    "题库",
)


def has_write_intent(question: str) -> bool:
    """用户本轮输入是否明确表达「创建/生成」意图。

    只用于写工具的执行守卫（开发方案 5.4 第 2 条）；
    不得用于决定调用哪个工具。
    """
    text = question.strip()
    if not text:
        return False
    return any(phrase in text for phrase in WRITE_INTENT_PHRASES)


__all__ = [
    "AgentArtifact",
    "AgentArtifactKind",
    "STEP_PAYLOAD_MAX_BYTES",
    "StepKind",
    "StepStatus",
    "TOOL_NAME_MAX_LENGTH",
    "TOOL_RESULT_MAX_BYTES",
    "TOOL_RESULTS_TOTAL_MAX_BYTES",
    "ToolContext",
    "ToolErrorCode",
    "ToolErrorPayload",
    "ToolEvidence",
    "ToolHandler",
    "ToolResult",
    "ToolSideEffect",
    "ToolSpec",
    "has_write_intent",
]
