"""ORM 模型注册表。

Alembic 的 ``env.py`` 只依赖这一个模块来获取 ``target_metadata``：
新增业务模块时必须在这里导入其 ``models``，否则 autogenerate 看不到新表。
"""

from __future__ import annotations

from sqlalchemy import MetaData

from app.db.base import Base
from app.modules.agent.models import AgentRun, AgentRunSource
from app.modules.assignments.models import (
    Assignment,
    AssignmentRubricItem,
    AssignmentRubricVersion,
)
from app.modules.auth.models import AuthSession, LoginAttempt, User
from app.modules.chat.models import (
    ChatGenerationAttempt,
    ChatMessage,
    ChatMessageCitation,
    ChatSession,
)
from app.modules.courses.models import Course, CourseMember
from app.modules.jobs.models import Job
from app.modules.materials.models import (
    Material,
    MaterialChunk,
    MaterialDeleteTodo,
    MaterialKnowledgePoint,
    MaterialSection,
    MaterialUploadSession,
)
from app.modules.practice.models import (
    PracticeAttempt,
    PracticeAttemptAnswer,
    PracticeGenerationAttempt,
    PracticeQuestion,
    PracticeSet,
    PracticeSetMaterial,
)

#: 供 Alembic 比较与自动生成迁移使用
target_metadata: MetaData = Base.metadata

__all__ = [
    "AgentRun",
    "AgentRunSource",
    "Assignment",
    "AssignmentRubricItem",
    "AssignmentRubricVersion",
    "AuthSession",
    "ChatGenerationAttempt",
    "ChatMessage",
    "ChatMessageCitation",
    "ChatSession",
    "Course",
    "CourseMember",
    "Job",
    "LoginAttempt",
    "Material",
    "MaterialChunk",
    "MaterialDeleteTodo",
    "MaterialKnowledgePoint",
    "MaterialSection",
    "MaterialUploadSession",
    "PracticeAttempt",
    "PracticeAttemptAnswer",
    "PracticeGenerationAttempt",
    "PracticeQuestion",
    "PracticeSet",
    "PracticeSetMaterial",
    "User",
    "target_metadata",
]
