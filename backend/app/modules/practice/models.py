"""Practice 模块 ORM 模型（``docs/api-contract.md`` 第 7 节：练习）。

六张表承载"生成 → 发布 → 提交 → 结果"的完整闭环：

- ``practice_sets``：练习集。课程、创建教师、标题、生成状态、难度、
  请求题数与题型、发布时间。
- ``practice_set_materials``：生成时选择的来源资料及顺序（**快照引用**，
  不设外键：资料被软删除或重新解析不影响历史练习）。
- ``practice_questions``：题目。题型、顺序、题干、JSONB 选项、标准答案、
  解析、知识点、简答题评分要点、来源资料/片段定位/原文摘录快照。
- ``practice_attempts``：答题记录。学生对练习**只能提交一次**
  （``(practice_set_id, student_id)`` 唯一约束是最终防线）。
- ``practice_attempt_answers``：每题提交答案、是否完全正确、得分与命中要点。
- ``practice_generation_attempts``：生成尝试记录（模型、提示词版本、耗时、
  安全失败摘要；不含提示词与课件原文）。

题型与难度使用 PostgreSQL 原生枚举；分数使用 ``Numeric(5, 2)`` 并按
``Decimal`` 计算，保证百分制两位小数的稳定结果。

时间列沿用 :class:`app.db.types.UtcDateTime`，读写两端均为 UTC。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.core.time import utc_now
from app.db.base import Base
from app.db.types import UtcDateTime

#: 练习标题长度
TITLE_MAX_LENGTH = 200

#: 知识点名称长度
KNOWLEDGE_POINT_MAX_LENGTH = 255

#: 来源资料名（快照）长度
SOURCE_NAME_MAX_LENGTH = 255

#: 单次生成可选择的最少 / 最多资料数
MIN_MATERIALS = 1
MAX_MATERIALS = 10

#: 单次生成题数范围
MIN_QUESTIONS = 1
MAX_QUESTIONS = 20

#: 简答题提交文本长度上限
SHORT_ANSWER_MAX_LENGTH = 2000

#: 生成尝试失败摘要长度
ATTEMPT_ERROR_MAX_LENGTH = 500

#: 提示词版本 / 模型名长度
PROMPT_VERSION_MAX_LENGTH = 32
MODEL_NAME_MAX_LENGTH = 128

#: 百分制分数精度
SCORE_PRECISION = Numeric(5, 2)


class PracticeStatus(str, enum.Enum):
    """练习状态（契约 7.1）。"""

    GENERATING = "GENERATING"
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PracticeDifficulty(str, enum.Enum):
    """难度（契约 7.2）。"""

    EASY = "EASY"
    MEDIUM = "MEDIUM"
    HARD = "HARD"


class PracticeQuestionType(str, enum.Enum):
    """题型（契约 7.2）。"""

    SINGLE_CHOICE = "SINGLE_CHOICE"
    TRUE_FALSE = "TRUE_FALSE"
    SHORT_ANSWER = "SHORT_ANSWER"


class PracticeGenerationStatus(str, enum.Enum):
    """生成尝试状态（内部记录）。"""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class PracticeSet(Base):
    """练习集（契约 7.2–7.5）。"""

    __tablename__ = "practice_sets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    course_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "courses.id",
            ondelete="CASCADE",
            name="fk_practice_sets_course_id_courses",
        ),
        nullable=False,
    )

    #: 创建教师；生成、发布与重试都只允许本人
    teacher_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "users.id", ondelete="CASCADE", name="fk_practice_sets_teacher_id_users"
        ),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH), nullable=False)

    status: Mapped[PracticeStatus] = mapped_column(
        SAEnum(PracticeStatus, name="practice_status", native_enum=True),
        nullable=False,
        default=PracticeStatus.GENERATING,
    )

    difficulty: Mapped[PracticeDifficulty] = mapped_column(
        SAEnum(PracticeDifficulty, name="practice_difficulty", native_enum=True),
        nullable=False,
    )

    #: 请求的题数（1–20），用于生成与失败重试
    requested_question_count: Mapped[int] = mapped_column(Integer, nullable=False)

    #: 实际生成的题数；生成成功前为 0
    question_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    #: 请求的题型列表（按请求顺序，决定题数分配）
    question_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False)

    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime,
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            f"requested_question_count >= {MIN_QUESTIONS}"
            f" AND requested_question_count <= {MAX_QUESTIONS}",
            name="ck_practice_sets_requested_count",
        ),
        CheckConstraint(
            f"question_count >= 0 AND question_count <= {MAX_QUESTIONS}",
            name="ck_practice_sets_question_count",
        ),
        # 课程已发布练习列表：按 (课程, 状态) 过滤后按 published_at/id 倒序
        Index(
            "ix_practice_sets_course_status_published",
            "course_id",
            "status",
            "published_at",
            "id",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<PracticeSet id={self.id} status={self.status.value}>"


class PracticeSetMaterial(Base):
    """生成时选择的来源资料及顺序（快照引用，不设外键）。"""

    __tablename__ = "practice_set_materials"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    practice_set_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "practice_sets.id",
            ondelete="CASCADE",
            name="fk_practice_set_materials_practice_set_id_practice_sets",
        ),
        nullable=False,
    )

    #: 来源资料 ID（快照：资料后续被删除不影响历史练习）
    material_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    #: 选择顺序，从 1 开始（决定片段轮询与题型分配顺序）
    order: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "practice_set_id",
            "order",
            name="uq_practice_set_materials_set_order",
        ),
        UniqueConstraint(
            "practice_set_id",
            "material_id",
            name="uq_practice_set_materials_set_material",
        ),
        Index("ix_practice_set_materials_set_id", "practice_set_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<PracticeSetMaterial set={self.practice_set_id} order={self.order}>"


class PracticeQuestion(Base):
    """题目（契约 7.8）：一经生成成功即不可变。"""

    __tablename__ = "practice_questions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    practice_set_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "practice_sets.id",
            ondelete="CASCADE",
            name="fk_practice_questions_practice_set_id_practice_sets",
        ),
        nullable=False,
    )

    #: 题目顺序，从 1 开始且在同一练习内连续
    order: Mapped[int] = mapped_column(Integer, nullable=False)

    type: Mapped[PracticeQuestionType] = mapped_column(
        SAEnum(PracticeQuestionType, name="practice_question_type", native_enum=True),
        nullable=False,
    )

    prompt: Mapped[str] = mapped_column(Text, nullable=False)

    #: 选项：[{"id": uuid, "text": str}]；判断题与简答题为空列表
    options: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list
    )

    #: 标准答案：单选为选项 ID（字符串）、判断为布尔、简答为标准答案文本
    correct_answer: Mapped[object] = mapped_column(JSONB, nullable=False)

    explanation: Mapped[str] = mapped_column(Text, nullable=False)

    knowledge_point: Mapped[str | None] = mapped_column(
        String(KNOWLEDGE_POINT_MAX_LENGTH), nullable=True
    )

    #: 简答题评分要点：[{"point": str, "accepted": [str]}]
    grading_points: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list
    )

    # ---------------- 来源快照（资料被删除或重解析不影响历史练习） ----------------
    source_material_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_material_name: Mapped[str] = mapped_column(
        String(SOURCE_NAME_MAX_LENGTH), nullable=False
    )
    source_location_start: Mapped[int] = mapped_column(Integer, nullable=False)
    source_location_end: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 生成时摘录的原文（用于核对与展示）
    source_quote: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "practice_set_id", "order", name="uq_practice_questions_set_order"
        ),
        Index("ix_practice_questions_set_id", "practice_set_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<PracticeQuestion id={self.id} order={self.order}>"


class PracticeAttempt(Base):
    """答题记录（契约 7.6）：每生每题集只能有一条。"""

    __tablename__ = "practice_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    practice_set_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "practice_sets.id",
            ondelete="CASCADE",
            name="fk_practice_attempts_practice_set_id_practice_sets",
        ),
        nullable=False,
    )

    student_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE", name="fk_practice_attempts_student_id_users"),
        nullable=False,
    )

    #: 百分制总分（两位小数，0–100）
    total_score: Mapped[Decimal] = mapped_column(SCORE_PRECISION, nullable=False)

    submitted_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        # 唯一约束是"只能提交一次"的最终防线（并发提交只有一个成功）
        UniqueConstraint(
            "practice_set_id",
            "student_id",
            name="uq_practice_attempts_set_student",
        ),
        CheckConstraint(
            "total_score >= 0 AND total_score <= 100",
            name="ck_practice_attempts_total_score",
        ),
        Index("ix_practice_attempts_student_submitted", "student_id", "submitted_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<PracticeAttempt id={self.id} score={self.total_score}>"


class PracticeAttemptAnswer(Base):
    """每题提交答案与得分（契约 7.7）。"""

    __tablename__ = "practice_attempt_answers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "practice_attempts.id",
            ondelete="CASCADE",
            name="fk_practice_attempt_answers_attempt_id_practice_attempts",
        ),
        nullable=False,
    )

    #: 题目快照引用（题目随练习存在，按 ID 关联即可）
    question_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    question_order: Mapped[int] = mapped_column(Integer, nullable=False)

    #: 提交的答案：单选为选项 ID、判断为布尔、简答为文本
    submitted_answer: Mapped[object] = mapped_column(JSONB, nullable=False)

    #: 是否完全正确（简答题需命中全部要点）
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)

    #: 本题得分（两位小数，0–100）
    score: Mapped[Decimal] = mapped_column(SCORE_PRECISION, nullable=False)

    #: 命中的评分要点文本列表（简答题用；其他题型为空）
    matched_points: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "attempt_id", "question_id", name="uq_practice_attempt_answers_question"
        ),
        CheckConstraint(
            "score >= 0 AND score <= 100",
            name="ck_practice_attempt_answers_score",
        ),
        Index("ix_practice_attempt_answers_attempt_id", "attempt_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<PracticeAttemptAnswer question={self.question_id} score={self.score}>"


class PracticeGenerationAttempt(Base):
    """生成尝试记录（内部；契约 7.10 的可观测性要求）。"""

    __tablename__ = "practice_generation_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    practice_set_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "practice_sets.id",
            ondelete="CASCADE",
            name="fk_practice_generation_attempts_set_id_practice_sets",
        ),
        nullable=False,
    )

    teacher_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    status: Mapped[PracticeGenerationStatus] = mapped_column(
        SAEnum(
            PracticeGenerationStatus,
            name="practice_generation_status",
            native_enum=True,
        ),
        nullable=False,
    )

    #: 模型名称；未配置时为 NULL
    model: Mapped[str | None] = mapped_column(
        String(MODEL_NAME_MAX_LENGTH), nullable=True
    )

    prompt_version: Mapped[str] = mapped_column(
        String(PROMPT_VERSION_MAX_LENGTH), nullable=False
    )

    requested_count: Mapped[int] = mapped_column(Integer, nullable=False)

    #: 实际生成的题数；失败时为 NULL
    question_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    #: 安全失败摘要（不含提示词、课件原文与模型地址）
    error: Mapped[str | None] = mapped_column(
        String(ATTEMPT_ERROR_MAX_LENGTH), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "ix_practice_generation_attempts_set_created",
            "practice_set_id",
            "created_at",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<PracticeGenerationAttempt set={self.practice_set_id} status={self.status.value}>"
