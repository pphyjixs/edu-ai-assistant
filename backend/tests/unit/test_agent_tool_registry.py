"""Agent 工具注册表的单元测试（开发方案 10.1 第 1–3 条）。

这些用例不接触数据库，也不需要真实模型：注册表是纯逻辑，
参数契约完全由 Pydantic 决定。
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.modules.agent.skills import load_skill_catalog
from app.modules.agent.tool_registry import (
    ToolRegistrationError,
    ToolRegistry,
    UnknownToolError,
)
from app.modules.agent.tool_types import (
    ToolContext,
    ToolResult,
    ToolSideEffect,
    ToolSpec,
)
from app.modules.agent.tools import build_default_registry
from app.modules.auth.models import UserRole

_BOTH = frozenset({UserRole.TEACHER, UserRole.STUDENT})


class _EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class _LooseInput(BaseModel):
    """没有 ``extra="forbid"`` 的输入模型：注册时必须被拒绝。"""

    text: str = ""


async def _echo_handler(ctx: ToolContext, args: _EchoInput) -> ToolResult:  # pragma: no cover
    return ToolResult(ok=True, data={"echo": args.text})


def _spec(
    name: str = "echo",
    *,
    side_effect: ToolSideEffect = ToolSideEffect.READ,
    roles: frozenset[UserRole] = _BOTH,
    input_model: type[BaseModel] = _EchoInput,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="回显输入",
        input_model=input_model,
        side_effect=side_effect,
        allowed_roles=roles,
        handler=_echo_handler,
    )


def _ctx(role: UserRole, make_settings, *, is_teacher: bool = True) -> ToolContext:
    return ToolContext(
        run_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        course_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        user_role=role,
        is_course_teacher=is_teacher,
        session_factory=None,  # type: ignore[arg-type] - 注册测试不执行 handler
        settings=make_settings(),
        question="总结这门课",
    )


# --------------------------------------------------------------------------- #
# 注册：名称与输入契约
# --------------------------------------------------------------------------- #
def test_register_rejects_invalid_and_duplicate_names() -> None:
    """非法工具名与重复工具名必须直接拒绝（开发方案 10.1 第 1 条）。"""
    registry = ToolRegistry()

    with pytest.raises(ToolRegistrationError):
        registry.register(_spec("Bad.Name"))
    with pytest.raises(ToolRegistrationError):
        registry.register(_spec("with-dash"))
    with pytest.raises(ToolRegistrationError):
        registry.register(_spec("a" * 65))

    registry.register(_spec("good_name"))
    with pytest.raises(ToolRegistrationError):
        registry.register(_spec("good_name"))


def test_register_requires_strict_input_model() -> None:
    """输入模型必须 ``extra="forbid"``，否则模型能塞进任意键值。"""
    registry = ToolRegistry()
    with pytest.raises(ToolRegistrationError):
        registry.register(_spec("loose", input_model=_LooseInput))


def test_unknown_tool_is_rejected_without_fuzzy_matching() -> None:
    """未注册工具不做模糊匹配，也不动态 import（开发方案 10.1 第 1 条）。"""
    registry = ToolRegistry()
    registry.register(_spec("search_course_knowledge"))

    assert registry.get("search_course_knowledge_v2") is None
    assert "search_course_knowledge_v2" not in registry
    with pytest.raises(UnknownToolError):
        registry.require("search-course-knowledge")


# --------------------------------------------------------------------------- #
# 默认注册表：角色过滤与参数契约
# --------------------------------------------------------------------------- #
def test_default_registry_covers_documented_tools(tmp_path, make_settings) -> None:
    """第一批工具都在默认注册表里，且不含任何未声明的能力。"""
    registry = build_default_registry(load_skill_catalog(tmp_path / "empty"))
    assert set(registry.names()) == {
        "search_course_knowledge",
        "list_course_materials",
        "list_course_assignments",
        "get_assignment",
        "generate_practice",
        "load_skill",
    }


def test_schemas_for_filters_by_role(tmp_path, make_settings) -> None:
    """只把当前角色可用的工具发给模型。"""
    registry = build_default_registry(load_skill_catalog(tmp_path / "empty"))

    # 一个只允许教师的工具：学生看不到它
    class _TeacherOnly(BaseModel):
        model_config = ConfigDict(extra="forbid")

    registry.register(
        ToolSpec(
            name="teacher_only",
            description="仅教师",
            input_model=_TeacherOnly,
            side_effect=ToolSideEffect.READ,
            allowed_roles=frozenset({UserRole.TEACHER}),
            handler=_echo_handler,
        )
    )

    teacher_names = {
        schema["function"]["name"]
        for schema in registry.schemas_for(_ctx(UserRole.TEACHER, make_settings))
    }
    student_names = {
        schema["function"]["name"]
        for schema in registry.schemas_for(_ctx(UserRole.STUDENT, make_settings))
    }

    assert "teacher_only" in teacher_names
    assert "teacher_only" not in student_names
    # 学生仍然能看到写工具：权限由执行阶段返回 FORBIDDEN 说明（开发方案 5.4）
    assert "generate_practice" in student_names


def test_schemas_never_expose_identity_fields(tmp_path, make_settings) -> None:
    """工具 schema 不暴露 user_id / course_id / 角色 / 会话（开发方案 9.2）。"""
    registry = build_default_registry(load_skill_catalog(tmp_path / "empty"))
    forbidden = {"user_id", "course_id", "role", "session_id", "user_role", "run_id"}

    for schema in registry.schemas_for(_ctx(UserRole.TEACHER, make_settings)):
        parameters = schema["function"]["parameters"]
        assert parameters.get("additionalProperties") is False
        assert forbidden.isdisjoint(parameters.get("properties", {}))


def test_tool_inputs_reject_extra_fields(tmp_path) -> None:
    """模型无法通过参数覆盖 course_id / user_id / 角色（开发方案 10.1 第 3 条）。"""
    registry = build_default_registry(load_skill_catalog(tmp_path / "empty"))
    spec = registry.require("search_course_knowledge")

    with pytest.raises(ValidationError):
        spec.input_model.model_validate_json(
            '{"query":"关系代数","course_id":"00000000-0000-0000-0000-000000000001"}'
        )
    with pytest.raises(ValidationError):
        spec.input_model.model_validate_json('{"query":"关系代数","user_id":"me"}')

    # 越界 limit 同样被拒绝（服务端限制 1..8）
    with pytest.raises(ValidationError):
        spec.input_model.model_validate_json('{"query":"关系代数","limit":99}')


def test_load_skill_name_pattern_blocks_paths(tmp_path) -> None:
    """``load_skill`` 不接受路径、``..`` 或盘符（开发方案 10.1 第 11 条）。"""
    registry = build_default_registry(load_skill_catalog(tmp_path / "empty"))
    spec = registry.require("load_skill")

    for bad in ("../secret", "a/b", "C:\\secret", "..", "a.b", ""):
        with pytest.raises(ValidationError):
            spec.input_model.model_validate_json(f'{{"name":"{bad}"}}')

    assert spec.input_model.model_validate_json('{"name":"course-summary"}').name == (
        "course-summary"
    )
