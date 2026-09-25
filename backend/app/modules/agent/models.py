"""Agent Run 的 ORM 模型（``docs/local-development-agent-backend.md`` 第 6.5 节）。

设计要点：``agent_runs`` **不保存状态**。状态、进度、错误、尝试次数、运行令牌与
租约统一由 ``jobs``（``type=AGENT_RUN`` / ``resource_type=AGENT_RUN``）承担，
Run 的响应由「Run 行 + Job 行」组合而成。这样避免两套状态字段互相竞争——
任务取消、重试与失联回收只需按现有 Job 语义处理。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.core.time import utc_now
from app.db.base import Base
from app.db.types import UtcDateTime

#: 用户附加文本的长度上限（契约 6.3）
SELECTED_TEXT_MAX_LENGTH = 4_000
#: 幂等键长度上限
CLIENT_REQUEST_ID_MAX_LENGTH = 64
#: 请求指纹长度（sha256 十六进制）
REQUEST_FINGERPRINT_MAX_LENGTH = 64
PROMPT_VERSION_MAX_LENGTH = 64
MODEL_NAME_MAX_LENGTH = 120
EVIDENCE_LEVEL_MAX_LENGTH = 8
#: 编排器版本（``agent-tools-v1`` 这类），成功与失败都记录
ORCHESTRATOR_VERSION_MAX_LENGTH = 64
#: 执行步骤的字段长度（开发方案 7.1）
STEP_KIND_MAX_LENGTH = 16
STEP_STATUS_MAX_LENGTH = 16
STEP_CALL_ID_MAX_LENGTH = 128
STEP_NAME_MAX_LENGTH = 64
STEP_ERROR_CODE_MAX_LENGTH = 64


class AgentEvidenceLevel(str, enum.Enum):
    """本次回答的依据充分度（评审文档「二、6」）。

    ``FULL`` 表示问题被资料完整覆盖；``PARTIAL`` 表示只找到部分依据——
    这时**保留正文并说明缺口**，而不是把整段回答抹成"未找到依据"；
    ``NONE`` 才走无依据路径。
    """

    FULL = "FULL"
    PARTIAL = "PARTIAL"
    NONE = "NONE"


class AgentRunAction(str, enum.Enum):
    """Run 的动作（契约 6.3 的 ``action``）。"""

    ASK = "ASK"
    SUMMARIZE_CONTEXT = "SUMMARIZE_CONTEXT"
    BREAK_DOWN_ASSIGNMENT = "BREAK_DOWN_ASSIGNMENT"
    CHECK_SUBMISSION = "CHECK_SUBMISSION"


class AgentEntityType(str, enum.Enum):
    """上下文对象类型（契约 6.3 的 ``entity_type``）。"""

    COURSE = "COURSE"
    MATERIAL = "MATERIAL"
    MATERIAL_SECTION = "MATERIAL_SECTION"
    ASSIGNMENT = "ASSIGNMENT"
    #: 等 Submission 模块实现后开放（契约 6.3 / 6.6）
    SUBMISSION = "SUBMISSION"
    #: 等 Grading 模块实现后开放
    GRADE = "GRADE"


class AgentSourceType(str, enum.Enum):
    """本次 Run 实际注入的来源类型。"""

    COURSE = "COURSE"
    MATERIAL_CHUNK = "MATERIAL_CHUNK"
    MATERIAL_OUTLINE = "MATERIAL_OUTLINE"
    ASSIGNMENT = "ASSIGNMENT"


class AgentRun(Base):
    """一次异步 Agent Run。"""

    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "chat_sessions.id",
            ondelete="CASCADE",
            name="fk_agent_runs_session_id_chat_sessions",
        ),
        nullable=False,
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_agent_runs_user_id_users"),
        nullable=False,
    )

    action: Mapped[AgentRunAction] = mapped_column(
        SAEnum(AgentRunAction, name="agent_run_action", native_enum=True), nullable=False
    )

    #: 本次提问保存下来的用户消息，与 Run 同事务写入
    input_message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "chat_messages.id",
            ondelete="CASCADE",
            name="fk_agent_runs_input_message_id_chat_messages",
        ),
        nullable=False,
    )

    #: 助手消息，Run 成功后才回填
    output_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "chat_messages.id",
            ondelete="SET NULL",
            name="fk_agent_runs_output_message_id_chat_messages",
        ),
        nullable=True,
    )

    #: 上下文对象；为空表示整个课程范围的 ASK
    entity_type: Mapped[AgentEntityType | None] = mapped_column(
        SAEnum(AgentEntityType, name="agent_entity_type", native_enum=True), nullable=True
    )
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    section_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    #: 用户附加文本：只是上下文材料，不是可信系统指令（契约 6.1 第 10 条）
    selected_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: 预留的调用选项（如 output_language），第一版不解释未知键
    options: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    #: 幂等键：同一用户重复提交返回同一个 Run（契约 6.3）
    client_request_id: Mapped[str] = mapped_column(
        String(CLIENT_REQUEST_ID_MAX_LENGTH), nullable=False
    )

    #: 幂等键对应的**请求指纹**（action + context + input + options 的 sha256）。
    #: 同一个 ``client_request_id`` 复用了不同请求体时，服务端返回 409，
    #: 而不是把上一次的结果当成这一次的答案（评审文档「一、#12」）。
    request_fingerprint: Mapped[str | None] = mapped_column(
        String(REQUEST_FINGERPRINT_MAX_LENGTH), nullable=True
    )

    #: 本次回答的依据充分度（``AgentEvidenceLevel``）
    evidence_level: Mapped[str | None] = mapped_column(
        String(EVIDENCE_LEVEL_MAX_LENGTH), nullable=True
    )

    #: 实际使用的提示词版本与模型名，便于回溯是哪个版本产生的回答
    prompt_version: Mapped[str | None] = mapped_column(
        String(PROMPT_VERSION_MAX_LENGTH), nullable=True
    )
    model: Mapped[str | None] = mapped_column(String(MODEL_NAME_MAX_LENGTH), nullable=True)

    #: 本次使用的编排器版本（例如 ``agent-tools-v1``）；成功与失败都记录
    #: （开发方案 7.1）。老数据为 NULL，表示"工具循环之前"的执行方式。
    orchestrator_version: Mapped[str | None] = mapped_column(
        String(ORCHESTRATOR_VERSION_MAX_LENGTH), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "user_id", "client_request_id", name="uq_agent_runs_user_client_request_id"
        ),
        Index("ix_agent_runs_session_created_id", "session_id", "created_at", "id"),
    )


class AgentRunSource(Base):
    """Run 实际注入的来源快照（可核对、可审计）。"""

    __tablename__ = "agent_run_sources"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "agent_runs.id", ondelete="CASCADE", name="fk_agent_run_sources_run_id_agent_runs"
        ),
        nullable=False,
    )

    order: Mapped[int] = mapped_column(Integer, nullable=False)

    source_type: Mapped[AgentSourceType] = mapped_column(
        SAEnum(AgentSourceType, name="agent_source_type", native_enum=True), nullable=False
    )

    #: 来源自身的 ID（片段 id / 章节 id / 作业 id / 课程 id）
    source_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    material_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "materials.id",
            ondelete="SET NULL",
            name="fk_agent_run_sources_material_id_materials",
        ),
        nullable=True,
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "material_chunks.id",
            ondelete="SET NULL",
            name="fk_agent_run_sources_chunk_id_material_chunks",
        ),
        nullable=True,
    )
    location_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: 人类可读的来源标签，供前端展示与排错
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 进提示词的受控文本快照（已按预算截断），用于事后核对模型看到了什么
    snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_agent_run_sources_run_order", "run_id", "order"),
    )


class AgentRunStep(Base):
    """Run 的执行步骤审计（开发方案 7.1）。

    每次工具调用与每次 Skill 加载都留一条**终态**记录（``SUCCEEDED`` / ``FAILED``），
    因此审计上「每次调用都有 step 终态」。

    两条唯一约束各司其职：

    - ``UNIQUE(run_id, step_order)``：步骤在 Run 内从 1 连续递增；
    - ``UNIQUE(run_id, call_id)``：Worker 重试时的**幂等防线**——
      同一个模型 ``tool_call_id`` 只执行一次，重放直接返回已有结果。

    ``request_json`` / ``response_json`` 都只存**已校验、已脱敏、有界**的内容：
    不存课件全文、不存 Skill 正文、不存数据库细节或堆栈。
    """

    __tablename__ = "agent_run_steps"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "agent_runs.id", ondelete="CASCADE", name="fk_agent_run_steps_run_id_agent_runs"
        ),
        nullable=False,
    )

    #: Run 内从 1 连续递增
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)

    #: ``TOOL_CALL`` / ``SKILL_LOAD``
    kind: Mapped[str] = mapped_column(String(STEP_KIND_MAX_LENGTH), nullable=False)

    #: 模型的 tool call id；Skill 加载使用固定的 ``skill:<name>``
    call_id: Mapped[str] = mapped_column(
        String(STEP_CALL_ID_MAX_LENGTH), nullable=False
    )

    #: 工具名或 Skill 名
    name: Mapped[str] = mapped_column(String(STEP_NAME_MAX_LENGTH), nullable=False)

    #: ``RUNNING`` / ``SUCCEEDED`` / ``FAILED``
    status: Mapped[str] = mapped_column(String(STEP_STATUS_MAX_LENGTH), nullable=False)

    #: 已校验和脱敏后的参数
    request_json: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    #: 有界摘要、artifact 与错误码；不存课件全文或 Skill 正文
    response_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    #: 稳定错误码；成功时为 NULL
    error_code: Mapped[str | None] = mapped_column(
        String(STEP_ERROR_CODE_MAX_LENGTH), nullable=True
    )

    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("run_id", "step_order", name="uq_agent_run_steps_run_order"),
        UniqueConstraint("run_id", "call_id", name="uq_agent_run_steps_run_call"),
        CheckConstraint(
            "jsonb_typeof(request_json) = 'object'",
            name="ck_agent_run_steps_request_object",
        ),
        Index("ix_agent_run_steps_run_order", "run_id", "step_order"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<AgentRunStep run={self.run_id} order={self.step_order} name={self.name}>"
