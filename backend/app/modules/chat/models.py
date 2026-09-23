"""Chat 模块 ORM 模型（``docs/api-contract.md`` 第 6 节：课程问答）。

四张表承载会话、消息、引用与生成尝试：

- ``chat_sessions``：会话。所有者（``user_id``）创建、归属课程；``version``
  是**服务端内部**的乐观并发控制版本，不对外暴露（契约 6.1：同一会话的
  并发发送只允许一个成功，其余返回 ``409 CHAT_CONFLICT``）。
- ``chat_messages``：消息。一问一答在**同一事务**中写入，``grounded`` 只在
  助手消息上有值（用户消息恒为 ``NULL``）。
- ``chat_message_citations``：引用快照。冗余保存资料名、章节标题与原文摘录，
  使历史对话在资料被删除/重新解析后仍可完整回读；因此**不设外键**到
  ``materials`` / ``material_sections``（保留写入时刻的快照）。
- ``chat_generation_attempts``：生成尝试记录。保存模型名称、提示词版本、
  耗时与**安全失败摘要**（不含提示词、课件原文或模型地址）。

时间列沿用 :class:`app.db.types.UtcDateTime`，读写两端均为 UTC。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.core.time import utc_now
from app.db.base import Base
from app.db.types import UtcDateTime

#: 资料名（引用快照）列长度，与 ``materials.filename`` 对齐
MATERIAL_NAME_MAX_LENGTH = 255

#: 失败摘要列长度：只保存可安全展示的原因，不保存堆栈或原文
ATTEMPT_ERROR_MAX_LENGTH = 500

#: 提示词版本列长度
PROMPT_VERSION_MAX_LENGTH = 32

#: 模型名称列长度
MODEL_NAME_MAX_LENGTH = 128

#: 引用来源类别列的取值长度
SOURCE_KIND_MAX_LENGTH = 16

#: 非资料引用的 ``source_type`` 占位值（资料引用用 PDF_PAGE / PPTX_SLIDE / DOCX_PARAGRAPH）
NON_MATERIAL_SOURCE_TYPE = "NONE"


class CitationSourceKind(str, enum.Enum):
    """引用的来源类别（评审文档「一、#2」）。

    ``MATERIAL`` 的引用指向资料片段/章节，前端可跳到阅读器；
    ``ASSIGNMENT`` 的引用指向作业与评分标准这类可信业务对象，
    它们没有页码，前端只做展示、不跳转。
    """

    MATERIAL = "MATERIAL"
    ASSIGNMENT = "ASSIGNMENT"


class ChatMessageRole(str, enum.Enum):
    """消息角色（契约 6.6）。"""

    USER = "USER"
    ASSISTANT = "ASSISTANT"


class ChatAttemptStatus(str, enum.Enum):
    """生成尝试状态（内部记录，不对外暴露）。"""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ChatSession(Base):
    """问答会话（契约 6.2–6.3）。"""

    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    course_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "courses.id",
            ondelete="CASCADE",
            name="fk_chat_sessions_course_id_courses",
        ),
        nullable=False,
    )

    #: 会话所有者；消息接口只对它可见（契约 6.1）
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_chat_sessions_user_id_users"),
        nullable=False,
    )

    #: 乐观并发版本：每次成功写入一问一答 +1（契约 6.1 的并发保护，不对外暴露）
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    #: 最近一条消息的时间；无消息时等于 ``created_at``（契约 6.3 的倒序排序键）
    last_message_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        # 会话列表：按 (课程, 所有者) 过滤后按 last_message_at/id 倒序分页
        Index(
            "ix_chat_sessions_course_user_last_message",
            "course_id",
            "user_id",
            "last_message_at",
            "id",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<ChatSession id={self.id} version={self.version}>"


class ChatMessage(Base):
    """会话消息（契约 6.6）。"""

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "chat_sessions.id",
            ondelete="CASCADE",
            name="fk_chat_messages_session_id_chat_sessions",
        ),
        nullable=False,
    )

    role: Mapped[ChatMessageRole] = mapped_column(
        SAEnum(ChatMessageRole, name="chat_message_role", native_enum=True),
        nullable=False,
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)

    #: 助手消息是否有资料依据；用户消息恒为 NULL（契约 6.6）
    grounded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        # 消息列表：按会话过滤后按 created_at/id 升序分页（契约 6.1）
        Index(
            "ix_chat_messages_session_created_id",
            "session_id",
            "created_at",
            "id",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<ChatMessage id={self.id} role={self.role.value}>"


class ChatMessageCitation(Base):
    """引用快照（契约 6.6）。

    **不设外键**：引用是写入时刻的展示快照，资料被删除、章节被重写后，
    历史对话仍需完整回读（契约 6.1 的读历史语义）。

    引用**不限于资料**（评审文档「一、#2」）：作业与评分标准是可信业务对象，
    同样可以支撑一次有依据的回答，只是它们没有页码、也不能跳到资料阅读器。
    因此用 ``source_kind`` 区分来源类别，资料之外的引用把 ``material_*`` 与
    ``location_*`` 留空，改为填 ``source_id`` / ``source_label``。
    """

    __tablename__ = "chat_message_citations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "chat_messages.id",
            ondelete="CASCADE",
            name="fk_chat_message_citations_message_id_chat_messages",
        ),
        nullable=False,
    )

    #: 引用顺序，从 1 开始（同一消息内连续）
    order: Mapped[int] = mapped_column(Integer, nullable=False)

    #: 来源类别：``MATERIAL``（资料片段/章节）或 ``ASSIGNMENT``（作业与评分标准）
    source_kind: Mapped[str] = mapped_column(
        String(SOURCE_KIND_MAX_LENGTH),
        nullable=False,
        default=CitationSourceKind.MATERIAL.value,
        server_default=CitationSourceKind.MATERIAL.value,
    )

    #: 来源自身的 ID：资料 ID / 作业 ID
    source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    #: 人类可读的来源标签，供前端直接展示（例如「作业《实验二》」）
    source_label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 仅 ``source_kind=MATERIAL`` 时有值
    material_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    material_name: Mapped[str | None] = mapped_column(
        String(MATERIAL_NAME_MAX_LENGTH), nullable=True
    )

    #: 命中章节；片段跨章节或落在章节外时为 NULL（契约 6.6）
    section_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    section_title: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: 来源类型：PDF_PAGE / PPTX_SLIDE / DOCX_PARAGRAPH；非资料来源为 ``NONE``
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)

    location_start: Mapped[int | None] = mapped_column(Integer, nullable=True)

    location_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: 仅 PDF 有值（等于 location_start）；PPTX 与 DOCX 为 NULL（契约 6.6）
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: 可核对的原文摘录，已由服务端校验确实出现在片段原文中
    quote: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_chat_message_citations_message_id", "message_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<ChatMessageCitation id={self.id} order={self.order}>"


class ChatGenerationAttempt(Base):
    """回答生成尝试记录（内部；契约 6.1 的可观测性要求）。

    每次发送问题都会留下一条记录：无论成功还是失败，都保存模型名称、
    提示词版本、检索片段数量、耗时与**安全失败摘要**——不含提示词、
    课件原文、模型地址或密钥。
    """

    __tablename__ = "chat_generation_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "chat_sessions.id",
            ondelete="CASCADE",
            name="fk_chat_generation_attempts_session_id_chat_sessions",
        ),
        nullable=False,
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    status: Mapped[ChatAttemptStatus] = mapped_column(
        SAEnum(ChatAttemptStatus, name="chat_attempt_status", native_enum=True),
        nullable=False,
    )

    #: 模型名称；模型未配置时为 NULL
    model: Mapped[str | None] = mapped_column(
        String(MODEL_NAME_MAX_LENGTH), nullable=True
    )

    prompt_version: Mapped[str] = mapped_column(
        String(PROMPT_VERSION_MAX_LENGTH), nullable=False
    )

    #: 本次实际送入模型的片段数量
    retrieved_count: Mapped[int] = mapped_column(Integer, nullable=False)

    #: 结果是否有依据；失败时为 NULL
    grounded: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    #: 安全失败摘要（不含提示词、原文与模型地址）
    error: Mapped[str | None] = mapped_column(
        String(ATTEMPT_ERROR_MAX_LENGTH), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_chat_generation_attempts_session_created",
            "session_id",
            "created_at",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<ChatGenerationAttempt id={self.id} status={self.status.value}>"
