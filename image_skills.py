"""Project-local image-editing prompt presets, separate from executable Codex skills."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import time
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal
from background_work import FileLock, atomic_json
from gguf_downloads import inside, model_root
from gguf_models import profile_for


class SkillInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    profile: Literal["wai_illustrious", "klein4b", "qwen21"] = "wai_illustrious"
    model_id: str = Field(min_length=1, max_length=500)
    prompt: str = Field(min_length=1, max_length=12000)
    steps: int = Field(ge=1, le=100)
    seed: int = Field(default=42, ge=-1, le=2147483647)
    output_mode: Literal["copy"] = "copy"

    @field_validator("model_id")
    @classmethod
    def validate_model_path(cls, value):
        root = model_root()
        path = inside(root, value)
        if path.suffix.lower() != ".gguf" or (path.parent != root and not path.is_relative_to(root / "diffusion_models")):
            raise ValueError("請指定 GGUF 資料夾或 diffusion_models 內的影像模型")
        return path.relative_to(root).as_posix()


def skills_root():
    return Path(os.environ.get("TWITTER_IMAGE_SKILLS_ROOT", Path(__file__).parent / "image_skills")).resolve()


def valid_id(value):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", value):
        raise ValueError("技能編號無效")
    return value


@contextmanager
def locked_store():
    root = skills_root()
    root.mkdir(parents=True, exist_ok=True)
    lock = FileLock(root / ".skills.lock")
    if not lock.acquire():
        raise RuntimeError("另一個操作正在儲存技能，請稍後重試")
    try:
        path = root / "skills.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("version") != 1 or not isinstance(data.get("skills"), list):
                    raise ValueError()
            except (ValueError, AttributeError):
                raise ValueError("技能檔案格式錯誤，請先檢查 image_skills/skills.json；原檔未被覆寫")
        else:
            templates = json.loads(Path(__file__).with_name("image_skill_templates.json").read_text(encoding="utf-8"))
            data = {"version": 1, "active_id": None, "skills": templates, "archived": []}
            atomic_json(path, data, durable=True)
        yield data, path
    finally:
        lock.release()


def status():
    with locked_store() as (data, _):
        return {"directory": str(skills_root()), "skills": data["skills"], "active_id": data.get("active_id")}


def save(request, skill_id=None):
    value = request.model_dump()
    inferred = profile_for(value["model_id"])
    if inferred and inferred != value["profile"]:
        raise ValueError("模型檔名與技能選擇的模型系列不符")
    with locked_store() as (data, path):
        if skill_id:
            valid_id(skill_id)
            if not any(item["id"] == skill_id for item in data["skills"]):
                raise KeyError("找不到技能")
        else:
            if len(data["skills"]) >= 200:
                raise ValueError("最多可保留 200 份技能")
            skill_id = uuid.uuid4().hex
        skill = {"id": skill_id, **value, "updated_at": time.time()}
        data["skills"] = [item for item in data["skills"] if item["id"] != skill_id] + [skill]
        atomic_json(path, data, durable=True)
        return skill


def find(skill_id):
    valid_id(skill_id)
    with locked_store() as (data, _):
        for item in data["skills"]:
            if item["id"] == skill_id:
                return {"id": item["id"], **SkillInput.model_validate({k: v for k, v in item.items() if k not in ("id", "updated_at")}).model_dump()}
    raise KeyError("找不到技能")


def prepare(skill_id):
    skill = find(skill_id)
    return {"skill_id": skill["id"], "name": skill["name"], "model_id": skill["model_id"],
            "profile": skill["profile"], "prompt": skill["prompt"], "seed": skill["seed"], "output_mode": "copy",
            "sample_params": {"sample_method": "euler", "sample_steps": skill["steps"],
                              "guidance": {"txt_cfg": 1.0, "img_cfg": 1.0, "distilled_guidance": 1.0}}}


def activate(skill_id, select_model):
    prepared = prepare(skill_id)
    select_model(prepared["model_id"], prepared["profile"])
    with locked_store() as (data, path):
        if not any(item["id"] == skill_id for item in data["skills"]):
            raise KeyError("找不到技能")
        data["active_id"] = skill_id
        atomic_json(path, data, durable=True)
    return prepared


def archive(skill_id):
    valid_id(skill_id)
    with locked_store() as (data, path):
        skill = next((item for item in data["skills"] if item["id"] == skill_id), None)
        if skill is None:
            raise KeyError("找不到技能")
        data.setdefault("archived", []).append({**skill, "archived_at": time.time()})
        data["skills"] = [item for item in data["skills"] if item["id"] != skill_id]
        if data.get("active_id") == skill_id:
            data["active_id"] = None
        atomic_json(path, data, durable=True)

