"""Skill 目录扫描与按需加载的单元测试（开发方案 10.1 第 10–12 条）。

全部使用 ``tmp_path`` 下的 fixture 目录：生产 Skill 目录刻意保持为空，
测试不能依赖仓库里存在某个正式 Skill。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.modules.agent import skills
from app.modules.agent.tools.skill_tools import build_specs

_GOOD_BODY = "# 工作流\n\n1. 先检索资料。\n2. 再按要点归纳。\n"


def _write_skill(
    root: Path,
    directory: str,
    *,
    name: str | None = None,
    description: str = "总结课程或课件时使用。",
    version: str = "1",
    enabled: str = "true",
    body: str = _GOOD_BODY,
    extra_frontmatter: str = "",
) -> Path:
    """在 ``root`` 下写一个 Skill 目录，返回目录路径。"""
    skill_dir = root / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f"name: {name if name is not None else directory}\n"
        f"description: {description}\n"
        f'version: "{version}"\n'
        f"enabled: {enabled}\n"
        f"{extra_frontmatter}"
        "---\n"
    )
    (skill_dir / "SKILL.md").write_text(frontmatter + body, encoding="utf-8")
    return skill_dir


# --------------------------------------------------------------------------- #
# 目录扫描
# --------------------------------------------------------------------------- #
def test_missing_root_is_an_empty_catalog(tmp_path: Path) -> None:
    """生产目录可以为空：没有目录不是错误（开发方案第 6 节）。"""
    catalog = skills.load_skill_catalog(tmp_path / "does-not-exist")
    assert len(catalog) == 0
    assert catalog.names() == []


def test_valid_skill_is_registered_with_metadata(tmp_path: Path) -> None:
    _write_skill(tmp_path, "course-summary", description="总结整门课程时使用。")
    catalog = skills.load_skill_catalog(tmp_path)

    assert catalog.names() == ["course-summary"]
    meta = catalog.get("course-summary")
    assert meta is not None
    assert meta.version == "1"
    assert meta.description == "总结整门课程时使用。"
    assert skills.read_skill_instructions(meta, catalog=catalog).startswith("# 工作流")


@pytest.mark.parametrize(
    ("case", "kwargs", "expected"),
    [
        ("name-mismatch", {"name": "other-name"}, "与目录名不一致"),
        ("unknown-field", {"extra_frontmatter": "owner: someone\n"}, "未知字段"),
        ("disabled", {"enabled": "false"}, "enabled: false"),
        ("long-description", {"description": "长" * 400}, "description 超过"),
        ("empty-body", {"body": "   \n"}, "正文为空"),
        ("bad-version", {"version": ""}, "version"),
    ],
)
def test_invalid_skill_is_disabled_with_reason(
    tmp_path: Path, case: str, kwargs: dict, expected: str
) -> None:
    """非法 Skill 被禁用并记录原因，而不是让 Worker 启动失败。"""
    _write_skill(tmp_path, "broken-skill", **kwargs)
    catalog = skills.load_skill_catalog(tmp_path)

    assert len(catalog) == 0
    assert any(expected in reason for _name, reason in catalog.rejected), catalog.rejected


def test_oversized_body_is_rejected(tmp_path: Path) -> None:
    """正文超过 8 KiB 的 Skill 不可加载（开发方案第 6 节）。"""
    _write_skill(tmp_path, "huge-skill", body="x" * (skills.SKILL_BODY_MAX_BYTES + 1))
    catalog = skills.load_skill_catalog(tmp_path)

    assert len(catalog) == 0
    assert any("正文超过" in reason for _name, reason in catalog.rejected)


def test_invalid_directory_name_is_rejected(tmp_path: Path) -> None:
    _write_skill(tmp_path, "Bad_Name")
    catalog = skills.load_skill_catalog(tmp_path)
    assert len(catalog) == 0


def test_skill_file_outside_root_is_rejected(tmp_path: Path) -> None:
    """解析后的路径必须仍在 Skill 根目录内（防符号链接逃逸）。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: escape\ndescription: 逃逸\nversion: \"1\"\nenabled: true\n---\n正文\n",
        encoding="utf-8",
    )

    root = tmp_path / "root"
    root.mkdir()
    skill_dir = root / "escape"
    skill_dir.mkdir()
    link = skill_dir / "SKILL.md"
    try:
        os.symlink(outside / "SKILL.md", link)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows 无权限
        pytest.skip("当前平台不支持创建符号链接")

    catalog = skills.load_skill_catalog(root)
    assert len(catalog) == 0
    assert any("不在 Skill 根目录内" in reason for _name, reason in catalog.rejected)


# --------------------------------------------------------------------------- #
# 目录渲染（system prompt）
# --------------------------------------------------------------------------- #
def test_empty_catalog_says_no_skill(tmp_path: Path) -> None:
    """没有 Skill 时明确说明，绝不虚构能力（开发方案 11.3）。"""
    catalog = skills.load_skill_catalog(tmp_path)
    assert skills.render_catalog(catalog) == "当前没有已启用的 Skill。"


def test_catalog_render_stays_within_budget(tmp_path: Path) -> None:
    """目录总长超过 4 KiB 时公平截断，且仍为合法文本（开发方案 10.1 第 12 条）。"""
    for index in range(30):
        _write_skill(
            tmp_path,
            f"skill-{index:02d}",
            description="说明" * 120,
        )
    catalog = skills.load_skill_catalog(tmp_path)
    assert len(catalog) == 30

    rendered = skills.render_catalog(catalog)
    assert len(rendered.encode("utf-8")) <= skills.CATALOG_MAX_BYTES
    # 公平截断：每一条 Skill 都仍然出现
    for meta in catalog.skills:
        assert f"- {meta.name}：" in rendered


# --------------------------------------------------------------------------- #
# load_skill 工具
# --------------------------------------------------------------------------- #
def _ctx(make_settings, **overrides):
    import uuid

    from app.modules.agent.tool_types import ToolContext
    from app.modules.auth.models import UserRole

    values = {
        "run_id": uuid.uuid4(),
        "session_id": uuid.uuid4(),
        "course_id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "user_role": UserRole.TEACHER,
        "is_course_teacher": True,
        "session_factory": None,
        "settings": make_settings(),
        "question": "帮我总结这门课",
    }
    values.update(overrides)
    return ToolContext(**values)  # type: ignore[arg-type]


async def test_load_skill_returns_instructions_once(tmp_path: Path, make_settings) -> None:
    _write_skill(tmp_path, "course-summary")
    catalog = skills.load_skill_catalog(tmp_path)
    spec = build_specs(catalog)[0]

    result = await spec.handler(
        _ctx(make_settings), spec.input_model.model_validate({"name": "course-summary"})
    )

    assert result.ok is True
    assert result.loaded_skills == ["course-summary"]
    assert result.data["version"] == "1"
    assert "先检索资料" in str(result.data["instructions"])


async def test_load_skill_unknown_name_is_rejected(tmp_path: Path, make_settings) -> None:
    _write_skill(tmp_path, "course-summary")
    catalog = skills.load_skill_catalog(tmp_path)
    spec = build_specs(catalog)[0]

    result = await spec.handler(
        _ctx(make_settings), spec.input_model.model_validate({"name": "not-a-skill"})
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "SKILL_NOT_FOUND"
    # 不泄露目录结构或路径
    assert "SKILL.md" not in result.error.message
