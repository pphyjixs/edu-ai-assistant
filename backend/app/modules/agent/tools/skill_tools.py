"""``load_skill`` 工具（开发方案 5.3 / 第 6 节）。

只允许读取**启动时已验证并注册**的 Skill 名称。不接受路径、``..``、盘符、
URI 或资源名：输入模型的 ``pattern`` 约束 + 目录查找共同保证这一点。

正文的注入策略由 orchestrator 负责：
``load_skill`` 只负责"取到正文并声明加载了哪个 Skill"，正文在**下一轮**作为
受信任的 Skill 指令注入一次（``app.modules.agent.skills`` 的 8 KiB / 16 KiB 预算
也在那里统一执行）。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.modules.agent.skills import SkillCatalog, read_skill_instructions
from app.modules.agent.tool_types import (
    ToolContext,
    ToolErrorCode,
    ToolResult,
    ToolSideEffect,
    ToolSpec,
)
from app.modules.auth.models import UserRole

_BOTH_ROLES = frozenset({UserRole.TEACHER, UserRole.STUDENT})

#: 只允许已注册的 Skill 名：小写字母、数字与连字符
SKILL_NAME_PATTERN = r"^[a-z0-9-]{1,64}$"


class LoadSkillInput(BaseModel):
    """``load_skill`` 的输入。

    ``pattern`` 不接受斜杠、点号或盘符，因此路径穿越在**参数校验阶段**就被拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=SKILL_NAME_PATTERN,
        description="Skill 名称，必须与 system prompt 中列出的名称完全一致",
    )


def build_specs(catalog: SkillCatalog) -> list[ToolSpec]:
    """按给定目录构造 ``load_skill`` 的 ToolSpec。

    目录在 Worker 启动时扫描并冻结，这里把它的查找能力闭包进 handler，
    因此 handler 无法访问注册之外的任何路径。
    """

    async def _load_skill(ctx: ToolContext, args: LoadSkillInput) -> ToolResult:
        meta = catalog.get(args.name)
        if meta is None:
            return ToolResult.failure(
                ToolErrorCode.SKILL_NOT_FOUND,
                f"没有名为 {args.name} 的 Skill。只能使用 system prompt 中列出的名称。",
            )
        try:
            instructions = read_skill_instructions(meta, catalog=catalog)
        except ValueError:
            # 目录可能在启动后被改动；按不可用处理，不泄露路径
            return ToolResult.failure(
                ToolErrorCode.SKILL_NOT_FOUND, f"Skill {args.name} 当前不可用。"
            )
        return ToolResult(
            ok=True,
            data={
                "name": meta.name,
                "version": meta.version,
                # 正文由 orchestrator 作为受信任指令注入下一轮上下文（只注入一次）
                "instructions": instructions,
            },
            loaded_skills=[meta.name],
        )

    return [
        ToolSpec(
            name="load_skill",
            description=(
                "加载一个任务工作流（Skill）的完整说明。"
                "只有在 system prompt 的 Skill 目录中列出的名称才可以加载；"
                "加载成功后才能声称使用了该 Skill。"
            ),
            input_model=LoadSkillInput,
            side_effect=ToolSideEffect.READ,
            allowed_roles=_BOTH_ROLES,
            handler=_load_skill,
        )
    ]


__all__ = ["LoadSkillInput", "SKILL_NAME_PATTERN", "build_specs"]
