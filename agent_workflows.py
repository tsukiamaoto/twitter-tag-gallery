"""Task-owned skill selection, legacy migration and immutable prompt snapshots."""
import json
from pathlib import Path

import agent_skills
import database
import prompt_store
from background_work import FileLock, atomic_json

WORKFLOWS = {"publication": "gallery-publication-tracing", "artist_identity": "gallery-artist-identity"}
LEGACY_SKILLS = {**WORKFLOWS, "deduplication": "gallery-deduplication"}


def settings_path():
    return Path(database.DATA_DIR) / "agent_workflows.json"


def settings():
    if not settings_path().exists():
        return {"version": 1, "skills": {}}
    value = json.loads(settings_path().read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("skills"), dict):
        raise ValueError("任務技能設定格式錯誤，原檔未覆寫")
    return value


def migrate():
    """Commit imported documents and task bindings before retiring legacy tasks."""
    if not prompt_store.path().exists():
        return settings()
    lock = FileLock(prompt_store.path().with_suffix(".lock"))
    if not lock.acquire():
        raise RuntimeError("工具說明正在儲存，請稍後重試流程遷移")
    try:
        old = prompt_store.load()
        config = settings()
        tasks = old.get("tasks", {})
        if not isinstance(tasks, dict):
            raise ValueError("舊任務流程設定格式錯誤，原檔未覆寫")
        if tasks:
            fingerprint = agent_skills.revision(json.dumps(tasks, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            if config.get("migrated_tasks") != fingerprint:
                unknown = set(tasks) - LEGACY_SKILLS.keys()
                if unknown:
                    raise ValueError("存在無法遷移的舊任務流程：" + ", ".join(sorted(unknown)))
                backup = Path(database.DATA_DIR) / "backups" / ("tool_prompts_before_skills_" + fingerprint + ".json")
                if not backup.exists():
                    atomic_json(backup, old, durable=True)
                imported = {}
                for workflow, body in tasks.items():
                    imported[workflow] = agent_skills.import_legacy(LEGACY_SKILLS[workflow], body)
                config = {**config, "migrated_tasks": fingerprint,
                          "imported_skills": imported,
                          "skills": {**config["skills"], **{key: value for key, value in imported.items() if key in WORKFLOWS}}}
                atomic_json(settings_path(), config, durable=True)
        if "tasks" in old:
            del old["tasks"]
            old["revision"] += 1
            atomic_json(prompt_store.path(), old, durable=True)
        return config
    finally:
        lock.release()


def snapshot(workflow, context):
    if workflow not in WORKFLOWS:
        raise ValueError("未知的查證工作")
    config = migrate()
    skill_id = config["skills"].get(workflow, WORKFLOWS[workflow])
    try:
        skill = agent_skills.require(skill_id)
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError(f"無法建立任務，請檢查技能 {skill_id}：{error}") from error
    from antigravity_bridge import mcp_server_name
    prompt = skill["body"]
    rendered = ("本工作唯一圖庫 MCP ServerName=" + mcp_server_name(database.GALLERY_ROOT)
                + "；所有 gallery_* 工具均透過此 ServerName 呼叫，禁止使用其他同名圖庫服務。\n\n"
                + prompt + "\n\n工作資料（JSON）：\n" + json.dumps(context, ensure_ascii=False))
    return {"workflow": workflow, "skill_id": skill_id, "revision": skill["revision"],
            "prompt": prompt, "rendered_prompt": rendered}
