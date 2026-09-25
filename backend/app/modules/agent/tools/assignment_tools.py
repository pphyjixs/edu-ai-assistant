"""作业相关工具（开发方案 5.3）。

- :func:`list_course_assignments` 复用 ``assignments/service.list_assignments``：
  学生只能看到已发布/已关闭/已归档且对其可见的作业，教师按现有 service 规则读取；
- :func:`get_assignment` 复用 ``assignments/service.get_assignment_detail``
  的详情与评分标准读取逻辑。

两者都**不复制 SQL**，也**不提前判权限**：越权与跨课程统一在 service 里变成
``RESOURCE_NOT_FOUND``，工具再把它转成稳定错误码返回给模型。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.errors import ResourceNotFoundError
from app.core.pagination import MAX_PAGE_SIZE, PaginationParams
from app.modules.agent.tool_types import (
    ToolContext,
    ToolErrorCode,
    ToolEvidence,
    ToolResult,
    ToolSideEffect,
    ToolSpec,
)
from app.modules.agent.tools._shared import read_session
from app.modules.assignments import service as assignments_service
from app.modules.assignments.models import AssignmentStatus
from app.modules.auth.models import User, UserRole

_BOTH_ROLES = frozenset({UserRole.TEACHER, UserRole.STUDENT})

#: 一次最多返回的作业条数（分页上限内的单页）
ASSIGNMENT_LIST_LIMIT = MAX_PAGE_SIZE


class ListCourseAssignmentsInput(BaseModel):
    """``list_course_assignments`` 的输入。"""

    model_config = ConfigDict(extra="forbid")

    status: AssignmentStatus | None = Field(
        default=None, description="可选：只看这个状态的作业"
    )
    due_before: datetime | None = Field(
        default=None,
        description="可选：只看截止时间早于该时刻的作业（ISO 8601，无时区按 UTC 处理）",
    )

    @field_validator("due_before")
    @classmethod
    def _require_aware(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        # 模型常给出不带时区的时间；统一按 UTC 解释，避免与库里的 UTC 时间比较错位
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class GetAssignmentInput(BaseModel):
    """``get_assignment`` 的输入。"""

    model_config = ConfigDict(extra="forbid")

    assignment_id: uuid.UUID = Field(description="作业 id（来自课程作业列表）")


async def _load_user(ctx: ToolContext, session) -> User | None:
    """读取当前用户 ORM 对象，供复用领域 service 的鉴权入口。"""
    return await session.get(User, ctx.user_id)


async def _list_course_assignments(
    ctx: ToolContext, args: ListCourseAssignmentsInput
) -> ToolResult:
    async with read_session(ctx) as session:
        user = await _load_user(ctx, session)
        if user is None:  # pragma: no cover - 用户被删除后 Run 已不可达
            return ToolResult.failure(ToolErrorCode.RESOURCE_NOT_FOUND, "当前用户不可用。")
        try:
            items, _total = await assignments_service.list_assignments(
                session,
                user=user,
                course_id=ctx.course_id,
                pagination=PaginationParams(page=1, page_size=ASSIGNMENT_LIST_LIMIT),
            )
        except ResourceNotFoundError:
            return ToolResult.failure(
                ToolErrorCode.RESOURCE_NOT_FOUND, "当前课程不可见或不存在。"
            )

        # 事务回滚会让 ORM 实例过期，因此**在会话内**就把需要的字段取成纯数据
        rows: list[dict] = []
        for item in items:
            assignment = item.assignment
            if args.status is not None and assignment.status is not args.status:
                continue
            if args.due_before is not None:
                if assignment.due_at is None or assignment.due_at >= args.due_before:
                    continue
            rows.append(
                {
                    "id": str(assignment.id),
                    "title": assignment.title,
                    "status": assignment.status.value,
                    "due_at": assignment.due_at.isoformat() if assignment.due_at else None,
                    "allow_late_submission": assignment.allow_late_submission,
                    "total_score": str(item.total_score),
                }
            )

    return ToolResult(ok=True, data={"assignments": rows, "count": len(rows)})


async def _get_assignment(ctx: ToolContext, args: GetAssignmentInput) -> ToolResult:
    async with read_session(ctx) as session:
        user = await _load_user(ctx, session)
        if user is None:  # pragma: no cover
            return ToolResult.failure(ToolErrorCode.RESOURCE_NOT_FOUND, "当前用户不可用。")
        try:
            detail = await assignments_service.get_assignment_detail(
                session, user=user, assignment_id=args.assignment_id
            )
        except ResourceNotFoundError:
            # 不存在 / 不属于本课程 / 对学生不可见统一不泄露标题
            return ToolResult.failure(
                ToolErrorCode.RESOURCE_NOT_FOUND, "该作业不在本课程中或对当前用户不可见。"
            )

        assignment = detail.assignment
        lines = [f"作业：{assignment.title}", f"状态：{assignment.status.value}"]
        if detail.total_score:
            lines.append(f"总分：{detail.total_score}")
        if assignment.due_at is not None:
            lines.append(f"截止时间：{assignment.due_at.isoformat()}（UTC）")
            lines.append(f"允许补交：{'是' if assignment.allow_late_submission else '否'}")
        if assignment.description:
            lines.append(f"任务说明：{assignment.description}")

        evidence = [
            ToolEvidence(
                source_type="ASSIGNMENT",
                source_id=assignment.id,
                label=f"作业《{assignment.title}》· 要求",
                text="\n".join(lines),
                groundable=True,
                display_kind="ASSIGNMENT",
            )
        ]
        if detail.rubric_items:
            rubric_lines = ["评分标准："]
            for item in detail.rubric_items:
                description = f"（{item.description}）" if item.description else ""
                rubric_lines.append(f"- {item.title}{description}：{item.max_score} 分")
            evidence.append(
                ToolEvidence(
                    source_type="ASSIGNMENT",
                    source_id=assignment.id,
                    label=f"作业《{assignment.title}》· 评分标准",
                    text="\n".join(rubric_lines),
                    groundable=True,
                    display_kind="ASSIGNMENT",
                )
            )

        data = {
            "id": str(assignment.id),
            "title": assignment.title,
            "status": assignment.status.value,
            "rubric_version": detail.rubric_version,
            "total_score": str(detail.total_score),
            "due_at": assignment.due_at.isoformat() if assignment.due_at else None,
            "allow_late_submission": assignment.allow_late_submission,
            "rubric_items": [
                {
                    "title": item.title,
                    "description": item.description,
                    "max_score": str(item.max_score),
                }
                for item in detail.rubric_items
            ],
        }

    return ToolResult(ok=True, data=data, evidence=evidence)


SPECS: list[ToolSpec] = [
    ToolSpec(
        name="list_course_assignments",
        description=(
            "列出本课程当前可见的作业（标题、状态、截止时间、总分）。"
            "回答「最近有哪些作业」「哪些快截止」时使用。学生看不到草稿作业。"
        ),
        input_model=ListCourseAssignmentsInput,
        side_effect=ToolSideEffect.READ,
        allowed_roles=_BOTH_ROLES,
        handler=_list_course_assignments,
    ),
    ToolSpec(
        name="get_assignment",
        description=(
            "读取一份作业的完整要求与评分标准（含各评分项分值）。"
            "需要拆解任务、解释评分标准或检查作业要求时使用。"
        ),
        input_model=GetAssignmentInput,
        side_effect=ToolSideEffect.READ,
        allowed_roles=_BOTH_ROLES,
        handler=_get_assignment,
    ),
]


__all__ = [
    "ASSIGNMENT_LIST_LIMIT",
    "GetAssignmentInput",
    "ListCourseAssignmentsInput",
    "SPECS",
]
