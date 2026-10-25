"""Versioned tool descriptions. Tools never load or modify agent skills."""
import json
from pathlib import Path
import time

from background_work import FileLock, atomic_json


def path():
    import database
    return Path(database.DATA_DIR) / "tool_prompts.json"


def load():
    if not path().exists():
        return {"revision": 0, "tools": {}}
    value = json.loads(path().read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or type(value.get("revision")) is not int
            or not isinstance(value.get("tools"), dict)):
        raise ValueError("工具說明設定格式錯誤，原檔未覆寫")
    return value


def instruction(tool):
    value = load()
    return value.get("tools", {}).get(tool.name, tool.description)

def details(tool):
    value = load()
    return {"usage_prompt": value.get("tools", {}).get(tool.name, tool.description),
            "default_usage_prompt": tool.description, "prompt_revision": value["revision"],
            "customized": tool.name in value.get("tools", {})}


def save(name, usage_prompt, revision, reset=False):
    from tool_registry import get_tool
    tool = get_tool(name)
    lock = FileLock(path().with_suffix(".lock"))
    if not lock.acquire():
        raise RuntimeError("其他視窗正在儲存工具說明")
    try:
        value = load()
        if value["revision"] != revision:
            raise RuntimeError("工具說明已被其他視窗更新，請重新載入後再編輯")
        if reset:
            value["tools"].pop(name, None)
        else:
            if not isinstance(usage_prompt, str) or not usage_prompt.strip() or len(usage_prompt) > 16000:
                raise ValueError("工具說明需為 1–16000 字")
            value["tools"][name] = usage_prompt.strip()
        # Preserve unmigrated data without interpreting workflows in the tool layer.
        value.update(revision=value["revision"] + 1, updated_at=time.time())
        atomic_json(path(), value, durable=True)
    finally:
        lock.release()
    return details(tool)
