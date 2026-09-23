"""Materials 业务规则与事务边界。

对应 ``docs/api-contract.md`` 第 4 节的初始化（4.3）与完成（4.5）、第 5 节的
列表（5.1）、删除（5.2）、重试解析（5.3）与大纲查询（5.4）。

关键约定：

- **检查顺序固定**：认证 → 课程存在（404）→ 创建教师（403）→ 课程未归档（409）
  → 上传会话存在且属于本课程（404）→ 已完成则幂等返回 → 会话未过期（422）
  → 对象确认（422/503）→ 创建。同时违反多条规则时以该顺序为准。
- **读路径统一 404**：资料不存在、已删除或当前用户不是课程成员时返回同一个
  ``RESOURCE_NOT_FOUND``，不区分「不存在」与「不可见」。
- **校验分层**：请求的结构问题由 pydantic 转成 ``VALIDATION_ERROR``；
  文件类型、MIME、大小、sha256、对象一致性与到期等业务规则抛
  ``UPLOAD_INVALID`` + ``details.reason``。
- **不接收文件内容**：只签发预签名地址与读取对象元数据，字节流由浏览器直传；
  解析 Worker 是唯一的服务端读对象方（契约 5.5）。
- **事务边界在本层**：初始化提交成功后才下发预签名地址；完成时资料、任务与
  会话完成标记在同一事务内落库，重复完成（含并发）返回同一份结果。
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    AiJobFailedError,
    InternalError,
    MaterialAlreadyReadyError,
    MaterialNotReadyError,
    ResourceNotFoundError,
    RoleForbiddenError,
    UploadInvalidError,
)
from app.core.pagination import PaginationParams
from app.core.time import isoformat_z, utc_now
from app.modules.auth.models import User, UserRole
from app.modules.courses import service as courses_service
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobStatusValue
from app.modules.jobs.schemas import JobStatus
from app.modules.materials import repository as repo
from app.modules.materials.models import (
    FILENAME_MAX_LENGTH,
    Material,
    MaterialDeleteStatus,
    MaterialKnowledgePoint,
    MaterialSection,
    MaterialStatus,
    MaterialUploadSession,
)
from app.modules.materials.schemas import (
    CANONICAL_CONTENT_TYPE_BY_EXTENSION,
    SHA256_HEX_LENGTH,
    SUPPORTED_EXTENSIONS,
    MaterialDetail,
    MaterialUploadCompleteResponse,
    MaterialUploadInitRequest,
)
from app.storage import (
    PresignedUpload,
    S3Storage,
    StorageConfigError,
    StorageObjectNotFoundError,
    StorageUnavailableError,
    as_service_unavailable,
    build_upload_object_key,
)

logger = logging.getLogger("app.materials")

#: 文件名中不允许出现的字符（路径分隔符与空字节），避免键或元数据被污染
_FORBIDDEN_FILENAME_CHARS = ("/", "\\", "\x00")

#: sha256 的十六进制格式
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class ValidatedUpload(NamedTuple):
    """通过业务校验的上传元数据。"""

    filename: str
    content_type: str
    size: int
    sha256: str
    #: 由规范 MIME 推导出的扩展名，用于生成对象键
    extension: str


class CompletionResult(NamedTuple):
    """完成上传的结果。

    :param response: 完成响应（首次为新建资料 + 任务；重放为**首次快照**，
        即使资料此后被删除或解析状态已变化也保持原样，契约 4.6 / 5.2）
    :param created: 本次是否真的创建了资料（``False`` 表示幂等重放）
    """

    response: MaterialUploadCompleteResponse
    created: bool


def _upload_invalid(
    reason: str,
    message: str,
    *,
    field: str | None = None,
    **extra: object,
) -> UploadInvalidError:
    """构造带稳定原因码的 ``UPLOAD_INVALID`` 错误（契约 4.8）。"""
    details: dict[str, object] = {"reason": reason}
    if field is not None:
        details["field"] = field
    details.update(extra)
    return UploadInvalidError(message, details=details)


def split_extension(filename: str) -> str:
    """取文件名的扩展名（含点，小写）；没有扩展名时返回空字符串。"""
    index = filename.rfind(".")
    if index < 0:
        return ""
    return filename[index:].lower()


def validate_upload_request(
    payload: MaterialUploadInitRequest, *, max_bytes: int
) -> ValidatedUpload:
    """业务校验：文件名、类型白名单、大小范围与 sha256 格式。

    顺序为 文件名 → 扩展名 → MIME → 大小 → sha256，与契约 4.2 的排列一致，
    这样同时违规时返回的是最靠前的那条原因。

    :raises UploadInvalidError: 任一规则不满足。
    """
    filename = payload.filename.strip()
    if (
        not filename
        or len(filename) > FILENAME_MAX_LENGTH
        or any(char in filename for char in _FORBIDDEN_FILENAME_CHARS)
    ):
        raise _upload_invalid(
            "FILE_TYPE_NOT_ALLOWED",
            f"文件名不合法：需为 1–{FILENAME_MAX_LENGTH} 个字符，且不含路径分隔符",
            field="filename",
        )

    extension = split_extension(filename)
    if extension not in SUPPORTED_EXTENSIONS:
        raise _upload_invalid(
            "FILE_TYPE_NOT_ALLOWED",
            "仅支持 .pdf、.pptx、.docx 文件",
            field="filename",
            supported_extensions=sorted(SUPPORTED_EXTENSIONS),
        )

    expected_content_type = CANONICAL_CONTENT_TYPE_BY_EXTENSION[extension]
    if payload.content_type.strip() != expected_content_type:
        raise _upload_invalid(
            "CONTENT_TYPE_MISMATCH",
            f"{extension} 对应的规范 MIME 必须是 {expected_content_type}",
            field="content_type",
            expected_content_type=expected_content_type,
        )

    if payload.size < 1 or payload.size > max_bytes:
        raise _upload_invalid(
            "SIZE_OUT_OF_RANGE",
            f"文件大小必须在 1 字节到 {max_bytes} 字节之间",
            field="size",
            max_size_bytes=max_bytes,
        )

    sha256 = payload.sha256.strip().lower()
    if not _SHA256_PATTERN.match(sha256):
        raise _upload_invalid(
            "SHA256_INVALID",
            f"sha256 必须是 {SHA256_HEX_LENGTH} 位十六进制字符串",
            field="sha256",
        )

    return ValidatedUpload(
        filename=filename,
        content_type=expected_content_type,
        size=payload.size,
        sha256=sha256,
        extension=extension,
    )


async def require_upload_teacher(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
):
    """初始化与完成的公共前置检查：课程存在、创建教师、课程未归档。"""
    course = await courses_service.require_course_teacher(
        session, user=user, course_id=course_id
    )
    courses_service.require_course_active(course)
    return course


async def init_upload(
    session: AsyncSession,
    *,
    user: User,
    course_id: uuid.UUID,
    payload: MaterialUploadInitRequest,
    settings: Settings,
    storage: S3Storage,
) -> tuple[MaterialUploadSession, PresignedUpload]:
    """初始化上传：校验 → 生成对象键 → 签发预签名地址 → 落库。

    返回 ``(上传会话, 预签名信息)``。预签名是**纯本地计算**，不会访问对象存储；
    会话必须提交成功后才返回 ``201``，落库失败由异常处理器转成 ``500``，
    绝不向客户端下发作废的地址。
    """
    course = await require_upload_teacher(session, user=user, course_id=course_id)
    validated = validate_upload_request(
        payload, max_bytes=settings.material_max_upload_bytes
    )

    upload_id = uuid.uuid4()
    # 对象键由后端生成：课程 UUID + 上传会话 UUID + 规范扩展名，不含用户文件名
    object_key = build_upload_object_key(course.id, upload_id, validated.extension)

    now = utc_now()
    try:
        presigned = storage.create_presigned_put(
            object_key,
            content_type=validated.content_type,
            sha256_hex=validated.sha256,
            expires_in=settings.storage_upload_url_ttl_seconds,
            now=now,
        )
    except StorageUnavailableError as exc:
        raise as_service_unavailable(exc) from exc
    except StorageConfigError as exc:  # pragma: no cover - 入参已被校验
        logger.error("预签名参数不合法：%s", exc)
        raise InternalError() from exc

    upload = repo.add_upload_session(
        session,
        upload_id=upload_id,
        course_id=course.id,
        teacher_id=user.id,
        object_key=object_key,
        filename=validated.filename,
        content_type=validated.content_type,
        size=validated.size,
        sha256=validated.sha256,
        upload_url_expires_at=presigned.expires_at,
        confirm_deadline_at=now
        + timedelta(seconds=settings.material_upload_confirm_ttl_seconds),
        now=now,
    )
    await session.commit()
    return upload, presigned


async def complete_upload(
    session: AsyncSession,
    *,
    user: User,
    course_id: uuid.UUID,
    upload_id: uuid.UUID,
    storage: S3Storage,
) -> CompletionResult:
    """完成上传：确认对象 → 创建资料与解析任务（重复完成幂等）。

    :raises ResourceNotFoundError: 上传会话不存在或不属于该课程。
    :raises UploadInvalidError: 会话已过期、对象缺失、大小/类型/校验值不符。
    :raises ServiceUnavailableError: 对象存储不可用。
    """
    course = await require_upload_teacher(session, user=user, course_id=course_id)

    # 锁住上传会话行：幂等判定与写入必须在同一临界区
    upload = await repo.get_upload_session_for_update(session, upload_id)
    if upload is None or upload.course_id != course.id:
        raise ResourceNotFoundError()

    if upload.completed_material_id is not None:
        # 已完成：重复确认返回首次响应快照，且不受确认窗口限制（契约 4.6）。
        # 快照保证资料此后被删除或解析状态变化时，重复确认仍返回首次结果。
        return await _load_completed(session, upload)

    now = utc_now()
    if upload.confirm_deadline_at <= now:
        raise _upload_invalid(
            "UPLOAD_EXPIRED",
            "上传确认窗口已过（初始化后 24 小时内需完成确认），请重新初始化上传",
            # details 必须是可 JSON 序列化的值：时间统一输出 ...Z 字符串
            confirm_deadline_at=isoformat_z(upload.confirm_deadline_at),
        )

    _verify_stored_object(storage, upload)

    material_id = uuid.uuid4()
    material = repo.add_material(
        session,
        material_id=material_id,
        course_id=upload.course_id,
        upload_id=upload.id,
        filename=upload.filename,
        content_type=upload.content_type,
        size=upload.size,
        sha256=upload.sha256,
        storage_key=upload.object_key,
        # 第一版没有 Worker：资料如实停在 PROCESSING，任务停在 PENDING
        status=MaterialStatus.PROCESSING,
        uploaded_by=user.id,
        now=now,
    )
    job = jobs_service.create_material_parse_job(
        session, material_id=material_id, now=now
    )
    upload.completed_material_id = material_id
    upload.completed_at = now
    upload.updated_at = now

    # 完成响应快照：重复确认（含资料删除/状态变化后）一律回填首次结果
    snapshot = MaterialUploadCompleteResponse(
        material=MaterialDetail.model_validate(material),
        job=JobStatus.model_validate(job),
    )
    repo.save_completion_snapshot(
        session, upload, snapshot=snapshot.model_dump(mode="json")
    )

    try:
        await session.commit()
    except IntegrityError:
        # 唯一约束兜底：并发重复完成时按幂等返回已存在的那一份
        await session.rollback()
        existing = await repo.get_material_by_upload_id(session, upload_id)
        if existing is None:
            raise
        existing_upload = await repo.get_upload_session(session, upload_id)
        if existing_upload is None:  # pragma: no cover - 外键保证存在
            raise ResourceNotFoundError()
        logger.warning("并发重复完成上传，返回已创建的资料（upload_id=%s）", upload_id)
        return await _load_completed(session, existing_upload)

    return CompletionResult(response=snapshot, created=True)


def _verify_stored_object(storage: S3Storage, upload: MaterialUploadSession) -> None:
    """对象确认：核对存在性、大小、内容类型与存储侧校验值（契约 4.5）。

    不接收文件内容，只读取对象元数据；**不使用 ETag 代替 SHA-256**。
    存储侧未返回校验值时只记日志（部分兼容实现不返回该头），不因此拒绝上传。
    """
    try:
        stored = storage.head_object(upload.object_key)
    except StorageObjectNotFoundError as exc:
        raise _upload_invalid(
            "OBJECT_MISSING",
            "对象存储中未找到已上传的文件，请先按预签名地址上传",
        ) from exc
    except StorageUnavailableError as exc:
        raise as_service_unavailable(exc) from exc

    if stored.size != upload.size:
        raise _upload_invalid(
            "OBJECT_SIZE_MISMATCH",
            "已上传对象的大小与初始化声明不一致",
            declared_size=upload.size,
            actual_size=stored.size,
        )

    if stored.content_type and stored.content_type != upload.content_type:
        raise _upload_invalid(
            "OBJECT_TYPE_MISMATCH",
            "已上传对象的类型与初始化声明不一致",
            declared_content_type=upload.content_type,
            actual_content_type=stored.content_type,
        )

    stored_sha256 = stored.checksum_sha256_hex()
    if stored_sha256 is None:
        logger.warning(
            "对象存储未返回 x-amz-checksum-sha256，跳过内容摘要比对（key=%s）",
            upload.object_key,
        )
    elif stored_sha256 != upload.sha256:
        raise _upload_invalid(
            "CHECKSUM_MISMATCH",
            "已上传对象的 SHA-256 与初始化声明不一致",
            declared_sha256=upload.sha256,
            actual_sha256=stored_sha256,
        )


async def _load_completed(
    session: AsyncSession, upload: MaterialUploadSession
) -> CompletionResult:
    """读取已完成上传的响应：优先回填首次快照（契约 4.6 / 5.2）。

    快照是重复确认的唯一事实来源——资料此后被删除或解析状态变化都不影响
    重复完成的结果。快照缺失（旧数据）时从当前资料与任务构造并补存。
    """
    snapshot = await repo.get_completion_snapshot(session, upload.id)
    if snapshot is not None:
        return CompletionResult(
            response=MaterialUploadCompleteResponse.model_validate(snapshot),
            created=False,
        )

    material = await repo.get_material_by_id(session, upload.completed_material_id)
    if material is None:  # pragma: no cover - 外键保证存在
        raise InternalError()
    job = await jobs_service.get_material_parse_job(session, material_id=material.id)
    if job is None:  # pragma: no cover - 同一事务内创建
        raise InternalError()
    response = MaterialUploadCompleteResponse(
        material=MaterialDetail.model_validate(material),
        job=JobStatus.model_validate(job),
    )
    repo.save_completion_snapshot(
        session, upload, snapshot=response.model_dump(mode="json")
    )
    await session.commit()
    return CompletionResult(response=response, created=False)


async def get_material_for_member(
    session: AsyncSession, *, user: User, material_id: uuid.UUID
) -> Material:
    """读取资料详情（契约 4.7）。

    资料不存在（**含已删除**，契约 5.2），或当前用户不是资料所属课程的成员时，
    统一抛 ``ResourceNotFoundError``（404），不区分「不存在」与「不可见」，
    避免用于枚举资源。归档课程的资料仍可读。
    """
    material = await repo.get_visible_material_by_id(session, material_id)
    if material is None:
        raise ResourceNotFoundError()
    if not await courses_service.is_course_member(
        session, user=user, course_id=material.course_id
    ):
        raise ResourceNotFoundError()
    return material


# --------------------------------------------------------------------------- #
# 契约 5.1–5.4：列表、删除、重试解析、大纲查询
# --------------------------------------------------------------------------- #
async def list_course_materials(
    session: AsyncSession,
    *,
    user: User,
    course_id: uuid.UUID,
    pagination: PaginationParams,
) -> tuple[list[Material], int]:
    """课程资料列表（契约 5.1）。

    课程不存在或当前用户不是成员时统一 404（与 4.7 的读路径一致）；
    含归档课程；已删除资料由 repository 层排除。
    """
    if not await courses_service.is_course_member(
        session, user=user, course_id=course_id
    ):
        raise ResourceNotFoundError()
    return await repo.list_course_materials(
        session,
        course_id=course_id,
        offset=pagination.offset,
        limit=pagination.limit,
    )


async def require_material_manager(
    session: AsyncSession, *, user: User, material: Material
) -> None:
    """资料管理权限（契约 5.2、5.3）：仅创建教师，且课程未归档。

    调用前提：资料存在、当前用户是资料所属课程成员（否则先抛 404）。
    供删除、重试解析与 Jobs 重试分派（契约 10.2 的 `MATERIAL_PARSE` 分支）复用：
    顺序固定为 **成员可见性 → 角色 → 归档**，因此非成员无法借重试接口探测任务类型。
    """
    if user.role == UserRole.STUDENT:
        raise RoleForbiddenError()
    course = await courses_service.require_course_teacher(
        session, user=user, course_id=material.course_id
    )
    courses_service.require_course_active(course)


async def delete_material(
    session: AsyncSession,
    *,
    user: User,
    material_id: uuid.UUID,
) -> bool:
    """删除资料（契约 5.2，标记删除 + 对象删除待办）。

    返回 ``True`` 表示本次真正执行了标记删除；``False`` 表示幂等重放
    （同一创建教师对已删除资料的再次删除）。

    处理顺序：资料存在（404）→ 成员（404）→ 已删除幂等/不可见（204/404）
    → 角色（403）→ 归档（409）→ 事务内删除。

    事务内容：标记删除（隐藏资料）→ 取消未完成解析（任务行锁内置
    ``CANCELLED``）→ 清空章节与知识点 → **清空可检索片段（6.1：删除后的资料
    不得再被检索）** → 写入对象删除待办。对象本身由
    独立维护命令在 PUT 地址过期 + 缓冲期后删除（避免晚到 PUT 重建窗口）；
    上传会话与资料行（最小删除记录）保留供审计。
    """
    material = await repo.get_material_by_id(session, material_id)
    if material is None:
        raise ResourceNotFoundError()
    if not await courses_service.is_course_member(
        session, user=user, course_id=material.course_id
    ):
        raise ResourceNotFoundError()

    if material.deleted_at is not None:
        # 已删除：仅创建教师可见幂等 204；其余用户视同不存在
        if material.uploaded_by != user.id:
            raise ResourceNotFoundError()
        return False

    await require_material_manager(session, user=user, material=material)

    locked = await repo.get_visible_material_for_update(session, material_id)
    if locked is None:  # pragma: no cover - 并发删除的兜底，按幂等处理
        return False

    now = utc_now()

    # 取消未完成解析：锁住关联任务行，与 Worker 领取/回写互斥
    job = await jobs_service.lock_material_parse_job(session, material_id=locked.id)
    if job is not None:
        jobs_service.cancel_material_parse_job(session, job, now=now)

    upload = await repo.get_upload_session(session, locked.upload_id)
    if upload is None:  # pragma: no cover - 外键保证存在
        raise ResourceNotFoundError()

    repo.mark_material_deleted(session, locked, now=now)
    # 章节与知识点级联清空；检索片段必须一并删除，否则删除后的资料仍会被问答检索到
    await repo.delete_sections(session, material_id=locked.id)
    await repo.delete_chunks(session, material_id=locked.id)
    repo.add_delete_todo(
        session,
        todo_id=uuid.uuid4(),
        material_id=locked.id,
        course_id=locked.course_id,
        object_key=locked.storage_key,
        upload_expires_at=upload.upload_url_expires_at,
        now=now,
    )
    await session.commit()
    return True


async def retry_parse(
    session: AsyncSession,
    *,
    user: User,
    material_id: uuid.UUID,
) -> tuple[Job, Material, bool]:
    """重试解析（契约 5.3）。

    返回 ``(任务, 资料, 是否重置)``；重置后由调用方调度 Worker。

    分流（契约 5.3 表格）：``READY`` → 409；``PROCESSING`` 且任务
    ``PENDING``/``RUNNING`` → 幂等原样返回；其余（任务 ``FAILED``、
    任务 ``SUCCEEDED`` 但资料未 ``READY`` 的异常窗口）→ 行锁内重置。
    """
    material = await repo.get_visible_material_by_id(session, material_id)
    if material is None:
        raise ResourceNotFoundError()
    if not await courses_service.is_course_member(
        session, user=user, course_id=material.course_id
    ):
        raise ResourceNotFoundError()

    await require_material_manager(session, user=user, material=material)

    locked = await repo.get_visible_material_for_update(session, material_id)
    if locked is None:  # pragma: no cover - 并发删除后按 404 处理
        raise ResourceNotFoundError()

    if locked.status == MaterialStatus.READY:
        raise MaterialAlreadyReadyError()

    # 锁定关联任务行：与删除（取消解析）和 Worker 回写互斥
    job = await jobs_service.lock_material_parse_job(session, material_id=locked.id)
    if job is None:  # pragma: no cover - 完成事务保证任务存在
        raise InternalError()

    now = utc_now()

    def _lease_still_valid() -> bool:
        """RUNNING 任务是否仍在租约内（执行者还活着）。"""
        return (
            job.lease_expires_at is not None and job.lease_expires_at > now
        )

    if (
        locked.status == MaterialStatus.PROCESSING
        and job.status == JobStatusValue.PENDING
    ) or (
        locked.status == MaterialStatus.PROCESSING
        and job.status == JobStatusValue.RUNNING
        and _lease_still_valid()
    ):
        return job, locked, False

    # 重试：复用原任务 ID，清空全部执行痕迹并**撤销旧执行者的运行令牌**
    # （契约 5.3）：清空 ``run_token`` 与租约后，任何携带旧令牌的回写都会
    # 因令牌/状态不匹配被拒绝，旧执行者不能再改变任务、资料或解析产物。
    # 覆盖：FAILED 任务、崩溃后的 RUNNING 租约过期（执行者失联）等。
    job.status = JobStatusValue.PENDING
    job.progress = 0
    job.error = None
    job.started_at = None
    job.finished_at = None
    job.run_token = None
    job.lease_expires_at = None
    locked.status = MaterialStatus.PROCESSING
    locked.error_message = None
    locked.updated_at = now
    await session.commit()
    return job, locked, True


async def cleanup_deleted_materials(
    session: AsyncSession,
    *,
    storage: S3Storage,
    settings: Settings,
    now: datetime | None = None,
    limit: int | None = None,
) -> tuple[int, int]:
    """处理对象删除待办（契约 5.2 的删除流水线，独立维护命令调用）。

    仅处理「原 PUT 地址过期 + 缓冲期」已过的待办——此前浏览器仍可能
    拿着原地址直传，立即删除会留下重建窗口（``If-None-Match: *`` 在对象
    不存在时放行晚到 PUT）。

    每条待办的处理：

    1. 删除对象（不存在则跳过）；
    2. **再次核查晚到 PUT**：HeadObject 确认对象不再出现，通过后标记
       ``DONE``；仍存在说明删除与核查之间有晚到 PUT 重建了对象，
       保持 ``PENDING`` 由下一轮继续清理（失败持续重试）；
    3. 存储不可用：递增 ``attempts``、记录 ``last_error``，下轮重试。

    :returns: ``(本轮标记 DONE 的数量, 仍处 PENDING 的数量)``。
    """
    current = now or utc_now()
    todos = await repo.list_due_delete_todos(
        session, now=current, buffer_seconds=settings.material_delete_buffer_seconds
    )

    done = 0
    pending = 0
    for todo in todos[: None if limit is None else limit]:
        todo.attempts += 1
        try:
            try:
                storage.delete_object(todo.object_key)
            except StorageObjectNotFoundError:
                pass
            # 晚到 PUT 核查：删除之后对象必须不再出现
            storage.head_object(todo.object_key)
        except StorageObjectNotFoundError:
            todo.status = MaterialDeleteStatus.DONE
            todo.verified_at = current
            todo.last_error = None
            done += 1
        except StorageUnavailableError as exc:
            todo.last_error = "对象存储暂时不可用，将在下一轮重试"
            logger.warning(
                "删除对象失败，待办保留（key=%s）：%s", todo.object_key, exc
            )
            pending += 1
        else:
            todo.last_error = "检测到晚到写入，将在下一轮继续清理"
            logger.warning("对象删除后再次出现（晚到 PUT），继续重试（key=%s）", todo.object_key)
            pending += 1

    await session.commit()
    return done, pending


async def get_material_outline(
    session: AsyncSession, *, user: User, material_id: uuid.UUID
) -> tuple[Material, list[tuple[MaterialSection, list[MaterialKnowledgePoint]]]]:
    """大纲查询（契约 5.4）：只对 ``READY`` 资料返回解析产物。

    ``PROCESSING``（含排队）→ 409 ``MATERIAL_NOT_READY``；
    ``FAILED`` → 502 ``AI_JOB_FAILED``（``details.job_id`` 指向解析任务）。
    """
    material = await get_material_for_member(session, user=user, material_id=material_id)

    if material.status == MaterialStatus.FAILED:
        job = await jobs_service.get_material_parse_job(session, material_id=material.id)
        details = {"job_id": str(job.id)} if job is not None else {}
        raise AiJobFailedError(
            "资料解析失败，无法查看大纲",
            details=details,
        )
    if material.status != MaterialStatus.READY:
        raise MaterialNotReadyError()

    sections = await repo.list_sections_with_points(session, material_id=material.id)
    return material, sections


async def cleanup_expired_uploads(
    session: AsyncSession,
    *,
    storage: S3Storage,
    now: datetime | None = None,
    limit: int | None = None,
) -> int:
    """清理超过确认窗口仍未完成的上传会话（契约 4.6）。

    返回本次清理标记为过期的会话数量。

    规则：

    - 只处理 ``confirm_deadline_at <= now`` 且 ``completed_material_id IS NULL``
      且 ``expired_at IS NULL`` 的会话；已完成会话及其资料绝不删除；
    - 对每个会话：删除对象存储中的孤立对象（对象本就不存在时忽略），
      然后把 ``expired_at`` 标记为当前时间；
    - 可重复执行：已标记 ``expired_at`` 的会话不再被扫描，删除对象天然幂等；
    - 用 ``FOR UPDATE SKIP LOCKED`` 锁定会话行，与并发完成请求互斥：
      完成请求先拿到锁并写入 ``completed_material_id`` 时，本函数读不到该行
      （条件里 ``completed_material_id IS NULL``），不会误删已确认对象。

    :returns: 本次标记为过期的会话数。
    """
    current = now or utc_now()
    batch = limit if limit is not None else repo.CLEANUP_BATCH_LIMIT
    expired = await repo.list_expired_incomplete_uploads(
        session, now=current, limit=batch
    )

    cleaned = 0
    for upload in expired:
        try:
            storage.delete_object(upload.object_key)
        except StorageObjectNotFoundError:
            # 对象本就不存在（未直传或已删），视为已清理
            pass
        except StorageUnavailableError:
            # 存储暂时不可用：跳过本会话，下次清理再处理，
            # 不能因为个别对象删除失败就中断整批或误标过期。
            logger.warning(
                "删除过期上传对象失败，跳过本次清理（key=%s）", upload.object_key
            )
            continue
        repo.mark_upload_expired(session, upload, now=current)
        cleaned += 1

    if cleaned:
        await session.commit()
    return cleaned


__all__ = [
    "CompletionResult",
    "ValidatedUpload",
    "cleanup_expired_uploads",
    "complete_upload",
    "delete_material",
    "get_material_for_member",
    "get_material_outline",
    "init_upload",
    "list_course_materials",
    "require_material_manager",
    "require_upload_teacher",
    "retry_parse",
    "split_extension",
    "validate_upload_request",
]
