"""Assignments 业务规则与事务边界（``docs/api-contract.md`` 第 8 节）。

关键约定：

- **检查顺序固定**（契约 8.1）：认证（框架）→ 课程/任务存在及成员可见性（404）
  → 角色与课程创建者（403）→ 课程归档（409）→ 任务状态（409）→ 请求体结构与
  字段（422）→ ``RUBRIC_SCORE_MISMATCH``（422）→ 数据写入。
- **可见性统一 404**：非成员、他人的课程、学生视角下的草稿，一律
  ``404 RESOURCE_NOT_FOUND``，不区分"不存在"与"不可见"。
- **统一锁顺序**：课程行 → 任务行 → 当前评分版本；写路径两阶段执行
  （守卫依赖先加课程与任务行锁并做权限/归档/状态检查，路由随后校验请求体，
  服务在锁内写入）。修改、发布、关闭都会调用
  :func:`lock_current_rubric_version` 取到版本行锁——创建时还没有版本行，
  由课程行锁覆盖。
- **评分规则版本只追加**：写入后不原地修改；只有总分或评分项发生实质变化时
  才追加 ``current_version + 1``，重复提交相同规则不产生新版本。
- **总分用 ``Decimal`` 精确比较**，不使用二进制浮点。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AssignmentNotOpenError,
    ResourceNotFoundError,
    RoleForbiddenError,
)
from app.core.pagination import PaginationParams
from app.core.time import utc_now
from app.modules.assignments import repository as repo
from app.modules.assignments.models import (
    Assignment,
    AssignmentRubricItem,
    AssignmentRubricVersion,
    AssignmentStatus,
    STUDENT_VISIBLE_STATUSES,
)
from app.modules.assignments.schemas import (
    AssignmentCreateRequest,
    AssignmentUpdateRequest,
    ensure_rubric_total_matches,
    rubric_fingerprint,
)
from app.modules.auth.models import User, UserRole
from app.modules.courses import repository as courses_repo
from app.modules.courses import service as courses_service
from app.modules.courses.models import Course


@dataclass(slots=True)
class AssignmentDetail:
    """任务详情：任务本身 + 当前评分版本号与总分 + 按顺序的评分项。"""

    assignment: Assignment
    rubric_version: int
    total_score: Decimal = Decimal("0")
    rubric_items: list[AssignmentRubricItem] = field(default_factory=list)


@dataclass(slots=True)
class AssignmentListItem:
    """列表元素：任务 + 当前评分版本号与总分。"""

    assignment: Assignment
    rubric_version: int
    total_score: Decimal


# --------------------------------------------------------------------------- #
# 前置检查（写路径的守卫依赖调用）
# --------------------------------------------------------------------------- #
async def _lock_visible_course(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
) -> Course:
    """锁课程行并做成员可见性检查（课程不存在或非成员统一 404）。"""
    course = await courses_repo.get_course_for_update(session, course_id)
    if course is None:
        raise ResourceNotFoundError()
    if not await courses_service.is_course_member(
        session, user=user, course_id=course.id
    ):
        raise ResourceNotFoundError()
    return course


async def lock_creator_course(
    session: AsyncSession, *, user: User, course_id: uuid.UUID
) -> Course:
    """写路径第一阶段：锁课程 → 成员（404）→ 创建教师（403）→ 未归档（409）。"""
    course = await _lock_visible_course(session, user=user, course_id=course_id)
    if user.role is UserRole.STUDENT:
        raise RoleForbiddenError()
    await courses_service.require_course_teacher(
        session, user=user, course_id=course.id
    )
    courses_service.require_course_active(course)
    return course


async def lock_writable_assignment(
    session: AsyncSession,
    *,
    user: User,
    assignment_id: uuid.UUID,
    publishable: bool = False,
    closable: bool = False,
    reopenable: bool = False,
) -> Assignment:
    """写路径第一阶段：读任务（404）→ 锁课程（成员/创建教师/归档）→ 锁任务。

    锁顺序固定为 **课程 → 任务**。状态检查按接口需要执行（发布 / 关闭 /
    重新开启接口各自限定允许的状态），因此请求体校验排在权限、归档与状态之后。

    :raises ResourceNotFoundError: 任务不存在或对当前用户不可见（404）。
    :raises RoleForbiddenError / CourseForbiddenError: 非创建教师（403）。
    :raises CourseArchivedError: 课程已归档（409）。
    :raises AssignmentNotOpenError: 状态不允许该操作（409）。
    """
    assignment = await repo.get_assignment_by_id(session, assignment_id)
    if assignment is None:
        raise ResourceNotFoundError()
    await lock_creator_course(session, user=user, course_id=assignment.course_id)

    locked = await repo.lock_assignment(session, assignment_id)
    if locked is None:  # pragma: no cover - 并发删除的兜底
        raise ResourceNotFoundError()
    if closable:
        require_closable(locked)
    elif reopenable:
        require_reopenable(locked)
    elif publishable:
        require_publishable(locked)
    else:
        require_editable(locked)
    return locked


# --------------------------------------------------------------------------- #
# 状态机（契约 8.1）
# --------------------------------------------------------------------------- #
def require_editable(assignment: Assignment) -> None:
    """只有 ``DRAFT``、``PUBLISHED`` 可以修改。"""
    if assignment.status not in (AssignmentStatus.DRAFT, AssignmentStatus.PUBLISHED):
        raise AssignmentNotOpenError()


def require_publishable(assignment: Assignment) -> None:
    """``DRAFT`` 可发布；``PUBLISHED`` 幂等；``CLOSED`` / ``ARCHIVED`` 拒绝。"""
    if assignment.status not in (AssignmentStatus.DRAFT, AssignmentStatus.PUBLISHED):
        raise AssignmentNotOpenError()


def require_closable(assignment: Assignment) -> None:
    """``PUBLISHED`` 可关闭；``CLOSED`` 幂等；``DRAFT`` / ``ARCHIVED`` 拒绝。"""
    if assignment.status not in (AssignmentStatus.PUBLISHED, AssignmentStatus.CLOSED):
        raise AssignmentNotOpenError()


def require_reopenable(assignment: Assignment) -> None:
    """``CLOSED`` 可重新开启；``PUBLISHED`` 幂等；``DRAFT`` / ``ARCHIVED`` 拒绝。

    重新开启是为了纠正「关早了」：关闭只是停止收作业，不是把任务作废，
    因此它必须是可逆的。归档课程仍不可写（守卫在状态检查之前就拦下）。
    """
    if assignment.status not in (AssignmentStatus.CLOSED, AssignmentStatus.PUBLISHED):
        raise AssignmentNotOpenError()


def require_attachment_writable(assignment: Assignment) -> None:
    """附件写路径的状态限制：只有**归档的任务**不允许再改附件（契约 8.15）。

    草稿、进行中、已关闭都允许上传附件 —— 附件是教师自己的参考资料，
    与"学生能不能提交"是两件事：作业关闭后教师仍可能想补一份评分说明。
    """
    if assignment.status is AssignmentStatus.ARCHIVED:
        raise AssignmentNotOpenError()


async def lock_attachment_writable_assignment(
    session: AsyncSession, *, user: User, assignment_id: uuid.UUID
) -> Assignment:
    """附件写路径第一阶段：读任务（404）→ 锁课程（成员/创建教师/归档）→ 锁任务。

    锁顺序与其它写路径一致：**课程 → 任务**。
    """
    assignment = await repo.get_assignment_by_id(session, assignment_id)
    if assignment is None:
        raise ResourceNotFoundError()
    await lock_creator_course(session, user=user, course_id=assignment.course_id)

    locked = await repo.lock_assignment(session, assignment_id)
    if locked is None:  # pragma: no cover - 并发删除的兜底
        raise ResourceNotFoundError()
    require_attachment_writable(locked)
    return locked


# --------------------------------------------------------------------------- #
# 内部服务：供第 9 节 Submission 模块复用（契约 8.10）
# --------------------------------------------------------------------------- #
def can_submit(assignment: Assignment, *, now: datetime) -> bool:
    """是否允许提交：``PUBLISHED`` 且未截止或允许补交（手工关闭优先）。"""
    if assignment.status is not AssignmentStatus.PUBLISHED:
        return False
    if assignment.due_at is None:
        return True
    if now < assignment.due_at:
        return True
    # now == due_at 视为已经截止
    return bool(assignment.allow_late_submission)


def current_rubric_version_id(assignment: Assignment) -> uuid.UUID | None:
    """当前评分规则版本 ID（第 9 节批改按此版本评分）。"""
    return assignment.current_rubric_version_id


async def lock_current_rubric_version(
    session: AsyncSession, *, assignment: Assignment
) -> AssignmentRubricVersion | None:
    """锁住当前评分规则版本行（统一锁顺序的最后一步）。

    顺序固定为 **课程 → 任务 → 当前评分版本**（契约 8.1），调用方必须已经拿到
    课程与任务行锁（守卫依赖完成）。创建事务里还没有版本行，此时没有可锁对象。

    所有写事务（修改、发布、关闭）都必须调用它；读接口只做普通读取。
    """
    version_id = assignment.current_rubric_version_id
    if version_id is None:  # pragma: no cover - 创建事务保证不为空
        return None
    return await repo.lock_rubric_version(session, version_id)


async def get_rubric_snapshot(
    session: AsyncSession, *, assignment: Assignment, for_update: bool = False
) -> tuple[AssignmentRubricVersion | None, list[AssignmentRubricItem]]:
    """读取当前评分版本与按 ``order`` 升序的评分项。

    ``for_update=True`` 时按统一锁顺序锁住当前版本行（写事务必须用它）；
    缺省为普通读取，供读接口与写入后的响应组装使用。
    """
    version_id = assignment.current_rubric_version_id
    if version_id is None:  # pragma: no cover - 创建事务保证不为空
        return None, []
    if for_update:
        version = await lock_current_rubric_version(session, assignment=assignment)
    else:
        version = await repo.get_rubric_version_by_id(session, version_id)
    if version is None:  # pragma: no cover - 外键保证存在
        return None, []
    items = await repo.list_rubric_items(session, rubric_version_id=version_id)
    return version, items


def _rubric_item_rows(
    items: list[AssignmentRubricItem],
) -> list[tuple[str, str, Decimal, int]]:
    """把 ORM 评分项转成比较指纹使用的元组。"""
    return [(item.title, item.description, item.max_score, item.order) for item in items]


# --------------------------------------------------------------------------- #
# 创建（契约 8.2）
# --------------------------------------------------------------------------- #
async def create_assignment(
    session: AsyncSession,
    *,
    course: Course,
    user: User,
    payload: AssignmentCreateRequest,
    now: datetime | None = None,
) -> Assignment:
    """创建任务：任务、评分版本 1 与评分项在**同一事务**写入。

    调用方必须先用 :func:`lock_creator_course` 取得课程行锁。

    :raises RubricScoreMismatchError: 评分项之和不等于总分（422）。
    """
    ensure_rubric_total_matches(
        payload.total_score, [item.max_score for item in payload.rubric_items]
    )

    timestamp = now or utc_now()
    teacher_id = user.id
    assignment_id = uuid.uuid4()
    assignment = repo.add_assignment(
        session,
        assignment_id=assignment_id,
        course_id=course.id,
        created_by=teacher_id,
        title=payload.title,
        description=payload.description,
        due_at=payload.due_at,
        allow_late_submission=payload.allow_late_submission,
        status=AssignmentStatus.DRAFT,
        now=timestamp,
    )
    # 三张表互相引用（assignments ↔ versions 为循环外键，items → versions 为普通外键），
    # ORM 在缺少 relationship 时不会保证插入顺序，因此**逐步 flush**：
    # 任务（指针 NULL）→ 评分版本 → 评分项 → 回填指针。
    # 任一步失败整笔回滚，不会留下没有评分规则的任务。
    await session.flush()
    version = repo.add_rubric_version(
        session,
        version_id=uuid.uuid4(),
        assignment_id=assignment_id,
        version=1,
        total_score=payload.total_score,
        created_by=teacher_id,
        now=timestamp,
    )
    await session.flush()
    for item in payload.rubric_items:
        repo.add_rubric_item(
            session,
            item_id=uuid.uuid4(),
            rubric_version_id=version.id,
            title=item.title,
            description=item.description,
            max_score=item.max_score,
            order=item.order,
            now=timestamp,
        )
    await session.flush()
    assignment.current_rubric_version_id = version.id
    await session.commit()
    return assignment


# --------------------------------------------------------------------------- #
# 查询（契约 8.3 / 8.4）
# --------------------------------------------------------------------------- #
async def list_assignments(
    session: AsyncSession,
    *,
    user: User,
    course_id: uuid.UUID,
    pagination: PaginationParams,
) -> tuple[list[AssignmentListItem], int]:
    """课程任务列表：成员可读；学生视角在 SQL 层排除草稿。"""
    await courses_service.require_member_course(
        session, user=user, course_id=course_id
    )
    include_drafts = user.role is UserRole.TEACHER
    rows, total = await repo.list_assignments(
        session,
        course_id=course_id,
        include_drafts=include_drafts,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    overview = await repo.rubric_overview(
        session, assignment_ids=[row.id for row in rows]
    )
    return (
        [
            AssignmentListItem(
                assignment=row,
                rubric_version=overview.get(row.id, (0, Decimal("0")))[0],
                total_score=overview.get(row.id, (0, Decimal("0")))[1],
            )
            for row in rows
        ],
        total,
    )


async def get_assignment_detail(
    session: AsyncSession, *, user: User, assignment_id: uuid.UUID
) -> AssignmentDetail:
    """任务详情：教师可读全部状态；学生只读已发布/已关闭/已归档。"""
    assignment = await repo.get_assignment_by_id(session, assignment_id)
    if assignment is None:
        raise ResourceNotFoundError()
    await courses_service.require_member_course(
        session, user=user, course_id=assignment.course_id
    )
    if (
        user.role is not UserRole.TEACHER
        and assignment.status not in STUDENT_VISIBLE_STATUSES
    ):
        # 学生视角下草稿按不存在处理
        raise ResourceNotFoundError()

    version, items = await get_rubric_snapshot(session, assignment=assignment)
    return AssignmentDetail(
        assignment=assignment,
        rubric_version=version.version if version else 0,
        total_score=version.total_score if version else Decimal("0"),
        rubric_items=items,
    )


# --------------------------------------------------------------------------- #
# 修改（契约 8.5 / 8.9）
# --------------------------------------------------------------------------- #
async def update_assignment(
    session: AsyncSession,
    *,
    assignment: Assignment,
    user: User,
    payload: AssignmentUpdateRequest,
    now: datetime | None = None,
) -> Assignment:
    """修改任务：候选值校验 + 必要时追加评分版本。

    调用方必须先用 :func:`lock_writable_assignment` 锁好课程与任务行；候选结果
    由"数据库当前值 + 本次提供字段"组成，因此只改总分或只改部分评分项也能正确
    判断一致性。

    :raises AssignmentNotOpenError: 状态不允许修改（409）。
    :raises RubricScoreMismatchError: 候选评分项之和不等于候选总分（422）。
    """
    require_editable(assignment)
    timestamp = now or utc_now()
    provided = payload.model_fields_set

    if "title" in provided:
        assignment.title = payload.title
    if "description" in provided:
        assignment.description = payload.description
    if "allow_late_submission" in provided:
        assignment.allow_late_submission = payload.allow_late_submission
    if "due_at" in provided:
        # 出现且为 None 表示清除截止时间
        assignment.due_at = payload.due_at

    # 统一锁顺序的最后一步：课程 → 任务（守卫已锁）→ 当前评分版本
    current_version, current_items = await get_rubric_snapshot(
        session, assignment=assignment, for_update=True
    )
    candidate: tuple[Decimal, list[tuple[str, str, Decimal, int]]] | None = None
    if "rubric_items" in provided:
        submitted = payload.rubric_items or []
        total = (
            payload.total_score
            if "total_score" in provided
            else (current_version.total_score if current_version else None)
        )
        if total is not None:
            candidate = (
                total,
                [
                    (item.title, item.description, item.max_score, item.order)
                    for item in submitted
                ],
            )
    elif "total_score" in provided:
        candidate = (payload.total_score, _rubric_item_rows(current_items))

    if candidate is not None:
        total, rows = candidate
        ensure_rubric_total_matches(total, [row[2] for row in rows])
        current_total = current_version.total_score if current_version else Decimal("0")
        changed = rubric_fingerprint(total, rows) != rubric_fingerprint(
            current_total, _rubric_item_rows(current_items)
        )
        if changed:
            # 版本号在同一事务内计算：守卫已持有课程与任务行锁，两个并发修改会串行
            next_number = (
                await repo.max_rubric_version(session, assignment_id=assignment.id) + 1
            )
            new_version = repo.add_rubric_version(
                session,
                version_id=uuid.uuid4(),
                assignment_id=assignment.id,
                version=next_number,
                total_score=total,
                created_by=user.id,
                now=timestamp,
            )
            # 与创建一致：先落库版本、再落库评分项、最后切换当前版本指针
            await session.flush()
            for title, description, max_score, order in rows:
                repo.add_rubric_item(
                    session,
                    item_id=uuid.uuid4(),
                    rubric_version_id=new_version.id,
                    title=title,
                    description=description,
                    max_score=max_score,
                    order=order,
                    now=timestamp,
                )
            await session.flush()
            assignment.current_rubric_version_id = new_version.id

    assignment.updated_at = timestamp
    await session.commit()
    return assignment


# --------------------------------------------------------------------------- #
# 发布 / 关闭（契约 8.6 / 8.7）
# --------------------------------------------------------------------------- #
async def publish_assignment(
    session: AsyncSession, *, assignment: Assignment, now: datetime | None = None
) -> Assignment:
    """发布任务：``DRAFT`` → ``PUBLISHED``；已发布幂等（不改 ``published_at``）。

    即使幂等返回，也先按统一锁顺序（课程 → 任务 → 当前评分版本）取到版本行锁，
    保证发布事务与其他写事务的加锁顺序一致。
    """
    require_publishable(assignment)
    await lock_current_rubric_version(session, assignment=assignment)
    if assignment.status is AssignmentStatus.PUBLISHED:
        return assignment

    timestamp = now or utc_now()
    assignment.status = AssignmentStatus.PUBLISHED
    assignment.published_at = timestamp
    assignment.updated_at = timestamp
    await session.commit()
    return assignment


async def close_assignment(
    session: AsyncSession, *, assignment: Assignment, now: datetime | None = None
) -> Assignment:
    """关闭任务：``PUBLISHED`` → ``CLOSED``；已关闭幂等（不改 ``closed_at``）。

    与发布一样，幂等返回前也先取到当前评分版本行锁。
    """
    require_closable(assignment)
    await lock_current_rubric_version(session, assignment=assignment)
    if assignment.status is AssignmentStatus.CLOSED:
        return assignment

    timestamp = now or utc_now()
    assignment.status = AssignmentStatus.CLOSED
    assignment.closed_at = timestamp
    assignment.updated_at = timestamp
    await session.commit()
    return assignment


async def reopen_assignment(
    session: AsyncSession, *, assignment: Assignment, now: datetime | None = None
) -> Assignment:
    """重新开启任务：``CLOSED`` → ``PUBLISHED``；已发布幂等。

    与关闭对称，但**不改变发布时间**：``published_at`` 记录的是"这份任务第一次
    对学生开放"的时间，开关一次不该把它改写。``closed_at`` 则被清空 —— 该字段
    表示"当前处于关闭状态"的时刻，任务重新开放后它不再成立。

    评分规则版本不受影响：重新开启不涉及评分项的实质变化，因此不会生成新版本，
    学生此前提交的报告仍然按提交时固定的版本评分。
    学生此前的提交也**不会**被清空，重新开启只是重新开放收作业的入口。
    """
    require_reopenable(assignment)
    await lock_current_rubric_version(session, assignment=assignment)
    if assignment.status is AssignmentStatus.PUBLISHED:
        return assignment

    timestamp = now or utc_now()
    assignment.status = AssignmentStatus.PUBLISHED
    assignment.closed_at = None
    assignment.updated_at = timestamp
    await session.commit()
    return assignment


__all__ = [
    "AssignmentDetail",
    "AssignmentListItem",
    "can_submit",
    "close_assignment",
    "create_assignment",
    "current_rubric_version_id",
    "get_assignment_detail",
    "get_rubric_snapshot",
    "list_assignments",
    "lock_attachment_writable_assignment",
    "lock_creator_course",
    "lock_current_rubric_version",
    "lock_writable_assignment",
    "publish_assignment",
    "reopen_assignment",
    "require_attachment_writable",
    "require_closable",
    "require_editable",
    "require_publishable",
    "require_reopenable",
    "update_assignment",
]
