"""个人 Skill 的上传、列表和删除接口。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import SettingsDep
from app.core.errors import ResourceNotFoundError, UploadInvalidError
from app.modules.agent import skills, user_skills
from app.modules.auth.permissions import CurrentUserDep

router = APIRouter(prefix="/users/me/skills", tags=["agent-skills"])


class SkillUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    content: str = Field(min_length=1)


@router.get("")
def list_skills(user: CurrentUserDep, settings: SettingsDep) -> dict:
    system = skills.load_skill_catalog()
    return {
        "items": user_skills.list_user_skills(settings, user.id),
        "system_items": [
            {"name": item.name, "description": item.description}
            for item in system.skills
        ],
        "categories": user_skills.CATEGORIES,
    }


@router.post("", status_code=201)
def upload_skill(payload: SkillUpload, user: CurrentUserDep, settings: SettingsDep) -> dict:
    try:
        return user_skills.create_user_skill(settings, user.id, payload.content, payload.category)
    except ValueError as exc:
        raise UploadInvalidError(str(exc)) from exc


@router.delete("/{skill_id}", status_code=204)
def remove_skill(skill_id: uuid.UUID, user: CurrentUserDep, settings: SettingsDep) -> None:
    if not user_skills.delete_user_skill(settings, user.id, skill_id):
        raise ResourceNotFoundError("Skill 不存在")
