"""Assignments 模块 ORM 模型（``docs/api-contract.md`` 第 8 节：实验任务）。

三张表承载"任务 + 不可变评分规则版本"：

- ``assignments``：实验任务。课程、创建教师、标题、说明、截止时间、
  是否允许补交、状态、**当前评分规则版本**指针与发布/关闭时间。
- ``assignment_rubric_versions``：评分规则版本。``(assignment_id, version)``
  唯一；版本一经写入不可原地修改，只有评分规则实际变化时才追加新版本。
- ``assignment_rubric_items``：某版本的评分项。``(rubric_version_id, order)``
  唯一，新版本生成新的评分项 ID。

分数使用 ``Numeric(10, 2)``（库中精确、响应为 number），比较一律用 ``Decimal``。

``assignments.current_rubric_version_id`` 与版本表互相引用，因此迁移先建
``assignments``（该列为普通列），建完版本表后再补外键约束。
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
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.core.time import utc_now
from app.db.base import Base
from app.db.types import UtcDateTime

#: 任务标题长度上限（去除首尾空白后校验）
TITLE_MAX_LENGTH = 200

#: 任务说明长度上限
DESCRIPTION_MAX_LENGTH = 20000

#: 评分项说明长度上限
RUBRIC_ITEM_DESCRIPTION_MAX_LENGTH = 2000

#: 单次创建的评分项数量范围
MIN_RUBRIC_ITEMS = 1
MAX_RUBRIC_ITEMS = 50

#: 分数精度：总与两位小数
SCORE_PRECISION = 10
SCORE_SCALE = 2

#: 分数列的 DDL 类型
SCORE_TYPE = Numeric(SCORE_PRECISION, SCORE_SCALE)

#: 分数上限（与列精度一致，超出由请求校验先拦下）
MAX_SCORE = Decimal("99999999.99")


class AssignmentStatus(str, enum.Enum):
    """任务状态（契约 8.1）。"""

    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    CLOSED = "CLOSED"
    ARCHIVED = "ARCHIVED"


#: 学生可见的状态（其余状态对学生一律按不存在处理）
STUDENT_VISIBLE_STATUSES = (
    AssignmentStatus.PUBLISHED,
    AssignmentStatus.CLOSED,
    AssignmentStatus.ARCHIVED,
)


class Assignment(Base):
    """实验任务（契约 8.2–8.7）。"""

    __tablename__ = "assignments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    course_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "courses.id", ondelete="CASCADE", name="fk_assignments_course_id_courses"
        ),
        nullable=False,
    )

    #: 创建教师（本模块唯一的写权限来源）
    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "users.id", ondelete="RESTRICT", name="fk_assignments_created_by_users"
        ),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH), nullable=False)

    #: 未填写时为空字符串，不使用 NULL
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )

    #: 截止时间；NULL 表示不限时间
    due_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    allow_late_submission: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    status: Mapped[AssignmentStatus] = mapped_column(
        SAEnum(AssignmentStatus, name="assignment_status", native_enum=True),
        nullable=False,
    )

    #: 当前评分规则版本；创建事务内写入版本 1 后回填
    current_rubric_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "assignment_rubric_versions.id",
            ondelete="RESTRICT",
            name="fk_assignments_current_rubric_version_id_versions",
        ),
        nullable=True,
    )

    published_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

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
        # 列表查询：课程 + 状态 + created_at DESC, id DESC
        Index(
            "ix_assignments_course_status_created",
            "course_id",
            "status",
            "created_at",
            "id",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<Assignment id={self.id} status={self.status.value}>"


class AssignmentRubricVersion(Base):
    """评分规则版本（契约 8.9）：一经写入不可原地修改。"""

    __tablename__ = "assignment_rubric_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    assignment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "assignments.id",
            ondelete="CASCADE",
            name="fk_assignment_rubric_versions_assignment_id_assignments",
        ),
        nullable=False,
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False)

    total_score: Mapped[Decimal] = mapped_column(SCORE_TYPE, nullable=False)

    created_by: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_assignment_rubric_versions_created_by_users",
        ),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "assignment_id", "version", name="uq_assignment_rubric_versions_version"
        ),
        CheckConstraint("version >= 1", name="ck_assignment_rubric_versions_version"),
        CheckConstraint(
            "total_score > 0", name="ck_assignment_rubric_versions_total_score"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<AssignmentRubricVersion id={self.id} version={self.version}>"


class AssignmentRubricItem(Base):
    """某评分版本下的一项评分规则（契约 8.8）。"""

    __tablename__ = "assignment_rubric_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    rubric_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "assignment_rubric_versions.id",
            ondelete="CASCADE",
            name="fk_assignment_rubric_items_rubric_version_id_versions",
        ),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(TITLE_MAX_LENGTH), nullable=False)

    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )

    max_score: Mapped[Decimal] = mapped_column(SCORE_TYPE, nullable=False)

    order: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "rubric_version_id", "order", name="uq_assignment_rubric_items_version_order"
        ),
        CheckConstraint("max_score > 0", name="ck_assignment_rubric_items_max_score"),
        CheckConstraint('"order" > 0', name="ck_assignment_rubric_items_order"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<AssignmentRubricItem id={self.id} order={self.order}>"


__all__ = [
    "Assignment",
    "AssignmentRubricItem",
    "AssignmentRubricVersion",
    "AssignmentStatus",
    "DESCRIPTION_MAX_LENGTH",
    "MAX_RUBRIC_ITEMS",
    "MAX_SCORE",
    "MIN_RUBRIC_ITEMS",
    "RUBRIC_ITEM_DESCRIPTION_MAX_LENGTH",
    "SCORE_SCALE",
    "SCORE_TYPE",
    "STUDENT_VISIBLE_STATUSES",
    "TITLE_MAX_LENGTH",
]
