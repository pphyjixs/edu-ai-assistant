"""Courses 业务规则与事务边界。

事务边界在本层：repository 只暂存对象，service 决定何时 ``commit``。
"创建课程同时写入创建教师成员""并发重复加入返回 200"这类规则才有明确落点。

对外（后续业务模块）提供三个权限检查方法：

- :func:`require_course_member`：课程成员检查，非成员 ``403 COURSE_FORBIDDEN``；
- :func:`require_course_teacher`：创建教师检查，其他教师 ``403 COURSE_FORBIDDEN``；
- :func:`require_course_active`：活动状态检查，归档 ``409 COURSE_ARCHIVED``。

检查顺序遵循契约 3.5：先认证、再资源存在与权限，最后才返回归档状态错误，
避免向无权访问者暴露课程状态。
"""

from __future__ import annotations

import logging
import secrets
import string
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    CourseArchivedError,
    CourseForbiddenError,
    InternalError,
    InviteCodeInvalidError,
    ResourceNotFoundError,
)
from app.core.pagination import PaginationParams
from app.core.time import utc_now
from app.modules.auth import service as auth_service
from app.modules.auth.models import User
from app.modules.courses import repository as repo
from app.modules.courses.models import (
    INVITE_CODE_LENGTH,
    Course,
    CourseRole,
    CourseStatus,
)
from app.modules.courses.schemas import (
    CourseCreateRequest,
    CourseJoinRequest,
    CourseMemberSummary,
    CourseUpdateRequest,
)

logger = logging.getLogger("app.courses")

#: 邀请码字符集：大写字母与数字
INVITE_CODE_ALPHABET = string.ascii_uppercase + string.digits

#: 生成邀请码时的最大尝试次数；12 位码的冲突概率极低，重试只是兜底
MAX_INVITE_CODE_ATTEMPTS = 12


def generate_invite_code() -> str:
    """生成 12 位大写字母与数字的邀请码。

    使用 :mod:`secrets`（密码学安全随机），避免被枚举。
    """
    return "".join(
        secrets.choice(INVITE_CODE_ALPHABET) for _ in range(INVITE_CODE_LENGTH)
    )


# --------------------------------------------------------------------------- #
# 供后续业务模块复用的权限检查
# --------------------------------------------------------------------------- #
async def require_course_member(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
) -> Course:
    """课程成员检查：课程不存在返回 404，非成员返回 403。"""
    course = await repo.get_course_by_id(session, course_id)
    if course is None:
        raise ResourceNotFoundError()
    member = await repo.get_member(session, course_id=course.id, user_id=user.id)
    if member is None:
        raise CourseForbiddenError()
    return course


async def is_course_member(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
) -> bool:
    """判断用户是否为课程成员（不抛异常）。

    供「不存在与不可见统一 404」的查询接口使用（契约 4.7）：
    资料/任务查询要求非成员与不存在返回同一个 404，而不是 403。
    """
    member = await repo.get_member(session, course_id=course_id, user_id=user.id)
    return member is not None


async def require_member_course(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
) -> Course:
    """成员检查并返回课程；课程不存在或非成员统一 404（契约 6.1）。

    与 :func:`require_course_member` 的区别：后者对非成员返回 403，用于
    「课程存在但不允许操作」的场景；问答接口按「不可见」处理，统一 404，
    不暴露课程是否存在。
    """
    course = await repo.get_course_by_id(session, course_id)
    if course is None:
        raise ResourceNotFoundError()
    if not await is_course_member(session, user=user, course_id=course.id):
        raise ResourceNotFoundError()
    return course


async def lock_member_course(
    session: AsyncSession, *, user_id: uuid.UUID, course_id: uuid.UUID
) -> Course:
    """**锁定课程行**并检查成员资格（问答写入路径的临界区入口）。

    锁定与 :func:`archive_course` 使用同一把行锁，因此「检查归档 → 写入」
    与并发归档严格按事务顺序生效：任何一方先拿到锁，另一方都会看到
    已提交的结果（归档后写入返回 ``409``，写入完成后归档正常生效）。

    课程不存在或非成员统一 404（契约 6.1）。只接收标量 ``user_id``，
    便于调用方在结束只读事务后重新开启写入事务。
    """
    course = await repo.get_course_for_update(session, course_id)
    if course is None:
        raise ResourceNotFoundError()
    if await repo.get_member(session, course_id=course.id, user_id=user_id) is None:
        raise ResourceNotFoundError()
    return course


async def require_course_teacher(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
) -> Course:
    """创建教师检查：只有创建教师能管理课程，其他教师返回 403。"""
    course = await repo.get_course_by_id(session, course_id)
    if course is None:
        raise ResourceNotFoundError()
    if course.teacher_id != user.id:
        raise CourseForbiddenError()
    return course


def require_course_active(course: Course) -> None:
    """活动状态检查：归档课程不能执行写入操作。"""
    if course.status != CourseStatus.ACTIVE:
        raise CourseArchivedError()


def visible_invite_code(course: Course, *, user: User) -> str | None:
    """邀请码可见性：仅创建教师查看 **未归档** 课程时可见。

    其余情况（学生、其他教师、已归档课程）一律返回 ``None``，
    由路由省略该字段，而不是返回 ``null``。
    """
    if course.status is CourseStatus.ACTIVE and course.teacher_id == user.id:
        return course.invite_code
    return None


# --------------------------------------------------------------------------- #
# 接口对应的业务方法
# --------------------------------------------------------------------------- #
async def create_course(
    session: AsyncSession, *, teacher: User, payload: CourseCreateRequest
) -> Course:
    """创建课程，并在同一事务中写入创建教师的成员记录。

    邀请码冲突由数据库唯一约束兜住：冲突时换一个新码重试，
    而不是"先查后插"（那样并发窗口内仍会撞码）。
    """
    now = utc_now()
    for _ in range(MAX_INVITE_CODE_ATTEMPTS):
        course_id = uuid.uuid4()
        invite_code = generate_invite_code()
        course = repo.add_course(
            session,
            course_id=course_id,
            name=payload.name,
            description=payload.description,
            teacher_id=teacher.id,
            status=CourseStatus.ACTIVE,
            invite_code=invite_code,
            now=now,
        )
        repo.add_course_member(
            session,
            course_id=course_id,
            user_id=teacher.id,
            course_role=CourseRole.TEACHER,
            joined_at=now,
        )
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            if not await repo.invite_code_exists(session, invite_code):
                raise
            logger.warning("邀请码生成冲突，重新生成")
            continue
        return course

    logger.error("邀请码连续冲突 %s 次，放弃创建", MAX_INVITE_CODE_ATTEMPTS)
    raise InternalError()


async def list_my_courses(
    session: AsyncSession, *, user: User, pagination: PaginationParams
) -> tuple[list[Course], int]:
    """列出当前用户参与的课程（活动 + 归档），按创建时间倒序、ID 倒序。"""
    return await repo.list_courses_for_user(
        session,
        user_id=user.id,
        offset=pagination.offset,
        limit=pagination.limit,
    )


async def get_course_detail(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
) -> tuple[Course, str | None]:
    """课程详情；归档课程仍可读，返回 ``(课程, 可见的邀请码)``。"""
    course = await require_course_member(session, user=user, course_id=course_id)
    return course, visible_invite_code(course, user=user)


async def update_course(
    session: AsyncSession,
    *,
    user: User,
    course_id: uuid.UUID,
    payload: CourseUpdateRequest,
) -> Course:
    """修改课程名称与说明；省略的字段保持原值。"""
    course = await _load_owned_course_for_update(
        session, user=user, course_id=course_id
    )
    require_course_active(course)

    if payload.name is not None:
        course.name = payload.name
    if payload.description is not None:
        course.description = payload.description
    course.updated_at = utc_now()

    await session.commit()
    return course


async def archive_course(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
) -> Course:
    """归档课程。

    锁住课程行后再判断，保证并发归档按事务顺序生效；
    已归档时直接返回当前详情（幂等），第一版不提供恢复。
    """
    course = await repo.get_course_for_update(session, course_id)
    if course is None:
        raise ResourceNotFoundError()
    if course.teacher_id != user.id:
        raise CourseForbiddenError()

    if course.status is CourseStatus.ARCHIVED:
        return course

    course.status = CourseStatus.ARCHIVED
    course.updated_at = utc_now()
    await session.commit()
    return course


async def reset_invite_code(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
) -> str:
    """重新生成邀请码；成功后旧码立即失效。"""
    course = await _load_owned_course_for_update(session, user=user, course_id=course_id)
    require_course_active(course)

    for _ in range(MAX_INVITE_CODE_ATTEMPTS):
        invite_code = generate_invite_code()
        if invite_code == course.invite_code:
            continue
        course.invite_code = invite_code
        course.updated_at = utc_now()
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            if not await repo.invite_code_exists(session, invite_code):
                raise
            logger.warning("重置邀请码冲突，重新生成")
            # 回滚后原对象已失效，重新加载再试
            course = await _load_owned_course_for_update(
                session, user=user, course_id=course_id
            )
            continue
        return invite_code

    logger.error("重置邀请码连续冲突 %s 次，放弃", MAX_INVITE_CODE_ATTEMPTS)
    raise InternalError()


async def join_course(
    session: AsyncSession, *, user: User, payload: CourseJoinRequest
) -> tuple[Course, bool]:
    """使用邀请码加入课程。

    返回 ``(课程, 是否首次加入)``：首次 ``201``，重复 ``200`` 且不新增成员。

    顺序很重要：先确认邀请码有效、课程为 ``ACTIVE``，再判断是否已是成员。
    因此旧码返回 ``422``，归档课程即使已经加入也返回 ``409``。
    """
    found = await repo.get_course_by_invite_code(session, payload.invite_code)
    if found is None:
        raise InviteCodeInvalidError()

    # 锁住课程行：归档判断与写入成员必须在同一临界区
    course = await repo.get_course_for_update(session, found.id)
    if course is None:  # pragma: no cover - 并发删除才会进入
        raise InviteCodeInvalidError()
    if course.invite_code != payload.invite_code:
        raise InviteCodeInvalidError()
    require_course_active(course)

    existing = await repo.get_member(session, course_id=course.id, user_id=user.id)
    if existing is not None:
        return course, False

    repo.add_course_member(
        session,
        course_id=course.id,
        user_id=user.id,
        course_role=CourseRole.STUDENT,
        joined_at=utc_now(),
    )
    try:
        await session.commit()
    except IntegrityError:
        # 并发重复加入被 (course_id, user_id) 唯一约束拦住，按幂等处理
        await session.rollback()
        concurrent = await repo.get_member(
            session, course_id=course.id, user_id=user.id
        )
        if concurrent is None:
            raise
        return course, False
    return course, True


async def list_course_members(
    session: AsyncSession,
    *,
    user: User,
    course_id: uuid.UUID,
    pagination: PaginationParams,
) -> tuple[list[CourseMemberSummary], int]:
    """课程成员列表（含创建教师），按加入时间正序、用户 ID 正序。

    显示名称通过 Auth 模块提供的批量公开摘要取得，不直接查询 ``users`` 表。
    """
    course = await require_course_teacher(session, user=user, course_id=course_id)
    members, total = await repo.list_members(
        session,
        course_id=course.id,
        offset=pagination.offset,
        limit=pagination.limit,
    )

    summaries = await auth_service.get_public_user_summaries(
        session, [member.user_id for member in members]
    )
    items = [
        CourseMemberSummary(
            user_id=member.user_id,
            # 外键保证用户存在；缺失时退化为空白，不因为展示需求让列表失败
            display_name=(
                summaries[member.user_id].display_name
                if member.user_id in summaries
                else ""
            ),
            course_role=member.course_role,
            joined_at=member.joined_at,
        )
        for member in members
    ]
    return items, total


async def _load_owned_course_for_update(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
) -> Course:
    """锁住课程行并校验创建教师身份。"""
    course = await repo.get_course_for_update(session, course_id)
    if course is None:
        raise ResourceNotFoundError()
    if course.teacher_id != user.id:
        raise CourseForbiddenError()
    return course
