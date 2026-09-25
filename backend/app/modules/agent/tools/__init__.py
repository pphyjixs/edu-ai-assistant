"""Agent 工具包（开发方案 5.1）。

所有可调用工具的**唯一注册入口**：``build_default_registry()`` 返回的注册表就是
模型能看到的全部能力，除此之外没有任何动态查找或 import。

Skill 目录可以通过参数注入：
- Worker 进程启动时扫描一次生产目录（``backend/app/agent_skills``）并复用；
- 测试注入临时目录下的 fixture，验证「目录只进 system prompt、正文按需加载」。
"""

from __future__ import annotations

from app.modules.agent.skills import SkillCatalog, load_skill_catalog
from app.modules.agent.tool_registry import ToolRegistry

from . import assignment_tools, course_tools, practice_tools, skill_tools


def build_default_registry(skill_catalog: SkillCatalog | None = None) -> ToolRegistry:
    """构造默认注册表。

    :param skill_catalog: 启动时冻结的 Skill 目录；未提供时按需扫描生产目录
        （生产目录可以为空）。
    """
    catalog = skill_catalog if skill_catalog is not None else load_skill_catalog()

    registry = ToolRegistry()
    registry.register_all(course_tools.SPECS)
    registry.register_all(assignment_tools.SPECS)
    registry.register_all(practice_tools.SPECS)
    registry.register_all(skill_tools.build_specs(catalog))
    return registry


__all__ = ["build_default_registry"]
