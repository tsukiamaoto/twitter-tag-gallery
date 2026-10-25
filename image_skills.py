"""Image repair Agent Skills: SKILL.md owns instructions and model configuration.

The legacy JSON store becomes an index of IDs, active selection and archives.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import time
import uuid
import yaml

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal
from background_work import FileLock, atomic_json
from gguf_downloads import inside, model_root
from gguf_models import profile_for
import agent_skills

DEFAULT_INSTRUCTIONS = """# 影像修補

使用此技能指定的模型與修補 Prompt 處理使用者選定的圖片及遮罩。
保留遮罩外的構圖、線條與色彩，依技能的步數與隨機種子設定推論。
先預覽修補結果，確認套用時另存副本，保留原始圖片。
若指定模型尚未下載或載入失敗，回報錯誤並請使用者至 AI 模型頁處理。
"""


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
    instructions: str = Field(default=DEFAULT_INSTRUCTIONS, min_length=1, max_length=60000)
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("model_id")
    @classmethod
    def validate_model_path(cls, value):
        root = model_root()
        path = inside(root, value)
        if path.suffix.lower() != ".gguf" or (path.parent != root and not path.is_relative_to(root / "diffusion_models")):
            raise ValueError("請指定 GGUF 資料夾或 diffusion_models 內的影像模型")
        return path.relative_to(root).as_posix()


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError("影像修補技能缺少模型與 Prompt 設定")
    config = SkillInput.model_validate(value)
    inferred = profile_for(config.model_id)
    if inferred and inferred != config.profile:
        raise ValueError("模型檔名與技能選擇的模型系列不符")
    return config


def document_value(skill):
    config = skill["metadata"]["gallery"]["config"]
    value = validate_config(config).model_dump(exclude={"revision", "instructions"})
    return {"id": skill["id"], **value, "instructions": skill["body"],
            "revision": skill["revision"], "path": skill["path"], "kind": "image-repair"}


def document_content(skill_id, request, current=None, legacy_source=None):
    # Preserve caller-authored metadata and supporting instructions on form edits.
    metadata = dict((current or {}).get("metadata", {}))
    gallery = dict(metadata.get("gallery", {}))
    gallery.update(kind="image-repair", config=request.model_dump(exclude={"revision", "instructions"}))
    if legacy_source:
        gallery["legacy_source"] = legacy_source
    metadata["gallery"] = gallery
    frontmatter = dict((current or {}).get("frontmatter", {}))
    frontmatter.update(name=skill_id, description=request.description or request.name, metadata=metadata)
    return "---\n" + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False) + "---\n\n" + request.instructions.strip() + "\n"


def create_document(skill_id, content):
    agent_skills.parse(content, skill_id)
    with agent_skills.locked():
        path = agent_skills.skill_path(skill_id)
        if path.exists():
            raise RuntimeError(f"技能 {skill_id} 已存在，原檔未覆寫")
        path.parent.mkdir(parents=True, exist_ok=True)
        agent_skills.atomic_write(path, content.encode("utf-8"))


def migrate(data, path):
    if data["version"] == 2:
        return
    # Keep the original intact until every document has been committed. A retry
    # recognizes its own imports, including documents edited after interruption.
    backup = path.with_name("skills.before-agent-skills.json")
    if not backup.exists():
        atomic_json(backup, data, durable=True)
    source = str(path.resolve())
    ids = []
    for item in data["skills"]:
        skill_id = valid_id(item["id"])
        request = validate_config({k: v for k, v in item.items() if k not in ("id", "updated_at")})
        target = agent_skills.skill_path(skill_id)
        if target.exists():
            current = agent_skills.require(skill_id)
            if current["metadata"].get("gallery", {}).get("legacy_source") != source:
                raise RuntimeError(f"技能 {skill_id} 已存在，無法匯入；原檔未覆寫")
        else:
            create_document(skill_id, document_content(skill_id, request, legacy_source=source))
        ids.append(skill_id)
    migrated = {**data, "version": 2, "skills": ids}
    atomic_json(path, migrated, durable=True)
    data.clear()
    data.update(migrated)


def skills_root():
    return Path(os.environ.get("TWITTER_IMAGE_SKILLS_ROOT", Path(__file__).parent / "image_skills")).resolve()


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
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
                if data.get("version") not in (1, 2) or not isinstance(data.get("skills"), list):
                    raise ValueError()
            except (ValueError, AttributeError):
                raise ValueError("技能檔案格式錯誤，請先檢查 image_skills/skills.json；原檔未被覆寫")
        else:
            templates = json.loads(Path(__file__).with_name("image_skill_templates.json").read_text(encoding="utf-8"))
            data = {"version": 1, "active_id": None, "skills": templates, "archived": []}
            atomic_json(path, data, durable=True)
        migrate(data, path)
        yield data, path
    finally:
        lock.release()


def status():
    with locked_store() as (data, _):
        # Discover documents too, so an externally installed image Agent Skill
        # does not require a second registration in the old JSON index.
        values = [document_value(agent_skills.require(item["id"])) for item in agent_skills.catalog()["skills"]
                  if item.get("kind") == "image-repair"]
        return {"directory": str(agent_skills.skills_root()), "skills": values, "active_id": data.get("active_id")}


def save(request, skill_id=None):
    validate_config(request.model_dump(exclude={"revision", "instructions"}))
    with locked_store() as (data, path):
        if skill_id:
            valid_id(skill_id)
            current = agent_skills.require(skill_id)
            if current.get("kind") != "image-repair":
                raise KeyError("找不到技能")
            if "instructions" not in request.model_fields_set:
                request = request.model_copy(update={"instructions": current["body"]})
            content = document_content(skill_id, request, current)
            saved = agent_skills.save(skill_id, content, request.revision or current["revision"])
        else:
            if sum(item.get("kind") == "image-repair" for item in agent_skills.catalog()["skills"]) >= 200:
                raise ValueError("最多可保留 200 份技能")
            skill_id = uuid.uuid4().hex
            create_document(skill_id, document_content(skill_id, request))
            saved = agent_skills.require(skill_id)
        # The index only tracks legacy membership. SKILL.md is authoritative,
        # including for new documents; no second commit can lose a saved edit.
        return document_value(saved)


def find(skill_id):
    valid_id(skill_id)
    with locked_store():
        skill = agent_skills.require(skill_id)
        if skill.get("kind") != "image-repair":
            raise KeyError("找不到技能")
        return document_value(skill)


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
        if not agent_skills.skill_path(skill_id).is_file():
            raise KeyError("找不到技能")
        data["active_id"] = skill_id
        atomic_json(path, data, durable=True)
    return prepared


def archive(skill_id):
    valid_id(skill_id)
    with locked_store() as (data, path):
        skill = agent_skills.require(skill_id)
        if skill.get("kind") != "image-repair":
            raise KeyError("找不到技能")
        data.setdefault("archived", []).append({**document_value(skill), "archived_at": time.time()})
        data["skills"] = [item for item in data["skills"] if item != skill_id]
        if data.get("active_id") == skill_id:
            data["active_id"] = None
        with agent_skills.locked():
            source = agent_skills.skill_path(skill_id)
            archive_path = agent_skills.skills_root() / ".storage" / "archived" / skill_id / (skill["revision"] + ".md")
            if not archive_path.resolve().is_relative_to(agent_skills.skills_root()):
                raise ValueError("技能封存路徑超出允許目錄")
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, archive_path)
            try:
                atomic_json(path, data, durable=True)
            except Exception:
                os.replace(archive_path, source)
                raise

