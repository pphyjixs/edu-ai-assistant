"""``generate_practice`` 写工具（开发方案 5.3 / 5.4）。

**只创建 ``PracticeSet + PRACTICE_GENERATE Job``**，实际出题仍由现有 Worker 完成
——工具本身不调用第二次 LLM，也不等待出题结束。

执行条件（全部满足才执行，任一条不满足即拒绝）：

1. 当前用户是**课程创建教师**（``ToolContext.is_course_teacher`` + 领域 service 复核）；
2. 用户本轮文本明确表达生成/创建意图（由 orchestration 的写策略判定）；
3. ``material_ids``、数量、题型、难度齐全且通过现有 ``PracticeGenerateRequest``
   与资料状态校验；
4. 本 Run 尚未成功执行过写工具；
5. ``(run_id, tool_call_id)`` 幂等记录检查通过（由 orchestration 负责）。

缺少必要参数时模型应直接追问用户，不能猜数量或难度——因此这里**没有**默认值。

已知限制：现有领域接口只支持按整份资料生成，不支持只按某个章节生成。
工具的描述里写明了这一点，避免模型声称"已经按章节过滤"。
"""

from __future__ import annotations

import json
import uuid

from app.core.errors import (
    CourseArchivedError,
    CourseForbiddenError,
    MaterialNotReadyError,
    ResourceNotFoundError,
    RoleForbiddenError,
    ValidationError,
)
from app.modules.agent.tool_types import (
    AgentArtifact,
    AgentArtifactKind,
    ToolContext,
    ToolErrorCode,
    ToolResult,
    ToolSideEffect,
    ToolSpec,
)
from app.modules.auth.models import User, UserRole
from app.modules.practice import service as practice_service
from app.modules.practice import repository as practice_repo
from app.modules.practice.schemas import PracticeGenerateRequest
from app.modules.jobs import service as jobs_service

#: 写工具的可见角色：**两种角色都可见**。
#:
#: 这是刻意的：开发方案 5.4 要求「学生请求创建练习时，工具返回 FORBIDDEN，
#: 最终回答说明该功能目前仅限课程教师」。如果直接把工具从学生的 schema 里隐藏，
#: 模型只会含糊其辞或编造结果，用户反而看不到明确说明。
#: 真正的门禁在写策略（``is_course_teacher``）与领域 service 两层。
_VISIBLE_TO_BOTH = frozenset({UserRole.TEACHER, UserRole.STUDENT})


async def _generate_practice(
    ctx: ToolContext, args: PracticeGenerateRequest
) -> ToolResult:
    """创建练习与生成任务，返回可点击的 artifact。"""
    canonical = json.dumps(
        args.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    set_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{ctx.run_id}|generate_practice|{canonical}")
    async with ctx.session_factory() as session:
        user = await session.get(User, ctx.user_id)
        if user is None:  # pragma: no cover - 用户被删除后 Run 已不可达
            return ToolResult.failure(ToolErrorCode.RESOURCE_NOT_FOUND, "当前用户不可用。")
        try:
            # 统一锁顺序：课程行锁 → 成员/创建教师/归档检查 → 资料可见性与就绪
            course = await practice_service.lock_teacher_course(
                session, user=user, course_id=ctx.course_id
            )
            practice_set = await practice_repo.get_set_by_id(session, set_id)
            if practice_set is None:
                practice_set, job = await practice_service.create_practice_set(
                    session, course=course, user=user, payload=args, set_id=set_id
                )
            else:
                if practice_set.course_id != ctx.course_id or practice_set.teacher_id != ctx.user_id:
                    return ToolResult.failure(ToolErrorCode.FORBIDDEN, "练习记录不可用。")
                job = await jobs_service.get_practice_generate_job(
                    session, practice_set_id=set_id
                )
                if job is None:
                    return ToolResult.failure(ToolErrorCode.INTERNAL, "练习生成任务不可用。")
        except (RoleForbiddenError, CourseForbiddenError):
            return ToolResult.failure(
                ToolErrorCode.FORBIDDEN,
                "只有这门课程的创建教师才能生成正式练习。",
            )
        except CourseArchivedError:
            return ToolResult.failure(
                ToolErrorCode.INVALID_STATE, "课程已归档，不能再创建练习。"
            )
        except MaterialNotReadyError:
            return ToolResult.failure(
                ToolErrorCode.INVALID_STATE,
                "所选资料尚未解析完成（或没有可检索内容），暂时无法据此出题。",
            )
        except ResourceNotFoundError:
            return ToolResult.failure(
                ToolErrorCode.RESOURCE_NOT_FOUND,
                "所选资料不在本课程中，或已经被删除。",
            )
        except ValidationError:
            return ToolResult.failure(
                ToolErrorCode.INVALID_TOOL_ARGUMENTS,
                "练习参数不符合要求（资料数量、题数、题型或难度）。",
            )

    return ToolResult(
        ok=True,
        data={
            "practice_set_id": str(practice_set.id),
            "job_id": str(job.id),
            "status": job.status.value,
            "question_count": args.question_count,
            "question_types": [item.value for item in args.question_types],
            "difficulty": args.difficulty.value,
            # 领域接口的限制要如实告诉模型，避免它声称"只按某章节出题"
            "note": "练习按所选整份资料生成，暂不支持只按某个章节生成。",
        },
        artifacts=[
            AgentArtifact(
                kind=AgentArtifactKind.PRACTICE_SET,
                id=practice_set.id,
                job_id=job.id,
                status=job.status.value,
                href=f"/courses/{ctx.course_id}/learn",
            )
        ],
    )


SPECS: list[ToolSpec] = [
    ToolSpec(
        name="generate_practice",
        description=(
            "为当前课程创建一套正式练习（由后台任务出题，稍后可在练习页查看进度）。"
            "仅课程创建教师可用。必须提供 material_ids（可用 list_course_materials 取得）、"
            "question_count、question_types 与 difficulty，缺少任何一项时先向用户询问，"
            "不要自行假设。练习按所选整份资料生成，不支持只按某个章节生成。"
        ),
        # 直接复用现有请求模型：参数形状与领域接口完全一致（开发方案 5.3）
        input_model=PracticeGenerateRequest,
        side_effect=ToolSideEffect.WRITE,
        allowed_roles=_VISIBLE_TO_BOTH,
        handler=_generate_practice,
    )
]


__all__ = ["SPECS"]
