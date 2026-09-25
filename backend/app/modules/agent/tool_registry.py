"""工具注册表（开发方案 5.2）。

注册表是**唯一可调用清单**：模型不能传 Python 路径、SQL、URL 或任意函数名，
只能引用注册表里的名称。因此这里做三件事：

1. 校验名称与唯一性（``[a-z0-9_]{1,64}``，重名直接拒绝）；
2. 提供按角色过滤的 function schema（只把当前用户真正能用的工具发给模型）；
3. 提供严格查找：未注册名称返回 ``None`` / 抛 :class:`UnknownToolError`，
   **不做模糊匹配、不动态 import**。

参数校验、权限判定与幂等执行在 :mod:`app.modules.agent.orchestration`；
handler 本身的实现放在 :mod:`app.modules.agent.tools` 下。
"""

from __future__ import annotations

import logging
import re

from app.modules.agent.tool_types import TOOL_NAME_MAX_LENGTH, ToolContext, ToolSpec

logger = logging.getLogger("app.agent.tools")

#: 工具名只允许小写字母、数字与下划线——避免不同模型对点号/冒号命名的兼容问题
_TOOL_NAME_RE = re.compile(rf"^[a-z0-9_]{{1,{TOOL_NAME_MAX_LENGTH}}}$")


class ToolRegistrationError(ValueError):
    """注册被拒绝（名称非法或重复）。属于**开发期**错误，应当在启动时暴露。"""


class UnknownToolError(LookupError):
    """模型调用了未注册的工具。"""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


class ToolRegistry:
    """不可变语义的注册表：注册完成后只读。"""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    # ------------------------------ 注册 ------------------------------ #
    def register(self, spec: ToolSpec) -> ToolSpec:
        """注册一个工具；名称非法或重复时抛 :class:`ToolRegistrationError`。"""
        if not _TOOL_NAME_RE.match(spec.name):
            raise ToolRegistrationError(
                f"工具名非法：{spec.name!r}（只允许 [a-z0-9_]{{1,{TOOL_NAME_MAX_LENGTH}}}）"
            )
        if spec.name in self._specs:
            raise ToolRegistrationError(f"工具名重复：{spec.name!r}")
        if spec.input_model.model_config.get("extra") != "forbid":
            # 输入模型必须拒绝未声明字段，否则模型能塞进任意键值
            raise ToolRegistrationError(
                f"工具 {spec.name!r} 的输入模型必须设置 extra='forbid'"
            )
        self._specs[spec.name] = spec
        return spec

    def register_all(self, specs: list[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    # ------------------------------ 查找 ------------------------------ #
    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def require(self, name: str) -> ToolSpec:
        """严格查找；未注册时抛 :class:`UnknownToolError`。"""
        spec = self._specs.get(name)
        if spec is None:
            raise UnknownToolError(name)
        return spec

    def names(self) -> list[str]:
        return list(self._specs)

    def __len__(self) -> int:  # pragma: no cover - 便于测试与日志
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._specs

    # ------------------------------ 权限 ------------------------------ #
    @staticmethod
    def is_allowed(spec: ToolSpec, ctx: ToolContext) -> bool:
        """角色是否允许调用该工具。

        课程级权限（是否为创建教师、资源归属）由 handler 与写策略分别复核，
        这里只做平台角色这一层。
        """
        return ctx.user_role in spec.allowed_roles

    def schemas_for(self, ctx: ToolContext) -> list[dict]:
        """发给模型的 function tool 列表（只含当前角色可用的工具）。"""
        return [
            spec.function_schema()
            for spec in self._specs.values()
            if self.is_allowed(spec, ctx)
        ]

    def descriptions_for(self, ctx: ToolContext) -> list[tuple[str, str]]:
        """system prompt 里的工具摘要（名称 + 一句话用途）。"""
        return [
            (spec.name, spec.description)
            for spec in self._specs.values()
            if self.is_allowed(spec, ctx)
        ]


__all__ = [
    "ToolRegistrationError",
    "ToolRegistry",
    "UnknownToolError",
]
