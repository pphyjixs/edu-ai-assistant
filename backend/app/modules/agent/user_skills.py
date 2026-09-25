"""个人文字 Skill：保存在持久化上传目录，每个用户只访问自己的文件。"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings
from app.modules.agent import skills

CATEGORIES = {
    "material-summary": "总结课件",
    "practice-generation": "生成题目",
    "lesson-preview": "课前预习",
    "review-plan": "复习巩固",
    "assignment-guidance": "作业指导",
    "other": "其他",
}
MAX_UPLOAD_BYTES = 40 * 1024
MAX_USER_SKILLS = 30


def owner_root(settings: Settings, user_id: uuid.UUID) -> Path:
    return Path(settings.storage_local_root).resolve() / "user-skills" / str(user_id)


def _record(directory: Path) -> dict | None:
    try:
        data = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        if data.get("category") not in CATEGORIES:
            return None
        if any(not isinstance(data.get(key), str) or not data[key] for key in (
            "id", "name", "internal_name", "description", "version", "created_at"
        )):
            return None
        if directory.name != data["internal_name"]:
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def list_user_skills(settings: Settings, user_id: uuid.UUID) -> list[dict]:
    root = owner_root(settings, user_id)
    if not root.is_dir():
        return []
    items = [_record(p) for p in root.iterdir() if p.is_dir()]
    return sorted((item for item in items if item), key=lambda item: item["created_at"], reverse=True)


def create_user_skill(settings: Settings, user_id: uuid.UUID, content: str, category: str) -> dict:
    if category not in CATEGORIES:
        raise ValueError("请选择有效的 Skill 功能")
    if len(content.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise ValueError("Skill 文件不能超过 40 KiB")
    if len(list_user_skills(settings, user_id)) >= MAX_USER_SKILLS:
        raise ValueError("个人 Skill 最多上传 30 个")
    fields, body = skills._parse_frontmatter(content)
    original_name = fields.get("name", "").strip()
    if not skills.SKILL_NAME_RE.fullmatch(original_name):
        raise ValueError("Skill 的 name 只能包含小写字母、数字和连字符，长度不超过 64")
    if not fields.get("description", "").strip() or not fields.get("version", "").strip():
        raise ValueError("Skill 需要 name、description、version 和正文")
    if not body.strip():
        raise ValueError("Skill 正文不能为空")
    root = owner_root(settings, user_id)
    root.mkdir(parents=True, exist_ok=True)
    skill_id = uuid.uuid4()
    internal_name = f"personal-{skill_id.hex}"
    directory = root / internal_name
    directory.mkdir()
    normalized = (
        "---\n"
        f"name: {internal_name}\n"
        f"description: {fields['description'].strip()}\n"
        f"version: {fields['version'].strip()}\n"
        "enabled: true\n---\n"
        f"{body.strip()}\n"
    )
    record = {
        "id": str(skill_id),
        "name": original_name,
        "internal_name": internal_name,
        "description": fields["description"].strip(),
        "version": fields["version"].strip(),
        "category": category,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        (directory / "SKILL.md").write_text(normalized, encoding="utf-8")
        catalog = skills.load_skill_catalog(root)
        if catalog.get(internal_name) is None:
            reason = next((reason for name, reason in catalog.rejected if name == internal_name), "格式无效")
            raise ValueError(f"Skill 格式无效：{reason}")
        (directory / "metadata.json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        shutil.rmtree(directory)
        raise
    return record


def delete_user_skill(settings: Settings, user_id: uuid.UUID, skill_id: uuid.UUID) -> bool:
    directory = owner_root(settings, user_id) / f"personal-{skill_id.hex}"
    if not directory.is_dir() or _record(directory) is None:
        return False
    shutil.rmtree(directory)
    return True


def catalog_for_run(
    settings: Settings,
    user_id: uuid.UUID,
    system_catalog: skills.SkillCatalog,
    selected_ids: list[str],
    question: str = "",
) -> tuple[skills.SkillCatalog, list[str], dict[str, str]]:
    records = list_user_skills(settings, user_id)
    selected = set(selected_ids)
    user_catalog = skills.load_skill_catalog(owner_root(settings, user_id))
    by_name = {meta.name: meta for meta in user_catalog.skills}
    # 每种功能默认使用最近上传的一份；手选的 Skill 额外加入。
    defaults: dict[str, dict] = {}
    for record in records:
        if record["category"] != "other" and record["internal_name"] in by_name:
            defaults.setdefault(record["category"], record)
    visible = {record["internal_name"] for record in defaults.values()}
    visible.update(record["internal_name"] for record in records if record["id"] in selected)
    declared = {
        record["internal_name"]
        for record in records
        if record["category"] == "other"
        and record["name"] in question
        and any(verb in question for verb in ("使用", "用", "采用", "按"))
    }
    visible.update(declared)
    system_skills = [meta for meta in system_catalog.skills if meta.name not in defaults]
    personal = [by_name[name] for name in visible if name in by_name]
    merged = skills.SkillCatalog(
        root=system_catalog.root,
        skills=tuple([*system_skills, *personal]),
        rejected=(*system_catalog.rejected, *user_catalog.rejected),
    )
    selected_names = [
        record["internal_name"] for record in records
        if (record["id"] in selected or record["internal_name"] in declared)
        and record["internal_name"] in by_name
    ]
    default_names = {category: record["internal_name"] for category, record in defaults.items()}
    return merged, selected_names, default_names


def assert_owned_skill_ids(settings: Settings, user_id: uuid.UUID, ids: list[uuid.UUID]) -> None:
    owned = {item["id"] for item in list_user_skills(settings, user_id)}
    if any(str(item) not in owned for item in ids):
        raise ValueError("所选 Skill 不存在或不属于当前用户")
