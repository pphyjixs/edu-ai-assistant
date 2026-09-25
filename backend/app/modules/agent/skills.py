"""Skill 目录扫描与按需加载（开发方案第 6 节，参照 Codex 的分层做法）。

范围刻意很小：

- 本模块扫描给定的 Skill 根目录。系统 Skill 在
  ``backend/app/agent_skills/``；个人 Skill 由 ``user_skills`` 在持久化目录管理；
- 目录在 Worker 启动时扫描并冻结（本模块提供扫描函数，进程内缓存由调用方决定）；
- 对模型只暴露 ``load_skill(name)``，不做分页 ``list``——
  可用 Skill 已在 system prompt 中列出且数量很少；
- 首次请求只注入**名称、用途与版本**（目录元数据），正文在模型调用
  ``load_skill`` 之后才注入，因此单个 Skill 不会长期占用上下文。

安全校验（非法 Skill 在启动时记录错误并**禁用**，不影响其他 Skill）：

- ``name`` 与目录名一致，且匹配 ``[a-z0-9-]{1,64}``；
- 名称不能重复；``description <= 300`` 字符；正文 UTF-8 且不超过 32 KiB；
- frontmatter 未知字段直接拒绝；
- 只按注册项解析后的绝对路径读取，且解析后必须仍在 Skill 根目录内；
- 目录展示总长度不超过 16 KiB，超限公平截断 description 并记录遗漏数量。

:mod:`frontmatter` 解析刻意手写（只支持 ``key: value``），
避免为一个极小的子集引入 YAML 依赖。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("app.agent.skills")

#: Skill 名：小写字母、数字与连字符
SKILL_NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")

#: Skill 描述长度上限（字符）
DESCRIPTION_MAX_CHARS = 300

#: 单个 Skill 正文的字节上限
SKILL_BODY_MAX_BYTES = 32 * 1024

#: 一个 Run 内全部已加载 Skill 正文的字节上限
LOADED_SKILLS_TOTAL_MAX_BYTES = 64 * 1024

#: system prompt 中 Skill 目录的总字节上限
CATALOG_MAX_BYTES = 16 * 1024

#: 允许出现在 frontmatter 里的字段；其余一律拒绝
_ALLOWED_FRONTMATTER_KEYS = frozenset({"name", "description", "version", "enabled"})

_SKILL_FILENAME = "SKILL.md"


def default_skills_root() -> Path:
    """生产 Skill 根目录：``backend/app/agent_skills``。

    本文件位于 ``app/modules/agent/skills.py``，因此要向上**三级**才是 ``app/``：
    ``parents[0]``=agent、``parents[1]``=modules、``parents[2]``=app。
    目录约定见 ``docs/home-chat-agent-tools-development-plan.md`` 第 6 节
    （``backend/app/agent_skills/<skill-name>/SKILL.md``）。
    """
    return Path(__file__).resolve().parents[2] / "agent_skills"


@dataclass(frozen=True, slots=True)
class SkillMeta:
    """一个已验证的 Skill 的**目录元数据**（不含正文）。"""

    name: str
    description: str
    version: str
    directory: Path
    root: Path | None = None

    @property
    def path(self) -> Path:
        return self.directory / _SKILL_FILENAME


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """启动时冻结的 Skill 目录。"""

    root: Path
    skills: tuple[SkillMeta, ...] = ()
    #: 扫描时被拒绝的 Skill（名称 → 原因），供启动日志与排错
    rejected: tuple[tuple[str, str], ...] = ()

    def get(self, name: str) -> SkillMeta | None:
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None

    def __len__(self) -> int:
        return len(self.skills)

    def names(self) -> list[str]:
        return [skill.name for skill in self.skills]


def _parse_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """解析 ``---`` 包裹的 frontmatter，返回 (字段, 正文)。

    只支持 ``key: value``（值可加引号）。缺少 frontmatter 或格式不对时抛
    :class:`ValueError`——这类 Skill 会被禁用而不是静默忽略。
    """
    text = raw.lstrip("\ufeff")
    if not text.startswith("---"):
        raise ValueError("缺少 frontmatter")
    parts = text.split("\n")
    if parts[0].strip() != "---":
        raise ValueError("frontmatter 起始行必须是 ---")
    try:
        end_index = next(
            index for index, line in enumerate(parts[1:], start=1) if line.strip() == "---"
        )
    except StopIteration as exc:
        raise ValueError("frontmatter 缺少结束行") from exc

    fields: dict[str, str] = {}
    for line in parts[1:end_index]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise ValueError(f"frontmatter 行格式非法：{stripped!r}")
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key not in _ALLOWED_FRONTMATTER_KEYS:
            raise ValueError(f"frontmatter 含未知字段：{key}")
        if key in fields:
            raise ValueError(f"frontmatter 字段重复：{key}")
        fields[key] = value

    body = "\n".join(parts[end_index + 1 :])
    return fields, body


def _load_one(directory: Path, *, root: Path) -> SkillMeta:
    """校验单个 Skill 目录，返回元数据；任何问题都抛 :class:`ValueError`。"""
    name = directory.name
    if not SKILL_NAME_RE.match(name):
        raise ValueError(f"目录名不符合 [a-z0-9-]{{1,64}}：{name}")

    skill_file = directory / _SKILL_FILENAME
    resolved = skill_file.resolve()
    # 解析后必须仍在 Skill 根目录内（防符号链接逃逸）
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("SKILL.md 解析后不在 Skill 根目录内")
    if not resolved.is_file():
        raise ValueError("缺少 SKILL.md")

    raw = resolved.read_text(encoding="utf-8")
    fields, body = _parse_frontmatter(raw)

    declared = fields.get("name", "").strip()
    if declared != name:
        raise ValueError(f"frontmatter name({declared!r}) 与目录名不一致")

    description = fields.get("description", "").strip()
    if not description:
        raise ValueError("缺少 description")
    if len(description) > DESCRIPTION_MAX_CHARS:
        raise ValueError(f"description 超过 {DESCRIPTION_MAX_CHARS} 字符")

    version = fields.get("version")
    if version is None:
        raise ValueError("缺少 version")
    version = version.strip()
    if not version:
        raise ValueError("version 不能为空")
    if len(version) > 64:
        raise ValueError("version 超过 64 字符")

    enabled = (fields.get("enabled") or "true").strip().lower()
    if enabled not in {"true", "false"}:
        raise ValueError("enabled 只能是 true 或 false")
    if enabled == "false":
        raise ValueError("Skill 已标记为 enabled: false")

    if not body.strip():
        raise ValueError("正文为空")
    if len(body.encode("utf-8")) > SKILL_BODY_MAX_BYTES:
        raise ValueError(f"正文超过 {SKILL_BODY_MAX_BYTES} 字节")

    return SkillMeta(
        name=name,
        description=description,
        version=version,
        directory=resolved.parent,
        root=root,
    )


def load_skill_catalog(root: Path | None = None) -> SkillCatalog:
    """扫描并冻结 Skill 目录。

    - 根目录不存在时返回**空目录**（生产目录可以为空，不是错误）；
    - 单个 Skill 非法只禁用自己，并记录原因；
    - 名称重复时后者被禁用。
    """
    base = (root or default_skills_root()).resolve()
    if not base.is_dir():
        return SkillCatalog(root=base)

    skills: list[SkillMeta] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()

    for entry in sorted(base.iterdir(), key=lambda item: item.name):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            meta = _load_one(entry, root=base)
        except (OSError, UnicodeError, ValueError) as exc:
            rejected.append((entry.name, str(exc)))
            logger.warning("Skill %s 无效，已禁用：%s", entry.name, exc)
            continue
        if meta.name in seen:
            rejected.append((entry.name, "名称重复"))
            logger.warning("Skill %s 名称重复，已禁用", entry.name)
            continue
        seen.add(meta.name)
        skills.append(meta)

    return SkillCatalog(root=base, skills=tuple(skills), rejected=tuple(rejected))


def read_skill_instructions(meta: SkillMeta, *, catalog: SkillCatalog) -> str:
    """读取并返回 Skill 正文（不含 frontmatter）。

    只按注册项解析后的绝对路径读取，并**再校验一次**它仍在根目录内——
    目录可能在启动后被改动，不能只依赖扫描时的结论。

    :raises ValueError: 路径越界、文件消失或正文超限。
    """
    resolved = meta.path.resolve()
    if not resolved.is_relative_to((meta.root or catalog.root).resolve()):
        raise ValueError("Skill 路径越界")
    if not resolved.is_file():
        raise ValueError("Skill 文件不存在")
    raw = resolved.read_text(encoding="utf-8")
    fields, body = _parse_frontmatter(raw)
    if (
        fields.get("name", "").strip() != meta.name
        or fields.get("version", "").strip() != meta.version
    ):
        raise ValueError("Skill 元数据在目录扫描后已变化")
    if (fields.get("enabled") or "true").strip().lower() != "true":
        raise ValueError("Skill 已禁用")
    if len(body.encode("utf-8")) > SKILL_BODY_MAX_BYTES:
        raise ValueError("Skill 正文超限")
    return body.strip()


def render_catalog(catalog: SkillCatalog) -> str:
    """把 Skill 目录渲染成提示词片段（总长不超过 :data:`CATALOG_MAX_BYTES`，UTF-8）。

    超限时**公平截断**每条 description（而不是丢掉后面的 Skill），并记录被截断的
    条数与**未出现在提示词里的 Skill 数量**（公平截断下恒为 0，日志仍然写明，
    便于与"直接截断后面几条"的实现区分开）。
    """
    if len(catalog) == 0:
        return "当前没有已启用的 Skill。"

    def render(desc_limit: int) -> tuple[str, int]:
        lines = []
        shortened = 0
        for skill in catalog.skills:
            description = skill.description
            if desc_limit >= 0 and len(description) > desc_limit:
                description = description[:desc_limit] + "…"
                shortened += 1
            lines.append(f"- {skill.name}：{description}（version: {skill.version}）")
        return "\n".join(lines), shortened

    # 先按原样渲染；超限则逐步收紧 description 长度
    text, shortened = render(-1)
    if len(text.encode("utf-8")) <= CATALOG_MAX_BYTES:
        return text

    for limit in (120, 80, 60, 40, 24, 12, 0):
        text, shortened = render(limit)
        if len(text.encode("utf-8")) <= CATALOG_MAX_BYTES:
            logger.warning(
                "Skill 目录超出 %s 字节：已截断 %s/%s 条 description（截到 %s 字符），"
                "未出现的 Skill 数=0",
                CATALOG_MAX_BYTES,
                shortened,
                len(catalog),
                limit,
            )
            return text

    # 极端情况：名称总量也可能超预算；逐个加入，保证返回值仍受字节限制。
    prefix = "（Skill 目录过长，仅列出部分名称）"
    names: list[str] = []
    for skill in catalog.skills:
        candidate = prefix + "、".join([*names, skill.name])
        if len(candidate.encode("utf-8")) > CATALOG_MAX_BYTES:
            break
        names.append(skill.name)
    omitted = len(catalog) - len(names)
    logger.warning(
        "Skill 目录 %s 条超出 %s 字节，已退化为仅列名称（未出现的 Skill 数=%s）",
        len(catalog),
        CATALOG_MAX_BYTES,
        omitted,
    )
    return prefix + "、".join(names)


__all__ = [
    "CATALOG_MAX_BYTES",
    "DESCRIPTION_MAX_CHARS",
    "LOADED_SKILLS_TOTAL_MAX_BYTES",
    "SKILL_BODY_MAX_BYTES",
    "SKILL_NAME_RE",
    "SkillCatalog",
    "SkillMeta",
    "default_skills_root",
    "load_skill_catalog",
    "read_skill_instructions",
    "render_catalog",
]
