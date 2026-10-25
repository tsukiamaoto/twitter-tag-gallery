"""Project SKILL.md documents, independent of tools and gallery workflows."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import uuid

import yaml

from background_work import FileLock

MAX_BYTES = 128 * 1024


def skills_root():
    return Path(os.environ.get("TWITTER_AGENT_SKILLS_ROOT", Path(__file__).parent / ".agents" / "skills")).resolve()


def skill_path(skill_id):
    if not isinstance(skill_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", skill_id):
        raise ValueError("技能 ID 無效")
    root = skills_root()
    path = root / skill_id / "SKILL.md"
    if not path.resolve().is_relative_to(root):
        raise ValueError("技能路徑超出允許目錄")
    return path


def revision(content):
    return hashlib.sha256(content).hexdigest()


def parse(content, skill_id):
    if len(content.encode("utf-8")) > MAX_BYTES:
        raise ValueError("技能內容超過 128 KiB")
    match = re.match(r"\A---\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)(.*)\Z", content, re.S)
    if not match:
        raise ValueError("技能必須包含 YAML frontmatter 與 Markdown 正文")
    try:
        metadata = yaml.safe_load(match[1])
    except yaml.YAMLError as error:
        raise ValueError("技能 YAML frontmatter 格式錯誤") from error
    if not isinstance(metadata, dict) or metadata.get("name") != skill_id:
        raise ValueError("技能 name 必須與資料夾 ID 相同；此介面不提供重新命名")
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("技能 description 不可空白")
    body = match[2].strip()
    if not body:
        raise ValueError("技能 Markdown 正文不可空白")
    extension = metadata.get("metadata", {})
    if not isinstance(extension, dict):
        raise ValueError("技能 metadata 必須為物件")
    gallery = extension.get("gallery", {})
    if not isinstance(gallery, dict):
        raise ValueError("技能 metadata.gallery 必須為物件")
    kind = gallery.get("kind", "agent")
    if kind == "image-repair":
        from image_skills import validate_config
        validate_config(gallery.get("config"))
    return {"name": metadata["name"], "description": description.strip(), "body": body,
            "metadata": extension, "kind": kind, "frontmatter": metadata}


def read(skill_id):
    path = skill_path(skill_id)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("技能內容超過 128 KiB")
    raw = path.read_bytes()
    content = raw.decode("utf-8-sig")
    result = {"id": skill_id, "name": skill_id, "description": "", "path": str(path),
              "content": content, "revision": revision(raw), "valid": True, "error": ""}
    try:
        result.update(parse(content, skill_id))
    except ValueError as error:
        result.update(valid=False, error=str(error))
    return result


def require(skill_id):
    value = read(skill_id)
    if not value["valid"]:
        raise ValueError(f"技能 {skill_id}：{value['error']}")
    return value


def catalog():
    root = skills_root()
    values = []
    for folder in sorted(root.iterdir()):
        if folder.name.startswith(".") or not folder.is_dir() or not (folder / "SKILL.md").exists():
            continue
        try:
            item = read(folder.name)
            item.pop("content")
            item.pop("body", None)
            item.pop("frontmatter", None)
        except (OSError, UnicodeError, ValueError) as error:
            item = {"id": folder.name, "name": folder.name, "description": "", "valid": False,
                    "error": f"無法讀取技能：{error}", "revision": None}
        values.append(item)
    return {"directory": str(root), "skills": values}


@contextmanager
def locked():
    root = skills_root()
    if not root.is_dir():
        raise FileNotFoundError("找不到 Agent Skill 目錄")
    storage = root / ".storage"
    if not storage.resolve().is_relative_to(root):
        raise ValueError("技能儲存路徑超出允許目錄")
    lock_path = storage / "skills.lock"
    if not lock_path.resolve().is_relative_to(root):
        raise ValueError("技能鎖定路徑超出允許目錄")
    lock = FileLock(lock_path)
    if not lock.acquire():
        raise RuntimeError("其他操作正在儲存技能，請稍後重試")
    try:
        yield
    finally:
        lock.release()


def atomic_write(path, content):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save(skill_id, content, expected_revision):
    parse(content, skill_id)
    with locked():
        path = skill_path(skill_id)
        current = read(skill_id)
        if current["revision"] != expected_revision:
            raise RuntimeError("技能已被其他視窗或編輯器更新，請重新載入後再儲存")
        raw = path.read_bytes()
        if revision(raw) != expected_revision:
            raise RuntimeError("技能已被其他編輯器更新，請重新載入後再儲存")
        backup = skills_root() / ".storage" / "backups" / skill_id / (current["revision"] + ".md")
        if not backup.resolve().is_relative_to(skills_root()):
            raise ValueError("技能備份路徑超出允許目錄")
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            atomic_write(backup, raw)
        if revision(path.read_bytes()) != expected_revision:
            raise RuntimeError("技能已被其他編輯器更新，請重新載入後再儲存")
        atomic_write(path, content.encode("utf-8"))
        return read(skill_id)


def import_legacy(base_id, content):
    if not isinstance(content, str) or not content.strip():
        raise ValueError("舊任務流程不可空白")
    skill_id = base_id + "-imported-" + revision(content.encode("utf-8"))[:12]
    document = f"---\nname: {skill_id}\ndescription: 從舊版設定匯入的 {base_id} 自訂流程。\n---\n\n" + content
    parse(document, skill_id)
    with locked():
        path = skill_path(skill_id)
        if path.exists():
            if path.read_bytes().decode("utf-8") != document:
                raise RuntimeError(f"匯入技能 {skill_id} 已有不同內容，原檔未覆寫")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, document.encode("utf-8"))
    return skill_id
