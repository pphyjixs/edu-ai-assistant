"""Courses 模块 ORM 模型。

两张表对应 ``docs/api-contract.md`` 第 3 节：

- ``courses``：课程。``teacher_id`` 是**创建教师**，第一版也只有他能管理课程；
  ``invite_code`` 建全局唯一约束，由数据库兜住并发生成冲突。
- ``course_members``：课程成员。``(course_id, user_id)`` 唯一约束保证同一课程内
  用户只有一条成员记录——并发重复加入正是被这条约束拦住的。

时间列沿用 :class:`app.db.types.UtcDateTime`，读写两端均为 UTC。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from app.core.time import utc_now
from app.db.base import Base
from app.db.types import UtcDateTime

#: 课程名称长度上限（去除首尾空白后校验）
COURSE_NAME_MAX_LENGTH = 100

#: 课程说明长度上限
COURSE_DESCRIPTION_MAX_LENGTH = 2000

#: 邀请码长度：12 位大写字母与数字
INVITE_CODE_LENGTH = 12


class CourseStatus(str, enum.Enum):
    """课程状态。新建为 ``ACTIVE``，归档后不可恢复。"""

    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class CourseRole(str, enum.Enum):
    """课程内角色，与平台角色相互独立。"""

    TEACHER = "TEACHER"
    STUDENT = "STUDENT"


class Course(Base):
    """课程。"""

    __tablename__ = "courses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    #: 已去除首尾空白的名称
    name: Mapped[str] = mapped_column(String(COURSE_NAME_MAX_LENGTH), nullable=False)

    #: 未填写时为空字符串，不使用 NULL
    description: Mapped[str] = mapped_column(
        String(COURSE_DESCRIPTION_MAX_LENGTH),
        nullable=False,
        default="",
        server_default="",
    )

    #: 创建教师。第一版只有创建教师能管理课程；
    #: 使用 RESTRICT 而不是 CASCADE——教师名下有课程时不应被删除。
    teacher_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_courses_teacher_id_users"),
        nullable=False,
    )

    status: Mapped[CourseStatus] = mapped_column(
        SAEnum(CourseStatus, name="course_status", native_enum=True), nullable=False
    )

    #: 当前有效的邀请码；重置后旧码立即失效（列值被覆盖）
    invite_code: Mapped[str] = mapped_column(
        String(INVITE_CODE_LENGTH), nullable=False
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

    members: Mapped[list[CourseMember]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # 邀请码全局唯一：并发生成冲突由数据库兜住
        UniqueConstraint("invite_code", name="uq_courses_invite_code"),
        Index("ix_courses_teacher_id", "teacher_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<Course id={self.id} status={self.status.value}>"


class CourseMember(Base):
    """课程成员（契约 3.3：成员列表包含创建教师与已加入学生）。"""

    __tablename__ = "course_members"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    course_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "courses.id", ondelete="CASCADE", name="fk_course_members_course_id_courses"
        ),
        nullable=False,
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "users.id", ondelete="CASCADE", name="fk_course_members_user_id_users"
        ),
        nullable=False,
    )

    course_role: Mapped[CourseRole] = mapped_column(
        SAEnum(CourseRole, name="course_role", native_enum=True), nullable=False
    )

    #: 加入时间；创建教师为课程创建时的成员记录时间
    joined_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, default=utc_now, server_default=func.now()
    )

    course: Mapped[Course] = relationship(back_populates="members")

    __table_args__ = (
        # 并发重复加入的唯一防线：不做先查后插
        UniqueConstraint(
            "course_id", "user_id", name="uq_course_members_course_id_user_id"
        ),
        Index("ix_course_members_user_id", "user_id"),
    )
