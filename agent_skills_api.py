"""Local Agent Skill document editor; no tool registration or reverse links."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import agent_skills
import agent_workflows
import image_skills

router = APIRouter(prefix="/api/agent-skills", tags=["Agent skills"])


class SkillUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str = Field(min_length=1, max_length=agent_skills.MAX_BYTES)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


def call(function, *arguments):
    try:
        return function(*arguments)
    except FileNotFoundError as error:
        raise HTTPException(404, "找不到技能檔案或目錄") from error
    except (ValueError, UnicodeError) as error:
        raise HTTPException(422, str(error)) from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(409, str(error)) from error


@router.get("")
def catalog():
    migration_error = ""
    try:
        agent_workflows.migrate()
    except (OSError, ValueError, RuntimeError) as error:
        migration_error = f"舊流程遷移未完成：{error}"
    try:
        image_skills.status()
    except (OSError, ValueError, RuntimeError) as error:
        migration_error += ("；" if migration_error else "") + f"影像技能遷移未完成：{error}"
    return {**call(agent_skills.catalog), "migration_error": migration_error}


@router.get("/{skill_id}")
def read(skill_id: str):
    return call(agent_skills.read, skill_id)


@router.put("/{skill_id}")
def save(skill_id: str, request: SkillUpdate):
    return call(agent_skills.save, skill_id, request.content, request.revision)
