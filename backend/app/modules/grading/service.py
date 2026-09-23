"""Grading 业务规则与事务边界（``docs/api-contract.md`` 第 9 节）。

关键约定：

- **检查顺序固定**（契约 9.1）：认证（框架）→ 资源可见性（404）→ 角色（403）
  → 课程归档（409）→ 业务状态（409）→ 请求结构（422）→ 字段与分数语义（422）
  → 数据写入。
- **可见性统一 404**：非成员、他人提交、学生视角下未发布的批改，一律
  ``404 RESOURCE_NOT_FOUND``，不区分"不存在"与"不可见"。
- **统一锁顺序**：课程 → Assignment → 提交固定的 RubricVersion → Submission
  →（UploadSession / Job / GradeReview）。所有写事务都按此顺序取锁。
- **只能提交一份**：``(assignment_id, student_id)`` 唯一约束 + 提交行锁；
  重新初始化复用 ``UPLOADING`` 提交，正式提交后返回 ``409``。
- **评分版本固定**：完成提交时写入当时的 ``current_rubric_version_id``，
  批改与复核只读提交引用的版本，绝不读取任务当前版本。
- 事务边界在本层：repository 只暂存对象，service 决定何时 ``commit``。
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import (
    AiJobFailedError,
    AssignmentNotOpenError,
    CourseForbiddenError,
    GradeAlreadyPublishedError,
    GradeNotReviewedError,
    InternalError,
    ResourceNotFoundError,
    RoleForbiddenError,
    SubmissionAlreadyExistsError,
    SubmissionNotReadyError,
    UploadInvalidError,
)
from app.core.pagination import PaginationParams
from app.core.time import isoformat_z, utc_now
from app.modules.assignments import repository as assignments_repo
from app.modules.assignments import service as assignments_service
from app.modules.assignments.models import (
    Assignment,
    AssignmentRubricItem,
    AssignmentRubricVersion,
)
from app.modules.auth.models import User, UserRole
from app.modules.courses import repository as courses_repo
from app.modules.courses import service as courses_service
from app.modules.courses.models import Course
from app.modules.grading import repository as repo
from app.modules.grading.models import (
    FILENAME_MAX_LENGTH,
    GradeItem,
    GradeReview,
    Submission,
    SubmissionStatus,
    SubmissionUploadSession,
)
from app.modules.grading.schemas import (
    CANONICAL_CONTENT_TYPE_BY_EXTENSION,
    SHA256_HEX_LENGTH,
    SUPPORTED_EXTENSIONS,
    GradeItemDetailSchema,
    GradeReviewDetailSchema,
    GradeReviewUpdateRequest,
    SubmissionDetailSchema,
    SubmissionSummarySchema,
    SubmissionUploadInitRequest,
    ensure_review_covers_rubric,
)
from app.modules.jobs import service as jobs_service
from app.modules.jobs.models import Job, JobStatusValue
from app.storage import (
    PresignedDownload,
    PresignedUpload,
    S3Storage,
    StorageConfigError,
    StorageObjectNotFoundError,
    StorageUnavailableError,
    as_service_unavailable,
    build_submission_object_key,
    presign_download,
)

logger = logging.getLogger("app.grading")

#: 文件名中不允许出现的字符（路径分隔符与空字节），避免键或元数据被污染
_FORBIDDEN_FILENAME_CHARS = ("/", "\\", "\x00")

#: sha256 的十六进制格式
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    """通过业务校验的上传元数据。"""

    filename: str
    content_type: str
    size: int
    sha256: str
    #: 由规范 MIME 推导出的扩展名，用于生成对象键
    extension: str


@dataclass(slots=True)
class SubmissionListItem:
    """列表元素：提交 + 固定的评分版本号。"""

    submission: Submission
    rubric_version: int | None


@dataclass(slots=True)
class SubmissionDetailData:
    """提交详情：提交 + 版本号 + 临时下载地址。"""

    submission: Submission
    rubric_version: int | None
    download_url: str | None = None
    download_expires_at: datetime | None = None


@dataclass(slots=True)
class GradeReviewData:
    """批改详情：批改记录 + 评分项 + 是否为"学生已发布"视角。"""

    review: GradeReview
    items: list[GradeItem] = field(default_factory=list)
    student_view: bool = False


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """完成提交的结果。

    :param response: 完成响应（契约 9.3 固定为 ``SubmissionDetail``；
        重放以**首次快照**为准，下载地址现场重新签发）
    :param created: 本次是否真的完成了提交（``False`` 表示幂等重放）
    """

    response: SubmissionDetailSchema
    created: bool


def _upload_invalid(
    reason: str,
    message: str,
    *,
    field: str | None = None,
    **extra: object,
) -> UploadInvalidError:
    """构造带稳定原因码的 ``UPLOAD_INVALID`` 错误（契约 4.8 / 9.2）。"""
    details: dict[str, object] = {"reason": reason}
    if field is not None:
        details["field"] = field
    details.update(extra)
    return UploadInvalidError(message, details=details)


#: ``submission_upload_sessions`` 的部分唯一索引：
#: 每份 Submission 最多只有一个**完成**的上传会话（契约 9.3）
COMPLETION_UNIQUE_INDEX = "uq_submission_upload_sessions_submission_completed"


def _is_submission_completed_conflict(exc: IntegrityError) -> bool:
    """判断唯一约束冲突是否来自"每份提交只能有一个完成会话"。

    只识别这一个**命名**约束；其他数据库错误必须原样抛出，
    绝不能被吞掉后伪装成成功的幂等响应（契约 9.3）。
    """
    name = getattr(exc.orig, "constraint_name", None) or ""
    if not name:
        name = COMPLETION_UNIQUE_INDEX if COMPLETION_UNIQUE_INDEX in str(exc) else ""
    return name == COMPLETION_UNIQUE_INDEX


def split_extension(filename: str) -> str:
    """取文件名的扩展名（含点，小写）；没有扩展名时返回空字符串。"""
    index = filename.rfind(".")
    if index < 0:
        return ""
    return filename[index:].lower()


def validate_upload_request(
    payload: SubmissionUploadInitRequest, *, max_bytes: int
) -> ValidatedUpload:
    """业务校验：文件名、类型白名单（PDF/DOCX）、MIME、大小与 sha256 格式。

    顺序为 文件名 → 扩展名 → MIME → 大小 → sha256，与第 4 节上传协议一致，
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
        # 明确拒绝旧版 .doc 与其他类型（契约 9.2）
        raise _upload_invalid(
            "FILE_TYPE_NOT_ALLOWED",
            "仅支持 .pdf、.docx 文件（不接受旧版 .doc）",
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


# --------------------------------------------------------------------------- #
# 锁与权限（守卫依赖调用）
# --------------------------------------------------------------------------- #
async def _lock_course(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
) -> Course:
    """锁课程行 → 成员可见性（404）。"""
    course = await courses_repo.get_course_for_update(session, course_id)
    if course is None:
        raise ResourceNotFoundError()
    if not await courses_service.is_course_member(
        session, user=user, course_id=course.id
    ):
        raise ResourceNotFoundError()
    return course


def _require_course_creator(course: Course, *, user: User) -> None:
    """写接口的课程创建教师检查：学生 ``403 ROLE_FORBIDDEN``，其他教师 ``403 COURSE_FORBIDDEN``。"""
    if user.role is UserRole.STUDENT:
        raise RoleForbiddenError()
    if course.teacher_id != user.id:
        raise CourseForbiddenError()


async def lock_student_course(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
) -> Course:
    """上传接口的守卫：锁课程 → 成员（404）→ 仅学生（403）→ 未归档（409）。"""
    course = await _lock_course(session, user=user, course_id=course_id)
    if user.role is not UserRole.STUDENT:
        raise RoleForbiddenError()
    courses_service.require_course_active(course)
    return course


async def lock_creator_course(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
) -> Course:
    """批改/复核/发布接口的守卫：锁课程 → 成员（404）→ 创建教师（403）→ 未归档（409）。"""
    course = await _lock_course(session, user=user, course_id=course_id)
    _require_course_creator(course, user=user)
    courses_service.require_course_active(course)
    return course


async def _read_visible_submission(
    session: AsyncSession, *, user: User, submission_id: uuid.UUID
) -> tuple[Submission, Course]:
    """读取提交并完成成员可见性检查（非成员统一 404）。"""
    submission = await repo.get_submission_by_id(session, submission_id)
    if submission is None:
        raise ResourceNotFoundError()
    course = await courses_repo.get_course_by_id(session, submission.course_id)
    if course is None:  # pragma: no cover - 外键保证存在
        raise ResourceNotFoundError()
    if not await courses_service.is_course_member(
        session, user=user, course_id=course.id
    ):
        raise ResourceNotFoundError()
    return submission, course


async def require_submission_viewer(
    session: AsyncSession, *, user: User, submission_id: uuid.UUID
) -> Submission:
    """读取提交的可见性检查（任务状态查询复用，契约 9.5 / 第 10 节）。

    可见者：课程创建教师与提交本人；其余一律 ``404``（不区分不存在与不可见）。
    """
    submission, course = await _read_visible_submission(
        session, user=user, submission_id=submission_id
    )
    if course.teacher_id == user.id:
        return submission
    if submission.student_id == user.id:
        return submission
    raise ResourceNotFoundError()


async def _lock_assignment(
    session: AsyncSession, assignment_id: uuid.UUID
) -> Assignment:
    """锁住任务行（课程行必须在之前已锁定）。"""
    assignment = await assignments_repo.lock_assignment(session, assignment_id)
    if assignment is None:
        raise ResourceNotFoundError()
    return assignment


@dataclass(frozen=True, slots=True)
class StudentAssignmentGuard:
    """初始化上传的守卫结果（资源检查 + 行锁已完成）。"""

    course: Course
    assignment: Assignment
    submission: Submission | None


@dataclass(frozen=True, slots=True)
class StudentUploadGuard:
    """完成提交的守卫结果。"""

    course: Course
    assignment: Assignment
    submission: Submission
    upload: SubmissionUploadSession
    rubric_version: AssignmentRubricVersion | None


@dataclass(frozen=True, slots=True)
class CreatorSubmissionGuard:
    """教师按提交操作的守卫结果（触发批改）。"""

    course: Course
    assignment: Assignment
    submission: Submission


@dataclass(frozen=True, slots=True)
class CreatorReviewGuard:
    """教师复核/发布的守卫结果。"""

    course: Course
    assignment: Assignment
    submission: Submission
    review: GradeReview


async def lock_student_assignment(
    session: AsyncSession, *, user: User, assignment_id: uuid.UUID
) -> StudentAssignmentGuard:
    """初始化上传的守卫：课程 → Assignment → 当前 RubricVersion → Submission。

    检查顺序：任务存在（404）→ 成员可见性（404）→ 仅学生（403）→ 课程未归档
    （409）→ 仍允许提交（409）。请求体校验排在其后（契约 9.1）。
    """
    assignment = await assignments_repo.get_assignment_by_id(session, assignment_id)
    if assignment is None:
        raise ResourceNotFoundError()
    course = await lock_student_course(
        session, user=user, course_id=assignment.course_id
    )
    locked = await _lock_assignment(session, assignment_id)
    await assignments_service.lock_current_rubric_version(session, assignment=locked)
    if not assignments_service.can_submit(locked, now=utc_now()):
        raise AssignmentNotOpenError()
    submission = await repo.lock_submission_for_student(
        session, assignment_id=locked.id, student_id=user.id
    )
    return StudentAssignmentGuard(
        course=course, assignment=locked, submission=submission
    )


async def lock_student_upload(
    session: AsyncSession,
    *,
    user: User,
    assignment_id: uuid.UUID,
    upload_id: uuid.UUID,
) -> StudentUploadGuard:
    """完成提交的守卫（契约 9.1 的锁顺序）：

    **课程 → Assignment → RubricVersion → Submission → UploadSession**。

    允许在加锁前**普通读取**上传会话与 Submission 的标量 ID（只用于确定要锁哪个
    评分版本），拿到前序锁后必须重新读取并复核：

    - 首次完成（提交未固定版本）→ 锁 Assignment 的**当前**评分版本；
    - 幂等重放（提交已固定版本）→ 锁提交**已固定**的评分版本。
    """
    assignment = await assignments_repo.get_assignment_by_id(session, assignment_id)
    if assignment is None:
        raise ResourceNotFoundError()
    course = await lock_student_course(
        session, user=user, course_id=assignment.course_id
    )
    locked = await _lock_assignment(session, assignment_id)

    # ---- 加锁前的普通读取：只取标量 ID，不作为业务判定依据 ----
    found = await repo.get_upload_session(session, upload_id)
    if (
        found is None
        or found.assignment_id != locked.id
        or found.student_id != user.id
    ):
        # 会话不存在、不属于该任务或不属于当前学生：统一 404
        raise ResourceNotFoundError()
    probe = await repo.get_submission_by_id(session, found.submission_id)
    if probe is None:  # pragma: no cover - 外键保证存在
        raise ResourceNotFoundError()

    # ---- RubricVersion：首次完成锁"当前版本"，重放锁"提交固定版本" ----
    if probe.rubric_version_id is not None:
        rubric_version = await assignments_repo.lock_rubric_version(
            session, probe.rubric_version_id
        )
    else:
        rubric_version = await assignments_service.lock_current_rubric_version(
            session, assignment=locked
        )

    # ---- Submission → UploadSession ----
    submission = await repo.lock_submission(session, found.submission_id)
    if submission is None:  # pragma: no cover - 外键保证存在
        raise ResourceNotFoundError()
    upload = await repo.lock_upload_session(session, upload_id)
    if upload is None:  # pragma: no cover - 上面已读到
        raise ResourceNotFoundError()

    # ---- 锁后复核：普通读取的结论必须仍然成立 ----
    if (
        upload.submission_id != submission.id
        or upload.assignment_id != locked.id
        or upload.student_id != user.id
    ):
        raise ResourceNotFoundError()
    if (
        submission.rubric_version_id is not None
        and (
            rubric_version is None
            or rubric_version.id != submission.rubric_version_id
        )
    ):
        # 普通读取后提交才固定了版本（并发完成）：补锁它固定的那个版本。
        # 评分版本只追加、写路径都先拿任务行锁，因此这里不存在交叉死锁。
        rubric_version = await assignments_repo.lock_rubric_version(
            session, submission.rubric_version_id
        )
    return StudentUploadGuard(
        course=course,
        assignment=locked,
        submission=submission,
        upload=upload,
        rubric_version=rubric_version,
    )


async def lock_creator_submission(
    session: AsyncSession, *, user: User, submission_id: uuid.UUID
) -> CreatorSubmissionGuard:
    """触发批改的守卫：课程 → Assignment → 提交固定 RubricVersion → Submission。"""
    found = await repo.get_submission_by_id(session, submission_id)
    if found is None:
        raise ResourceNotFoundError()
    course = await lock_creator_course(session, user=user, course_id=found.course_id)
    assignment = await _lock_assignment(session, found.assignment_id)
    await _lock_submission_rubric_version(session, found)
    submission = await repo.lock_submission(session, submission_id)
    if submission is None:  # pragma: no cover - 并发删除的兜底
        raise ResourceNotFoundError()
    return CreatorSubmissionGuard(
        course=course, assignment=assignment, submission=submission
    )


async def lock_creator_review(
    session: AsyncSession, *, user: User, review_id: uuid.UUID
) -> CreatorReviewGuard:
    """复核/发布的守卫：在触发批改的锁序后追加 GradeReview。"""
    found = await repo.get_grade_review_by_id(session, review_id)
    if found is None:
        raise ResourceNotFoundError()
    submission = await repo.get_submission_by_id(session, found.submission_id)
    if submission is None:  # pragma: no cover - 外键保证存在
        raise ResourceNotFoundError()

    course = await lock_creator_course(
        session, user=user, course_id=submission.course_id
    )
    assignment = await _lock_assignment(session, submission.assignment_id)
    await _lock_submission_rubric_version(session, submission)
    locked_submission = await repo.lock_submission(session, submission.id)
    if locked_submission is None:  # pragma: no cover - 并发删除的兜底
        raise ResourceNotFoundError()
    review = await repo.lock_grade_review(session, review_id)
    if review is None:  # pragma: no cover - 上面已读到
        raise ResourceNotFoundError()
    return CreatorReviewGuard(
        course=course,
        assignment=assignment,
        submission=locked_submission,
        review=review,
    )


# --------------------------------------------------------------------------- #
# 初始化上传（契约 9.2）
# --------------------------------------------------------------------------- #
async def init_submission_upload(
    session: AsyncSession,
    *,
    user: User,
    course: Course,
    assignment: Assignment,
    existing_submission: Submission | None,
    payload: SubmissionUploadInitRequest,
    settings: Settings,
    storage: S3Storage,
) -> tuple[Submission, SubmissionUploadSession, PresignedUpload]:
    """初始化报告上传：复用/创建提交，签发新的预签名 PUT 地址。

    调用方（守卫依赖）已按 **课程 → Assignment → 当前 RubricVersion → Submission**
    的顺序取锁并完成权限、归档与"仍允许提交"检查；本函数在锁内写入。
    预签名是纯本地计算，落库成功后才返回 ``201``。
    """
    if existing_submission is not None and existing_submission.submitted_at is not None:
        # 已有正式提交：不能再次初始化（契约 9.2）
        raise SubmissionAlreadyExistsError()

    validated = validate_upload_request(
        payload, max_bytes=settings.submission_max_upload_bytes
    )

    submission = existing_submission
    now = utc_now()
    # 先算出对象键再落库：完全不依赖"先插入占位键"的两步写法，
    # 也就不会在并发场景下因为占位键相同而撞上唯一约束。
    upload_id = uuid.uuid4()
    object_key = build_submission_object_key(
        course.id, assignment.id, upload_id, validated.extension
    )
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

    if submission is None:
        submission = repo.add_submission(
            session,
            submission_id=uuid.uuid4(),
            assignment_id=assignment.id,
            course_id=course.id,
            student_id=user.id,
            status=SubmissionStatus.UPLOADING,
            filename=validated.filename,
            content_type=validated.content_type,
            size=validated.size,
            sha256=validated.sha256,
            object_key=object_key,
            now=now,
        )
        # 上传会话通过外键指向提交；ORM 在缺少 relationship 时不会保证跨表插入顺序，
        # 因此先 flush 提交行，再插入会话（与 Assignments 的逐步 flush 一致）。
        await session.flush()
    else:
        # 重新初始化：复用同一份提交，把当前仍有效的旧上传会话标记为"被替代"
        for active in await repo.list_active_upload_sessions(session, submission.id):
            active.superseded_at = now
            active.updated_at = now
        submission.status = SubmissionStatus.UPLOADING
        submission.filename = validated.filename
        submission.content_type = validated.content_type
        submission.size = validated.size
        submission.sha256 = validated.sha256
        submission.object_key = object_key
        submission.error_message = None
        submission.updated_at = now

    upload = repo.add_upload_session(
        session,
        upload_id=upload_id,
        submission_id=submission.id,
        course_id=course.id,
        assignment_id=assignment.id,
        student_id=user.id,
        object_key=object_key,
        filename=validated.filename,
        content_type=validated.content_type,
        size=validated.size,
        sha256=validated.sha256,
        upload_url_expires_at=presigned.expires_at,
        confirm_deadline_at=now
        + timedelta(seconds=settings.submission_upload_confirm_ttl_seconds),
        now=now,
    )
    await session.commit()
    return submission, upload, presigned


# --------------------------------------------------------------------------- #
# 完成提交（契约 9.3）
# --------------------------------------------------------------------------- #
async def complete_submission_upload(
    session: AsyncSession,
    *,
    user: User,
    assignment: Assignment,
    upload: SubmissionUploadSession,
    submission: Submission,
    rubric_version: AssignmentRubricVersion | None,
    settings: Settings,
    storage: S3Storage,
) -> CompletionResult:
    """完成提交：确认对象 → 写入提交快照与固定评分版本（同一 upload 幂等）。

    调用方（守卫依赖）已按统一锁顺序取锁并完成权限、归档与上传会话归属检查。

    会话状态检查**全部在 HeadObject 之前**完成，优先级固定为：

    1. 该会话已完成 → 幂等返回首次快照（201）；
    2. 会话已被替代或提交的对象键不再指向它 → ``422 UPLOAD_SUPERSEDED``；
    3. 会话已清理或超过确认窗口 → ``422 UPLOAD_EXPIRED``；
    4. 提交已由其他会话正式提交 → ``409 SUBMISSION_ALREADY_EXISTS``；
    5. 提交状态不是 ``UPLOADING`` → ``409 SUBMISSION_NOT_READY``；
    6. 任务不再允许提交 → ``409 ASSIGNMENT_NOT_OPEN``；
    7. 对象确认（``422`` / ``503``）→ 写入。
    """
    # 1) 已完成：重复确认返回首次结果，且不受确认窗口限制
    if upload.completed_at is not None:
        return await _load_completed(
            session, upload, submission, settings=settings, storage=storage
        )

    now = utc_now()

    # 2) 已被新一次初始化替代（提交的对象键不再指向本会话）
    if upload.superseded_at is not None or submission.object_key != upload.object_key:
        raise _upload_invalid(
            "UPLOAD_SUPERSEDED",
            "该上传会话已被新的上传替代，请使用最新的上传会话完成提交",
            superseded_at=(
                isoformat_z(upload.superseded_at) if upload.superseded_at else None
            ),
        )

    # 3) 已被清理（expired_at）或超过确认窗口
    if upload.expired_at is not None or upload.confirm_deadline_at <= now:
        raise _upload_invalid(
            "UPLOAD_EXPIRED",
            "上传确认窗口已过或会话已被清理，请重新初始化上传",
            confirm_deadline_at=isoformat_z(upload.confirm_deadline_at),
            expired_at=(
                isoformat_z(upload.expired_at) if upload.expired_at else None
            ),
        )

    # 4) 提交已由其他上传会话正式提交：拒绝，且不改写任何字段
    if submission.submitted_at is not None:
        raise SubmissionAlreadyExistsError()

    # 5) 提交必须仍处于 UPLOADING（未完成、未开始批改）
    if submission.status is not SubmissionStatus.UPLOADING:
        raise SubmissionNotReadyError()

    # 6) 完成时再次检查任务仍允许提交
    if not assignments_service.can_submit(assignment, now=now):
        raise AssignmentNotOpenError()

    # 7) 对象确认（存在性、大小、MIME、存储侧 SHA-256）
    _verify_stored_object(storage, upload)

    # 固定评分规则版本：即使之后教师修改 Rubric 生成新版本，本提交仍按此版本批改
    version_id = assignment.current_rubric_version_id
    if version_id is None:  # pragma: no cover - 创建事务保证不为空
        raise InternalError()
    if rubric_version is None:
        rubric_version = await assignments_repo.lock_rubric_version(
            session, version_id
        )

    submission.status = SubmissionStatus.SUBMITTED
    submission.rubric_version_id = version_id
    submission.filename = upload.filename
    submission.content_type = upload.content_type
    submission.size = upload.size
    submission.sha256 = upload.sha256
    submission.object_key = upload.object_key
    submission.is_late = _is_late(assignment, now=now)
    submission.submitted_at = now
    submission.error_message = None
    submission.updated_at = now

    upload.completed_at = now
    upload.updated_at = now

    # 成功完成：同 Submission 的其他未完成会话标记为被替代
    # （它们的对象进入清理范围，后续完成请求返回 UPLOAD_SUPERSEDED）
    for other in await repo.list_active_upload_sessions(session, submission.id):
        if other.id == upload.id:
            continue
        other.superseded_at = now
        other.updated_at = now

    version_number = rubric_version.version if rubric_version else None
    # 快照只存不变量：预签名地址会在 10 分钟后过期，不能固化（见 _load_completed）
    snapshot = _detail_schema(
        submission, rubric_version=version_number, download=None
    )
    repo.save_completion_snapshot(
        session, upload, snapshot=snapshot.model_dump(mode="json")
    )

    try:
        await session.commit()
    except IntegrityError as exc:
        # 只识别"每份提交最多只有一个完成会话"这条命名唯一约束：
        # 命中时说明并发的另一个会话已经完成，本请求按 409 拒绝；
        # 其他数据库错误原样抛出，绝不吞掉后伪装成成功的幂等响应。
        await session.rollback()
        if _is_submission_completed_conflict(exc):
            logger.warning(
                "并发完成被部分唯一索引拦下（submission=%s）", submission.id
            )
            raise SubmissionAlreadyExistsError() from exc
        raise

    # 响应里的下载地址每次都**现场签发**：快照存的是不变量，
    # 预签名会在 10 分钟后过期，不能把过期地址固化进快照。
    return CompletionResult(
        response=_detail_schema(
            submission,
            rubric_version=version_number,
            download=_presign_download(
                storage, submission.object_key, settings=settings
            ),
        ),
        created=True,
    )


def _is_late(assignment: Assignment, *, now: datetime) -> bool:
    """是否为补交：完成时已过截止时间（``now == due_at`` 视为已截止）。"""
    return assignment.due_at is not None and now >= assignment.due_at


def _verify_stored_object(storage: S3Storage, upload: SubmissionUploadSession) -> None:
    """对象确认：核对存在性、大小、内容类型与存储侧校验值（契约 9.3）。

    不接收文件内容，只读取对象元数据；**不使用 ETag 代替 SHA-256**。
    """
    try:
        stored = storage.head_object(upload.object_key)
    except StorageObjectNotFoundError as exc:
        raise _upload_invalid(
            "OBJECT_MISSING",
            "对象存储中未找到已上传的报告，请先按预签名地址上传",
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
    session: AsyncSession,
    upload: SubmissionUploadSession,
    submission: Submission,
    *,
    settings: Settings,
    storage: S3Storage,
) -> CompletionResult:
    """读取已完成上传的响应：以首次快照为准，下载地址现场重新签发（契约 9.3）。

    只允许读取 ``completed_at``、完成快照与**正式提交状态**三者都存在的会话；
    不再为未完成的会话自动制造快照——那会把未完成记录伪装成已完成的幂等结果。
    """
    if (
        upload.completed_at is None
        or upload.completion_snapshot is None
        or submission.submitted_at is None
    ):
        raise _upload_invalid(
            "UPLOAD_STATE_CONFLICT",
            "上传会话状态与提交状态不一致，请重新初始化上传",
        )

    response = SubmissionDetailSchema.model_validate(upload.completion_snapshot)
    if response.download_url is None:
        # 预签名会过期，因此不在快照里固化；每次重放都重新签发一份新的
        download = _presign_download(
            storage, submission.object_key, settings=settings
        )
        response = response.model_copy(
            update={
                "download_url": download.url,
                "download_expires_at": download.expires_at,
            }
        )
    return CompletionResult(response=response, created=False)


# --------------------------------------------------------------------------- #
# 查询（契约 9.4 / 9.5）
# --------------------------------------------------------------------------- #
async def list_submissions(
    session: AsyncSession,
    *,
    user: User,
    assignment_id: uuid.UUID,
    pagination: PaginationParams,
) -> tuple[list[SubmissionListItem], int]:
    """提交列表：创建教师看全班分页，学生只看本人 0–1 条。"""
    assignment = await assignments_repo.get_assignment_by_id(session, assignment_id)
    if assignment is None:
        raise ResourceNotFoundError()
    if not await courses_service.is_course_member(
        session, user=user, course_id=assignment.course_id
    ):
        raise ResourceNotFoundError()
    course = await courses_repo.get_course_by_id(session, assignment.course_id)
    if course is None:  # pragma: no cover - 外键保证存在
        raise ResourceNotFoundError()

    if course.teacher_id == user.id:
        rows, total = await repo.list_assignment_submissions(
            session,
            assignment_id=assignment.id,
            offset=pagination.offset,
            limit=pagination.limit,
        )
    elif user.role is UserRole.STUDENT:
        rows, total = await repo.list_student_submissions(
            session, assignment_id=assignment.id, student_id=user.id
        )
    else:
        # 其他教师：不是本课程的授课教师，按角色无权处理（契约 9.1）
        raise RoleForbiddenError()

    versions = await repo.rubric_version_numbers(
        session, version_ids=[row.rubric_version_id for row in rows]
    )
    items = [
        SubmissionListItem(
            submission=row,
            rubric_version=(
                versions.get(row.rubric_version_id)
                if row.rubric_version_id is not None
                else None
            ),
        )
        for row in rows
    ]
    return items, total


async def get_submission_detail(
    session: AsyncSession,
    *,
    user: User,
    submission_id: uuid.UUID,
    settings: Settings,
    storage: S3Storage,
) -> SubmissionDetailData:
    """提交详情：本人或课程创建教师可读；正式提交附临时下载地址。"""
    submission, course = await _read_visible_submission(
        session, user=user, submission_id=submission_id
    )
    if course.teacher_id != user.id:
        if user.role is not UserRole.STUDENT:
            raise CourseForbiddenError()
        if submission.student_id != user.id:
            raise ResourceNotFoundError()

    versions = await repo.rubric_version_numbers(
        session,
        version_ids=[submission.rubric_version_id]
        if submission.rubric_version_id is not None
        else [],
    )
    download = None
    if submission.submitted_at is not None:
        download = _presign_download(
            storage, submission.object_key, settings=settings
        )
    return SubmissionDetailData(
        submission=submission,
        rubric_version=(
            versions.get(submission.rubric_version_id)
            if submission.rubric_version_id is not None
            else None
        ),
        download_url=download.url if download else None,
        download_expires_at=download.expires_at if download else None,
    )


def _presign_download(
    storage: S3Storage, object_key: str, *, settings: Settings
) -> PresignedDownload:
    """签发临时下载地址；存储不可用时统一转 503（契约 9.12）。

    与资料、作业附件共用 ``app.storage.downloads.presign_download``，
    保证三处的有效期来源与错误语义完全一致。
    """
    return presign_download(
        storage, object_key, ttl_seconds=settings.storage_upload_url_ttl_seconds
    )


# --------------------------------------------------------------------------- #
# 触发/重试批改（契约 9.6）
# --------------------------------------------------------------------------- #
async def request_grade(
    session: AsyncSession,
    *,
    submission: Submission,
    now: datetime | None = None,
) -> Job:
    """触发或重试 AI 批改：仅课程创建教师，返回 ``SUBMISSION_GRADE`` 任务。

    调用方（守卫依赖）已按 **课程 → Assignment → 提交固定 RubricVersion →
    Submission** 的顺序取锁并完成权限与归档检查；本函数锁任务行后按状态分流。
    """
    job = await jobs_service.lock_submission_grade_job(
        session, submission_id=submission.id
    )
    timestamp = now or utc_now()

    if submission.status in (
        SubmissionStatus.REVIEW_REQUIRED,
        SubmissionStatus.PUBLISHED,
    ):
        # 已有复核结果：不能覆盖（契约 9.6）
        raise SubmissionNotReadyError()
    if submission.status is SubmissionStatus.UPLOADING:
        raise SubmissionNotReadyError()

    if job is None:
        job = jobs_service.create_submission_grade_job(
            session, submission_id=submission.id, now=timestamp
        )
        submission.status = SubmissionStatus.GRADING
        submission.error_message = None
        submission.updated_at = timestamp
        await session.commit()
        return job

    lease_valid = (
        job.lease_expires_at is not None and job.lease_expires_at > timestamp
    )
    if job.status is JobStatusValue.PENDING or (
        job.status is JobStatusValue.RUNNING and lease_valid
    ):
        # 幂等：任务已在排队或执行中
        return job

    if job.status is JobStatusValue.RUNNING or job.status is JobStatusValue.FAILED:
        # 租约过期的 RUNNING（崩溃遗留）与 FAILED：复用原 job ID 重置
        job.status = JobStatusValue.PENDING
        job.progress = 0
        job.error = None
        job.started_at = None
        job.finished_at = None
        job.run_token = None
        job.lease_expires_at = None
        submission.status = SubmissionStatus.GRADING
        submission.error_message = None
        submission.updated_at = timestamp
        await session.commit()
        return job

    # SUCCEEDED / CANCELLED 与提交状态不一致：不覆盖已有结果
    raise SubmissionNotReadyError()


async def _lock_submission_rubric_version(
    session: AsyncSession, submission: Submission
) -> AssignmentRubricVersion | None:
    """按统一锁顺序锁住提交**固定**的评分版本行（未提交时为 NULL）。"""
    if submission.rubric_version_id is None:
        return None
    return await assignments_repo.lock_rubric_version(
        session, submission.rubric_version_id
    )


# --------------------------------------------------------------------------- #
# 批改详情（契约 9.7）
# --------------------------------------------------------------------------- #
async def get_grade_review_detail(
    session: AsyncSession,
    *,
    user: User,
    submission_id: uuid.UUID,
) -> GradeReviewData:
    """批改详情：教师始终可读；学生仅在提交 ``PUBLISHED`` 后可读本人终稿。"""
    submission, course = await _read_visible_submission(
        session, user=user, submission_id=submission_id
    )

    is_creator = course.teacher_id == user.id
    if not is_creator:
        if user.role is not UserRole.STUDENT:
            raise CourseForbiddenError()
        if submission.student_id != user.id:
            raise ResourceNotFoundError()
        if submission.status is not SubmissionStatus.PUBLISHED:
            # 发布前学生看不到 AI 草稿与教师未发布分数（契约 9.1）
            raise ResourceNotFoundError()

    review = await repo.get_grade_review_by_submission(session, submission.id)
    if review is None:
        if submission.status is SubmissionStatus.FAILED:
            job = await jobs_service.get_submission_grade_job(
                session, submission_id=submission.id
            )
            details = {"job_id": str(job.id)} if job is not None else {}
            raise AiJobFailedError("报告批改失败，暂时无法查看批改详情", details=details)
        raise SubmissionNotReadyError()

    items = await repo.list_grade_items(session, review_id=review.id)
    return GradeReviewData(review=review, items=items, student_view=not is_creator)


# --------------------------------------------------------------------------- #
# 教师复核与发布（契约 9.8 / 9.9）
# --------------------------------------------------------------------------- #
async def _submission_rubric_items(
    session: AsyncSession, submission: Submission
) -> list[AssignmentRubricItem]:
    """读取提交**固定**评分版本下的评分项（历史版本，非任务当前版本）。"""
    version_id = submission.rubric_version_id
    if version_id is None:  # pragma: no cover - 批改记录存在即已提交
        raise InternalError()
    return await assignments_repo.list_rubric_items(
        session, rubric_version_id=version_id
    )


async def update_grade_review(
    session: AsyncSession,
    *,
    user: User,
    review: GradeReview,
    submission: Submission,
    payload: GradeReviewUpdateRequest,
    now: datetime | None = None,
) -> GradeReviewData:
    """教师提交完整复核结果（契约 9.8）。

    调用方（守卫依赖）已按 **课程 → Assignment → 提交固定 RubricVersion →
    Submission → GradeReview** 的顺序取锁并完成权限与归档检查。
    AI 原始字段保持不变，只写教师终稿与评语，因此两者可同时审计。
    """
    if review.published_at is not None:
        raise GradeAlreadyPublishedError()

    rubric_items = await _submission_rubric_items(session, submission)
    max_scores = {item.id: item.max_score for item in rubric_items}
    submitted_scores = {item.rubric_item_id: item.final_score for item in payload.items}
    ensure_review_covers_rubric(
        [item.rubric_item_id for item in payload.items],
        [item.id for item in rubric_items],
        max_scores,
        submitted_scores,
    )

    timestamp = now or utc_now()
    by_rubric_item = {item.rubric_item_id: item for item in payload.items}
    items = await repo.list_grade_items(session, review_id=review.id)
    total = Decimal("0")
    for item in items:
        update = by_rubric_item[item.rubric_item_id]
        item.final_score = update.final_score
        item.teacher_comment = update.teacher_comment
        item.updated_at = timestamp
        total += update.final_score
        session.add(item)

    review.teacher_summary = payload.summary
    review.final_total_score = total
    review.reviewed_by = user.id
    review.reviewed_at = timestamp
    review.updated_at = timestamp
    session.add(review)
    await session.commit()
    return GradeReviewData(review=review, items=items, student_view=False)


async def publish_grade_review(
    session: AsyncSession,
    *,
    user: User,
    review: GradeReview,
    submission: Submission,
    now: datetime | None = None,
) -> GradeReviewData:
    """发布正式成绩（契约 9.9）：必须已复核；重复发布幂等。

    调用方（守卫依赖）已按统一锁顺序取锁并完成权限与归档检查。
    """
    items = await repo.list_grade_items(session, review_id=review.id)

    if review.published_at is not None:
        # 幂等：不覆盖首次 published_at
        return GradeReviewData(review=review, items=items, student_view=False)
    if review.reviewed_at is None:
        raise GradeNotReviewedError()

    timestamp = now or utc_now()
    review.published_at = timestamp
    review.updated_at = timestamp
    submission.status = SubmissionStatus.PUBLISHED
    submission.updated_at = timestamp
    session.add(review)
    session.add(submission)
    await session.commit()
    return GradeReviewData(review=review, items=items, student_view=False)


# --------------------------------------------------------------------------- #
# 响应组装
# --------------------------------------------------------------------------- #
def _summary_schema(
    submission: Submission, *, rubric_version: int | None
) -> SubmissionSummarySchema:
    return SubmissionSummarySchema(
        id=submission.id,
        assignment_id=submission.assignment_id,
        course_id=submission.course_id,
        student_id=submission.student_id,
        status=submission.status,
        filename=submission.filename,
        content_type=submission.content_type,
        size=submission.size,
        is_late=submission.is_late,
        rubric_version=rubric_version,
        submitted_at=submission.submitted_at,
        created_at=submission.created_at,
        updated_at=submission.updated_at,
    )


def _detail_schema(
    submission: Submission,
    *,
    rubric_version: int | None,
    download: PresignedDownload | None,
) -> SubmissionDetailSchema:
    return SubmissionDetailSchema(
        **_summary_schema(submission, rubric_version=rubric_version).model_dump(),
        sha256=submission.sha256,
        download_url=download.url if download else None,
        download_expires_at=download.expires_at if download else None,
    )


def submission_summary(
    item: SubmissionListItem,
) -> SubmissionSummarySchema:
    """列表元素 → 响应模型。"""
    return _summary_schema(item.submission, rubric_version=item.rubric_version)


def submission_detail(data: SubmissionDetailData) -> SubmissionDetailSchema:
    """详情 → 响应模型。"""
    return _detail_schema(
        data.submission,
        rubric_version=data.rubric_version,
        download=(
            PresignedDownload(
                url=data.download_url,
                method="GET",
                expires_at=data.download_expires_at,
            )
            if data.download_url and data.download_expires_at
            else None
        ),
    )


def grade_review_detail(data: GradeReviewData) -> GradeReviewDetailSchema:
    """批改详情 → 响应模型；``student_view`` 时隐藏 AI 原始建议。"""
    student = data.student_view
    return GradeReviewDetailSchema(
        id=data.review.id,
        submission_id=data.review.submission_id,
        ai_summary=None if student else data.review.ai_summary,
        teacher_summary=data.review.teacher_summary,
        suggested_total_score=(
            None if student else float(data.review.suggested_total_score)
        ),
        final_total_score=float(data.review.final_total_score),
        items=[
            GradeItemDetailSchema(
                id=item.id,
                rubric_item_id=item.rubric_item_id,
                order=item.order,
                title=item.title,
                max_score=float(item.max_score),
                ai_score=None if student else float(item.ai_score),
                final_score=float(item.final_score),
                ai_comment=None if student else item.ai_comment,
                evidence_quote=item.evidence_quote,
                evidence_source_type=item.evidence_source_type,
                evidence_location_start=item.evidence_location_start,
                evidence_location_end=item.evidence_location_end,
                error_type=item.error_type,
                improvement_suggestion=item.improvement_suggestion,
                teacher_comment=item.teacher_comment,
            )
            for item in data.items
        ],
        reviewed_by=data.review.reviewed_by,
        reviewed_at=data.review.reviewed_at,
        published_at=data.review.published_at,
    )


# --------------------------------------------------------------------------- #
# 过期/被替代上传对象的清理（契约 9.3 的孤立对象清理）
# --------------------------------------------------------------------------- #
async def cleanup_expired_submission_uploads(
    session: AsyncSession,
    *,
    storage: S3Storage,
    settings: Settings,
    now: datetime | None = None,
    limit: int | None = None,
) -> int:
    """清理过期或被替代的报告上传对象（幂等维护命令，契约 9.3）。

    领取条件：``completed_at IS NULL``（已完成会话及其对象**永不清理**）、
    ``expired_at IS NULL``（幂等）、（已超过确认窗口或已被替代）、且
    ``upload_url_expires_at + SUBMISSION_UPLOAD_DELETE_BUFFER_SECONDS`` 已过——
    预签名 PUT 到期前浏览器仍可能直传，立即删除会留下重建窗口。

    每条会话：删除对象 → **再次 HeadObject 复查**：

    - 确认对象不存在才设置 ``expired_at``；
    - 对象重新出现（晚到 PUT）或存储不可用 → 保持未过期，留给下一轮重试。

    用 ``FOR UPDATE SKIP LOCKED`` 与并发的完成请求互斥，不会误删已确认对象。
    """
    current = now or utc_now()
    batch = limit if limit is not None else repo.CLEANUP_BATCH_LIMIT
    url_expired_before = current - timedelta(
        seconds=settings.submission_upload_delete_buffer_seconds
    )
    sessions = await repo.list_cleanable_upload_sessions(
        session,
        now=current,
        url_expired_before=url_expired_before,
        limit=batch,
    )

    cleaned = 0
    for upload in sessions:
        try:
            try:
                storage.delete_object(upload.object_key)
            except StorageObjectNotFoundError:
                # 对象本就不存在（未直传或已删），视为已清理
                pass
            # 删除后复查：晚到 PUT 会在删除之后重建对象
            storage.head_object(upload.object_key)
        except StorageObjectNotFoundError:
            upload.expired_at = current
            upload.updated_at = current
            session.add(upload)
            cleaned += 1
        except StorageUnavailableError as exc:
            # 存储暂时不可用：跳过本会话，下次清理再处理，不误标过期
            logger.warning(
                "清理报告上传对象时存储不可用，跳过本次（key=%s）：%s",
                upload.object_key,
                exc.reason,
            )
            continue
        else:
            logger.warning(
                "报告对象删除后再次出现（晚到 PUT），本轮不标记过期，"
                "留给下一轮继续清理（key=%s）",
                upload.object_key,
            )

    if cleaned:
        await session.commit()
    return cleaned


__all__ = [
    "CompletionResult",
    "CreatorReviewGuard",
    "CreatorSubmissionGuard",
    "GradeReviewData",
    "StudentAssignmentGuard",
    "StudentUploadGuard",
    "SubmissionDetailData",
    "SubmissionListItem",
    "ValidatedUpload",
    "cleanup_expired_submission_uploads",
    "complete_submission_upload",
    "get_grade_review_detail",
    "get_submission_detail",
    "grade_review_detail",
    "init_submission_upload",
    "list_submissions",
    "lock_creator_course",
    "lock_creator_review",
    "lock_creator_submission",
    "lock_student_assignment",
    "lock_student_course",
    "lock_student_upload",
    "publish_grade_review",
    "request_grade",
    "require_submission_viewer",
    "split_extension",
    "submission_detail",
    "submission_summary",
    "update_grade_review",
    "validate_upload_request",
]
