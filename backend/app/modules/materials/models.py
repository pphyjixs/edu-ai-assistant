"""Materials 模块 ORM 模型。

两张表加一条唯一约束，承载 ``docs/api-contract.md`` 第 4 节的课件上传协议：

- ``material_upload_sessions``：上传会话。保存课程、发起教师、**随机对象键**、
  预期大小、类型、哈希、两个期限（PUT 地址到期 / 确认截止）与完成结果
  （``completed_material_id`` + ``completed_at``）。对象键随机生成且唯一，
  客户端无法猜测或覆盖别人的对象。
- ``materials``：资料。``upload_id`` **唯一外键**指向上传会话——重复完成同一
  上传会话会被这条约束拦住，是「幂等完成」的第二道防线（第一道是会话行锁）。
- 解析任务不在本模块建表：统一落在 ``jobs`` 表（``app.modules.jobs``），
  由 ``(type, resource_id)`` 唯一约束保证一条资料只有一个 ``MATERIAL_PARSE`` 任务。

``completed_material_id`` 与 ``materials.upload_id`` 互相引用，构成循环外键，
因此会话侧的约束用 ``use_alter=True``：迁移必须先建两张表，再补这条外键。

时间列沿用 :class:`app.db.types.UtcDateTime`，读写两端均为 UTC。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from app.core.time import utc_now
from app.db.base import Base
from app.db.types import UtcDateTime

#: 文件名列长度（契约 4.2：去除首尾空白后 1–255 个字符）
FILENAME_MAX_LENGTH = 255

#: MIME 列长度；规范 MIME 最长的是 pptx（84 字符），留足余量
CONTENT_TYPE_MAX_LENGTH = 255

#: sha256 十六进制摘要固定 64 字符
SHA256_HEX_LENGTH = 64

#: 对象键列长度（课程/资料分片拼成的键）
OBJECT_KEY_MAX_LENGTH = 512

#: 失败原因列长度：只保存可安全展示的摘要，不保存堆栈
MATERIAL_ERROR_MAX_LENGTH = 500


class MaterialStatus(str, enum.Enum):
    """资料解析状态（``docs/modules.md`` 第 4 节）。

    第一版不实现解析 Worker：完成确认后资料即为 ``PROCESSING``，且不会自动推进。
    """

    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


class MaterialUploadSession(Base):
    """一次课件上传会话（契约 4.3 的 ``upload_id``）。"""

    __tablename__ = "material_upload_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    course_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "courses.id",
            ondelete="CASCADE",
            name="fk_material_upload_sessions_course_id_courses",
        ),
        nullable=False,
    )

    #: 发起上传的教师（契约：只有课程教师能初始化上传）
    teacher_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "users.id",
            ondelete="RESTRICT",
            name="fk_material_upload_sessions_teacher_id_users",
        ),
        nullable=False,
    )

    #: 随机对象键：不可猜测，唯一约束保证不会与其他上传重叠
    object_key: Mapped[str] = mapped_column(
        String(OBJECT_KEY_MAX_LENGTH), nullable=False
    )

    #: 去除首尾空白后的文件名，保留原始大小写
    filename: Mapped[str] = mapped_column(String(FILENAME_MAX_LENGTH), nullable=False)

    #: 规范 MIME，必须与扩展名匹配
    content_type: Mapped[str] = mapped_column(
        String(CONTENT_TYPE_MAX_LENGTH), nullable=False
    )

    #: 声明的对象字节数；完成确认时与对象实际大小比对
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    #: 声明的内容摘要（小写十六进制）；第一版不与对象内容二次比对
    sha256: Mapped[str] = mapped_column(String(SHA256_HEX_LENGTH), nullable=False)

    #: 预签名 PUT 地址的到期时间（初始化 + 10 分钟）
    upload_url_expires_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False
    )

    #: 完成确认的截止时间（初始化 + 24 小时）；过期未确认的会话由清理任务删除
    confirm_deadline_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    #: 完成结果：指向本次上传创建的资料。未完成时为空。
    #: use_alter=True —— 与 materials.upload_id 构成循环外键，建表后补约束。
    completed_material_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey(
            "materials.id",
            ondelete="RESTRICT",
            name="fk_material_upload_sessions_completed_material_id_materials",
            use_alter=True,
        ),
        nullable=True,
    )

    #: 完成确认时间；与资料、任务在同一事务中写入
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    #: 过期清理标记：超过确认窗口仍未完成的会话由清理命令标记为过期，
    #: 其孤立对象已被删除。非 NULL 表示已被清理，清理命令据此幂等跳过。
    #: 已完成的会话（``completed_material_id`` 非空）永不进入清理范围。
    expired_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

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
        # 对象键全局唯一：不同上传不会互相覆盖
        UniqueConstraint("object_key", name="uq_material_upload_sessions_object_key"),
        # 一次上传最多对应一个完成结果
        UniqueConstraint(
            "completed_material_id",
            name="uq_material_upload_sessions_completed_material_id",
        ),
        Index("ix_material_upload_sessions_course_id", "course_id"),
        Index("ix_material_upload_sessions_teacher_id", "teacher_id"),
        # 过期清理按确认截止时间扫描
        Index(
            "ix_material_upload_sessions_confirm_deadline_at", "confirm_deadline_at"
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<MaterialUploadSession id={self.id} filename={self.filename!r}>"


class Material(Base):
    """课程资料（契约 4.7 的 ``MaterialDetail``）。"""

    __tablename__ = "materials"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)

    course_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "courses.id", ondelete="CASCADE", name="fk_materials_course_id_courses"
        ),
        nullable=False,
    )

    #: 来源上传会话。唯一约束是幂等完成的数据库防线：
    #: 并发重复完成会被它拦下（应用层另用行锁兜一次）。
    upload_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "material_upload_sessions.id",
            ondelete="RESTRICT",
            name="fk_materials_upload_id_material_upload_sessions",
        ),
        nullable=False,
    )

    filename: Mapped[str] = mapped_column(String(FILENAME_MAX_LENGTH), nullable=False)

    content_type: Mapped[str] = mapped_column(
        String(CONTENT_TYPE_MAX_LENGTH), nullable=False
    )

    size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    sha256: Mapped[str] = mapped_column(String(SHA256_HEX_LENGTH), nullable=False)

    #: 对象存储中的键；冗余保存，便于上传会话被清理后仍能定位对象
    storage_key: Mapped[str] = mapped_column(
        String(OBJECT_KEY_MAX_LENGTH), nullable=False
    )

    #: 完成确认后即为 PROCESSING；第一版没有 Worker，状态不会自动推进
    status: Mapped[MaterialStatus] = mapped_column(
        SAEnum(MaterialStatus, name="material_status", native_enum=True),
        nullable=False,
        default=MaterialStatus.PROCESSING,
    )

    #: 上传教师（契约 4.7 的 uploaded_by）
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="RESTRICT", name="fk_materials_uploaded_by_users"),
        nullable=False,
    )

    #: 失败原因的安全摘要；非 FAILED 时为 NULL
    error_message: Mapped[str | None] = mapped_column(
        String(MATERIAL_ERROR_MAX_LENGTH), nullable=True
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
        # 一次上传只产生一条资料：重复完成的最终防线
        UniqueConstraint("upload_id", name="uq_materials_upload_id"),
        UniqueConstraint("storage_key", name="uq_materials_storage_key"),
        Index("ix_materials_course_id", "course_id"),
        Index("ix_materials_uploaded_by", "uploaded_by"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 仅用于调试
        return f"<Material id={self.id} status={self.status.value}>"
