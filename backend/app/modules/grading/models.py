"""Grading 模块 ORM 模型（``docs/api-contract.md`` 第 9 节：提交与批改）。

五张表：

- ``submissions``：一位学生对一份实验任务的提交。``(assignment_id, student_id)``
  唯一——"只能提交一份报告"由数据库兜住并发。创建于初始化上传时（``UPLOADING``），
  完成确认后写入文件快照、提交时间、补交标志与**固定的评分规则版本**
  （``rubric_version_id``）。此后教师修改 Rubric 生成新版本也不影响历史提交。
- ``submission_upload_sessions``：一次上传尝试。保存声明的文件信息、对象键、
  PUT 到期时间、确认截止时间与完成/过期/被替代时间；``completion_snapshot``
  让同一 upload 重复完成返回**首次响应快照**。学生重新初始化时旧会话标记
  ``superseded_at``，其对象进入孤立对象清理范围。
- ``grade_reviews``：每份提交唯一一条批改记录（``submission_id`` 唯一）。
  同时保存 **AI 原始建议**（``ai_summary`` / ``suggested_total_score`` /
  ``grade_items.ai_score``）与**教师终稿**（``teacher_summary`` /
  ``final_total_score`` / ``grade_items.final_score`` / ``teacher_comment``），
  因此 AI 建议与教师修改可同时审计。
- ``grade_items``：某一评分项在本次批改中的快照与结果。``rubric_item_id`` 是
  **历史外键**——批改只按提交固定的版本评分，绝不读取 Assignment 的当前版本。
- ``submission_grade_attempts``：每次 Worker 尝试的审计记录（模型、提示词版本、
  原始结构化输出、安全错误、耗时），成功与失败都留痕。

时间列沿用 :class:`app.db.types.UtcDateTime`，读写两端均为 UTC。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
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
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.core.time import utc_now
from app.db.base import Base
from app.db.types import UtcDateTime

#: 文件名列长度（契约 9.2 复用 4.2：去除首尾空白后 1–255 个字符）
FILENAME_MAX_LENGTH = 255

#: MIME 列长度；规范 MIME 最长的是 docx（71 字符），留足余量
CONTENT_TYPE_MAX_LENGTH = 255

#: sha256 十六进制摘要固定 64 字符
SHA256_HEX_LENGTH = 64

#: 对象键列长度（课程/任务/上传会话 UUID 拼成的键）
OBJECT_KEY_MAX_LENGTH = 512

#: 失败原因列长度：只保存可安全展示的摘要，不保存堆栈
SUBMISSION_ERROR_MAX_LENGTH = 500

#: 评分规则版本号：批改按提交固定的版本评分
#: 复核摘要长度上限（契约 9.8）
SUMMARY_MAX_LENGTH = 20000

#: 教师评语与改进建议长度上限
COMMENT_MAX_LENGTH = 2000

#: 证据摘录长度上限（与报告原文摘录对齐）
EVIDENCE_MAX_LENGTH = 2000

#: 错误类型长度上限
ERROR_TYPE_MAX_LENGTH = 100

#: 证据来源类型（由报告 MIME 在服务端确定，模型不能自行决定）：
#: PDF 按页、DOCX 按段落，均从 1 开始
EVIDENCE_SOURCE_PDF_PAGE = "PDF_PAGE"
EVIDENCE_SOURCE_DOCX_PARAGRAPH = "DOCX_PARAGRAPH"
EVIDENCE_SOURCE_TYPES = frozenset(
    {EVIDENCE_SOURCE_PDF_PAGE, EVIDENCE_SOURCE_DOCX_PARAGRAPH}
)

#: 证据来源类型列长度
EVIDENCE_SOURCE_MAX_LENGTH = 32

#: 记录在尝试表里的原始结构化输出上限（只做审计，超长截断）
RAW_OUTPUT_MAX_LENGTH = 20000

#: 分数精度：与 ``assignment_rubric_items.max_score`` 一致（两位小数）
SCORE_PRECISION = 10
SCORE_SCALE = 2

#: 分数列的 DDL 类型
SCORE_TYPE = Numeric(SCORE_PRECISION, SCORE_SCALE)


class SubmissionStatus(str, enum.Enum):
    """提交状态（契约 9.1）。"""

    UPLOADING = "UPLOADING"
    SUBMITTED = "SUBMITTED"
    GRADING = "GRADING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"


#: 教师正式提交列表包含的状态（``UPLOADING`` 不进入）
SUBMITTED_STATUSES = (
    SubmissionStatus.SUBMITTED,
    SubmissionStatus.GRADING,
    SubmissionStatus.REVIEW_REQUIRED,
    SubmissionStatus.PUBLISHED,
    SubmissionStatus.FAILED,
)


class SubmissionGradeAttemptStatus(str, enum.Enum):
    """一次批改尝试的结果。"""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class Submission(Base):
    """一位学生对一份实验任务的提交（契约 9.1–9.10）。"""

    __tablename__ = "submissions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    assignment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "assignments.id",
            ondelete="CASCADE",
            name="fk_submissions_assignment_id_assignments",
        ),
        nullable=False,
    )

    #: 冗余保存课程：列表、加锁与权限检查都按课程聚合，避免每次回查任务
    course_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "courses.id", ondelete="CASCADE", name="fk_submissions_course_id_courses"
        ),
        nullable=False,
    )

    student_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "users.id", ondelete="RESTRICT", name="fk_submissions_student_id_users"
        ),
        nullable=False,
    )

    status: Mapped[SubmissionStatus] = mapped_column(
        SAEnum(SubmissionStatus, name="submission_status", native_enum=True),
        nullable=False,
        default=SubmissionStatus.UPLOADING,
    )

    #: 提交**固定**的评分规则版本；完成确认时写入，未提交时为 NULL。
    #: 历史外键（RESTRICT）：批改与复核只读它，不读任务的当前版本。
    rubric_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "assignment_rubric_versions.id",
            ondelete="RESTRICT",
            name="fk_submissions_rubric_version_id_versions",
        ),
        nullable=True,
    )

    #: 当前有效上传会话声明的文件信息；完成确认后即为提交快照
    filename: Mapped[str] = mapped_column(String(FILENAME_MAX_LENGTH), nullable=False)

    content_type: Mapped[str] = mapped_column(
        String(CONTENT_TYPE_MAX_LENGTH), nullable=False
    )

    size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    sha256: Mapped[str] = mapped_column(String(SHA256_HEX_LENGTH), nullable=False)

    #: 当前有效上传会话的对象键；重新初始化时被替换为新的键
    object_key: Mapped[str] = mapped_column(
        String(OBJECT_KEY_MAX_LENGTH), nullable=False
    )

    #: 是否补交：完成确认时已过截止时间但允许补交时为 true
    is_late: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    #: 正式提交时间；NULL 表示尚未完成提交（``UPLOADING``）
    submitted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    #: 批改失败原因的安全摘要；非 FAILED 时为 NULL
    error_message: Mapped[str | None] = mapped_column(
        String(SUBMISSION_ERROR_MAX_LENGTH), nullable=True
    )

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
        # 一位学生对同一任务只能有一份提交：并发由唯一约束兜住
        UniqueConstraint(
            "assignment_id", "student_id", name="uq_submissions_assignment_student"
        ),
        # 对象键全局唯一：不同上传尝试不会互相覆盖
        UniqueConstraint("object_key", name="uq_submissions_object_key"),
        # 教师列表（契约 9.4）：按任务过滤、按 submitted_at DESC, id DESC 分页
        Index("ix_submissions_assignment_submitted", "assignment_id", "submitted_at", "id"),
        # 学生视角（9.4）：按 (task, student) 直查本人提交
        Index("ix_submissions_student_id", "student_id"),
        Index("ix_submissions_course_id", "course_id"),
        Index("ix_submissions_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<Submission id={self.id} status={self.status.value}>"


class SubmissionUploadSession(Base):
    """一次报告上传尝试（契约 9.2 / 9.3）。"""

    __tablename__ = "submission_upload_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    submission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "submissions.id",
            ondelete="CASCADE",
            name="fk_submission_upload_sessions_submission_id_submissions",
        ),
        nullable=False,
    )

    #: 冗余课程与任务：清理命令按对象键定位，不需要回查提交
    course_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    assignment_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    student_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)

    #: 随机对象键：不可猜测，唯一约束保证不会与其他上传重叠
    object_key: Mapped[str] = mapped_column(
        String(OBJECT_KEY_MAX_LENGTH), nullable=False
    )

    filename: Mapped[str] = mapped_column(String(FILENAME_MAX_LENGTH), nullable=False)

    content_type: Mapped[str] = mapped_column(
        String(CONTENT_TYPE_MAX_LENGTH), nullable=False
    )

    size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    sha256: Mapped[str] = mapped_column(String(SHA256_HEX_LENGTH), nullable=False)

    #: 预签名 PUT 地址的到期时间（初始化 + 10 分钟）
    upload_url_expires_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    #: 完成确认的截止时间（初始化 + 24 小时）
    confirm_deadline_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    #: 完成确认时间；与提交快照在同一事务中写入
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    #: 完成响应快照（JSONB）：同一 upload 重复完成一律回填首次结果
    completion_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    #: 过期清理标记：超过确认窗口仍未完成的会话，其孤立对象已被删除
    expired_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    #: 被替代时间：学生重新初始化上传时旧会话被替换，其对象进入清理范围
    superseded_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

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
        UniqueConstraint(
            "object_key", name="uq_submission_upload_sessions_object_key"
        ),
        # 每份提交最多只有一个**完成**的上传会话（部分唯一索引）：
        # 无论顺序还是并发完成不同会话，第二个都会被它拦下，
        # 而不是悄悄产生第二份"已完成"记录（契约 9.3）。
        Index(
            "uq_submission_upload_sessions_submission_completed",
            "submission_id",
            unique=True,
            postgresql_where=text("completed_at IS NOT NULL"),
        ),
        Index(
            "ix_submission_upload_sessions_submission_id", "submission_id"
        ),
        # 过期清理按确认截止时间扫描
        Index(
            "ix_submission_upload_sessions_confirm_deadline_at", "confirm_deadline_at"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<SubmissionUploadSession id={self.id} filename={self.filename!r}>"


class GradeReview(Base):
    """一份提交的批改记录（契约 9.7–9.9）：每份提交唯一一条。"""

    __tablename__ = "grade_reviews"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    submission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "submissions.id",
            ondelete="CASCADE",
            name="fk_grade_reviews_submission_id_submissions",
        ),
        nullable=False,
    )

    #: AI 生成的总体评语（原始值，教师修改不影响它）
    ai_summary: Mapped[str] = mapped_column(Text, nullable=False)

    #: 教师终稿摘要；复核前与 AI 摘要相同
    teacher_summary: Mapped[str] = mapped_column(Text, nullable=False)

    #: AI 建议总分（各评分项建议分之和，服务端计算）
    suggested_total_score: Mapped[Decimal] = mapped_column(SCORE_TYPE, nullable=False)

    #: 教师最终总分（发布成绩以它为准）
    final_total_score: Mapped[Decimal] = mapped_column(SCORE_TYPE, nullable=False)

    #: 复核人；未复核时为 NULL
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "users.id", ondelete="RESTRICT", name="fk_grade_reviews_reviewed_by_users"
        ),
        nullable=True,
    )

    reviewed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

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
        # 每份提交唯一一条批改记录：重复触发不会创建第二条
        UniqueConstraint("submission_id", name="uq_grade_reviews_submission_id"),
        CheckConstraint(
            "suggested_total_score >= 0", name="ck_grade_reviews_suggested_total_score"
        ),
        CheckConstraint(
            "final_total_score >= 0", name="ck_grade_reviews_final_total_score"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<GradeReview id={self.id} submission={self.submission_id}>"


class GradeItem(Base):
    """一个评分项在本次批改中的快照与结果（契约 9.7 / 9.8）。

    ``rubric_item_id`` 指向**提交固定的**评分版本下的评分项（历史外键）：
    教师之后修改 Rubric 生成新版本时，历史批改仍指向旧版本的评分项。
    """

    __tablename__ = "grade_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    review_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "grade_reviews.id",
            ondelete="CASCADE",
            name="fk_grade_items_review_id_grade_reviews",
        ),
        nullable=False,
    )

    #: 历史外键：提交固定的评分版本下的评分项
    rubric_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "assignment_rubric_items.id",
            ondelete="RESTRICT",
            name="fk_grade_items_rubric_item_id_rubric_items",
        ),
        nullable=False,
    )

    #: 评分项快照：标题、满分与顺序在批改时固定，之后 Rubric 变更不影响历史结果
    title: Mapped[str] = mapped_column(String(FILENAME_MAX_LENGTH), nullable=False)

    max_score: Mapped[Decimal] = mapped_column(SCORE_TYPE, nullable=False)

    order: Mapped[int] = mapped_column(Integer, nullable=False)

    #: AI 建议分（原始值，教师修改不影响它）
    ai_score: Mapped[Decimal] = mapped_column(SCORE_TYPE, nullable=False)

    #: 教师终稿分；复核前与 AI 建议分相同
    final_score: Mapped[Decimal] = mapped_column(SCORE_TYPE, nullable=False)

    #: AI 判断说明
    ai_comment: Mapped[str] = mapped_column(Text, nullable=False)

    #: 证据定位：逐字摘自报告原文的摘录（服务端已核对存在于声明的位置区间内）
    evidence_quote: Mapped[str] = mapped_column(String(EVIDENCE_MAX_LENGTH), nullable=False)

    #: 证据来源类型：由报告 MIME 在服务端确定（``PDF_PAGE`` / ``DOCX_PARAGRAPH``），
    #: 模型不能自行决定
    evidence_source_type: Mapped[str] = mapped_column(
        String(EVIDENCE_SOURCE_MAX_LENGTH), nullable=False
    )

    #: 证据在来源中的位置区间（从 1 开始，结束不小于起点）
    evidence_location_start: Mapped[int] = mapped_column(Integer, nullable=False)

    evidence_location_end: Mapped[int] = mapped_column(Integer, nullable=False)

    #: 错误类型（模型给出的分类，可为空字符串）
    error_type: Mapped[str] = mapped_column(
        String(ERROR_TYPE_MAX_LENGTH), nullable=False, default="", server_default=""
    )

    #: 改进建议
    improvement_suggestion: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )

    #: 教师评语；未复核时为空字符串
    teacher_comment: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )

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
        # 同一份批改里每个评分项恰好一行：模型输出重复会被这条约束拦下
        UniqueConstraint(
            "review_id", "rubric_item_id", name="uq_grade_items_review_rubric_item"
        ),
        UniqueConstraint("review_id", "order", name="uq_grade_items_review_order"),
        CheckConstraint("max_score > 0", name="ck_grade_items_max_score"),
        CheckConstraint('"order" > 0', name="ck_grade_items_order"),
        # 分数范围：AI 建议分与教师终稿都必须在 0..max_score 之间
        CheckConstraint(
            "ai_score >= 0 AND ai_score <= max_score", name="ck_grade_items_ai_score"
        ),
        CheckConstraint(
            "final_score >= 0 AND final_score <= max_score",
            name="ck_grade_items_final_score",
        ),
        # 证据定位：来源类型白名单 + 位置从 1 开始 + 区间不倒序
        CheckConstraint(
            "evidence_source_type IN ('PDF_PAGE', 'DOCX_PARAGRAPH')",
            name="ck_grade_items_evidence_source",
        ),
        CheckConstraint(
            "evidence_location_start > 0", name="ck_grade_items_evidence_start"
        ),
        CheckConstraint(
            "evidence_location_end >= evidence_location_start",
            name="ck_grade_items_evidence_range",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<GradeItem id={self.id} order={self.order}>"


class SubmissionGradeAttempt(Base):
    """一次批改尝试的审计记录（契约 9.11）：成功与失败都留痕。

    只保存**安全**内容：模型名、提示词版本、结构化输出（截断）与失败摘要，
    绝不保存完整报告原文、提示词全文或任何密钥。
    """

    __tablename__ = "submission_grade_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    submission_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "submissions.id",
            ondelete="CASCADE",
            name="fk_submission_grade_attempts_submission_id_submissions",
        ),
        nullable=False,
    )

    status: Mapped[SubmissionGradeAttemptStatus] = mapped_column(
        SAEnum(
            SubmissionGradeAttemptStatus,
            name="submission_grade_attempt_status",
            native_enum=True,
        ),
        nullable=False,
    )

    #: 模型名与提示词版本；未配置模型时为空
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(100), nullable=True)

    #: 评分项数量（成功时为模型返回的项数）
    item_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    #: 模型返回的原始结构化输出（截断后保存，供审计与排障）
    raw_output: Mapped[str | None] = mapped_column(
        String(RAW_OUTPUT_MAX_LENGTH), nullable=True
    )

    #: 失败原因的安全摘要；成功时为 NULL
    error: Mapped[str | None] = mapped_column(
        String(SUBMISSION_ERROR_MAX_LENGTH), nullable=True
    )

    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_submission_grade_attempts_submission_id", "submission_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<SubmissionGradeAttempt id={self.id} status={self.status.value}>"


__all__ = [
    "COMMENT_MAX_LENGTH",
    "CONTENT_TYPE_MAX_LENGTH",
    "EVIDENCE_MAX_LENGTH",
    "EVIDENCE_SOURCE_DOCX_PARAGRAPH",
    "EVIDENCE_SOURCE_MAX_LENGTH",
    "EVIDENCE_SOURCE_PDF_PAGE",
    "EVIDENCE_SOURCE_TYPES",
    "ERROR_TYPE_MAX_LENGTH",
    "FILENAME_MAX_LENGTH",
    "GradeItem",
    "GradeReview",
    "OBJECT_KEY_MAX_LENGTH",
    "RAW_OUTPUT_MAX_LENGTH",
    "SCORE_PRECISION",
    "SCORE_SCALE",
    "SCORE_TYPE",
    "SHA256_HEX_LENGTH",
    "SUBMISSION_ERROR_MAX_LENGTH",
    "SUBMITTED_STATUSES",
    "SUMMARY_MAX_LENGTH",
    "Submission",
    "SubmissionGradeAttempt",
    "SubmissionGradeAttemptStatus",
    "SubmissionStatus",
    "SubmissionUploadSession",
]
