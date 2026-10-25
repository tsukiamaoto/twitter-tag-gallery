"""Local MCP policy, multi-server card registry, and client configuration; model login belongs to the client."""
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

from background_work import atomic_json, FileLock
from tool_registry import TOOLS

DEFAULTS = {"mcp_enabled": True, "mcp_read_only": False, "disabled_tools": []}
CLIENT_SERVER_NAME = "gallery"  # FQN mcp_<server>_<tool> must fit model tool-name limits.
SIDECAR_SERVER_ID = "gallerypub"
BUILTIN_IDS = (CLIENT_SERVER_NAME, SIDECAR_SERVER_ID)
SIDECAR_TOOLS = (
    "gallery_get_overview",
    "gallery_get_agent_job",
    "gallery_get_image_details",
    "gallery_get_tool_run",
    "gallery_trace_publication_dates",
    "gallery_submit_publication_sources",
    "gallery_complete_agent_batch",
    "gallery_submit_artist_identity",
)
SERVER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TRANSPORTS = ("stdio", "http", "sse")


def config_path():
    from database import DATA_DIR
    return Path(DATA_DIR) / "integrations.json"


def antigravity_config_root(config_root=None):
    return Path(config_root) if config_root else Path.home() / ".gemini" / "config"


def antigravity_mcp_config_path(config_root=None):
    return antigravity_config_root(config_root) / "mcp_config.json"


def _validate_string_dict(value, field_name, max_items=100, max_key_len=200, max_val_len=4000):
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > max_items:
        raise ValueError(f"{field_name} 必須為鍵值對物件（最多 {max_items} 項）")
    cleaned = {}
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, (str, int, float, bool)):
            raise ValueError(f"{field_name} 的鍵與值必須為字串或基本型別")
        key = k.strip()
        val = str(v)
        if not key or len(key) > max_key_len or len(val) > max_val_len:
            raise ValueError(f"{field_name} 鍵值長度超出限制")
        cleaned[key] = val
    return cleaned


def _validate_custom_config(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("custom_config 必須為 JSON 物件")
    serialized = json.dumps(value, ensure_ascii=False)
    if len(serialized.encode("utf-8")) > 32768:
        raise ValueError("custom_config 超過 32 KiB 上限")
    return value


def _sanitize_custom_server(raw):
    if not isinstance(raw, dict):
        raise ValueError("Invalid custom MCP server")
    server_id = str(raw.get("id") or "").strip()
    if not SERVER_ID_PATTERN.fullmatch(server_id) or server_id in BUILTIN_IDS:
        raise ValueError("Invalid custom MCP server id")
    transport = raw.get("transport", "stdio")
    if transport not in TRANSPORTS:
        raise ValueError("Invalid custom MCP transport")
    enabled = raw.get("enabled", True)
    read_only = raw.get("read_only", False)
    if not isinstance(enabled, bool) or not isinstance(read_only, bool):
        raise ValueError("Invalid custom MCP flags")
    disabled_tools = raw.get("disabled_tools", [])
    if not isinstance(disabled_tools, list) or any(not isinstance(item, str) for item in disabled_tools):
        raise ValueError("Invalid custom MCP disabled_tools")
    args = raw.get("args", [])
    if not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        raise ValueError("Invalid custom MCP args")
    discovered_tools = raw.get("discovered_tools", [])
    if not isinstance(discovered_tools, list):
        discovered_tools = []
    return {
        "id": server_id,
        "server_name": str(raw.get("server_name") or server_id).strip() or server_id,
        "name": str(raw.get("name") or server_id).strip()[:100] or server_id,
        "category": str(raw.get("category") or "外部／自訂 MCP 串連").strip()[:100],
        "description": str(raw.get("description") or "").strip()[:1000],
        "transport": transport,
        "enabled": enabled,
        "read_only": read_only,
        "disabled_tools": sorted(set(disabled_tools))[:200],
        "command": str(raw.get("command") or "").strip()[:1000],
        "args": [str(item) for item in args[:100]],
        "cwd": str(raw.get("cwd") or "").strip()[:1000],
        "env": _validate_string_dict(raw.get("env") or {}, "env"),
        "url": str(raw.get("url") or "").strip()[:2000],
        "headers": _validate_string_dict(raw.get("headers") or {}, "headers"),
        "custom_config": _validate_custom_config(raw.get("custom_config") or {}),
        "discovered_tools": [
            {"name": str(t.get("name", "")), "label": str(t.get("label") or t.get("name") or ""), "writes": bool(t.get("writes", False))}
            for t in discovered_tools if isinstance(t, dict) and t.get("name")
        ][:200],
        "last_test": raw.get("last_test") if isinstance(raw.get("last_test"), dict) else None,
        "updated_at": float(raw.get("updated_at") or 0),
    }


def _load_store():
    path = config_path()
    if not path.exists():
        return {**DEFAULTS, "disabled_tools": [], "builtin_overrides": {}, "custom_servers": [], "managed_client_keys": []}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or any(not isinstance(value.get(key, True), bool) for key in ("mcp_enabled", "mcp_read_only")):
            raise ValueError("Invalid MCP policy")
        if not isinstance(value.get("disabled_tools", []), list) or any(not isinstance(name, str) for name in value.get("disabled_tools", [])):
            raise ValueError("Invalid MCP tools")
        raw_overrides = value.get("builtin_overrides", {})
        if not isinstance(raw_overrides, dict):
            raise ValueError("Invalid builtin_overrides")
        overrides = {}
        for bid in BUILTIN_IDS:
            item = raw_overrides.get(bid)
            if item is None:
                continue
            if not isinstance(item, dict):
                raise ValueError("Invalid builtin override")
            entry = {}
            if "name" in item and isinstance(item["name"], str) and item["name"].strip():
                entry["name"] = item["name"].strip()[:100]
            if "category" in item and isinstance(item["category"], str) and item["category"].strip():
                entry["category"] = item["category"].strip()[:100]
            if "description" in item and isinstance(item["description"], str):
                entry["description"] = item["description"].strip()[:1000]
            if "enabled" in item:
                if not isinstance(item["enabled"], bool):
                    raise ValueError("Invalid builtin enabled")
                entry["enabled"] = item["enabled"]
            if "read_only" in item:
                if not isinstance(item["read_only"], bool):
                    raise ValueError("Invalid builtin read_only")
                entry["read_only"] = item["read_only"]
            if "disabled_tools" in item:
                if not isinstance(item["disabled_tools"], list) or any(not isinstance(t, str) for t in item["disabled_tools"]):
                    raise ValueError("Invalid builtin disabled_tools")
                entry["disabled_tools"] = sorted({t for t in item["disabled_tools"] if t in TOOLS})
            if "custom_config" in item:
                entry["custom_config"] = _validate_custom_config(item["custom_config"])
            if "last_test" in item and isinstance(item["last_test"], dict):
                entry["last_test"] = item["last_test"]
            overrides[bid] = entry

        raw_custom = value.get("custom_servers", [])
        if not isinstance(raw_custom, list):
            raise ValueError("Invalid custom_servers")
        custom_servers = []
        seen_ids = set(BUILTIN_IDS)
        for item in raw_custom:
            sanitized = _sanitize_custom_server(item)
            if sanitized["id"] in seen_ids:
                raise ValueError("Duplicate MCP server id")
            seen_ids.add(sanitized["id"])
            custom_servers.append(sanitized)

        managed_keys = value.get("managed_client_keys", [])
        if not isinstance(managed_keys, list) or any(not isinstance(k, str) for k in managed_keys):
            managed_keys = []

        return {
            "mcp_enabled": value.get("mcp_enabled", DEFAULTS["mcp_enabled"]),
            "mcp_read_only": value.get("mcp_read_only", DEFAULTS["mcp_read_only"]),
            "disabled_tools": sorted({name for name in value.get("disabled_tools", []) if name in TOOLS}),
            "builtin_overrides": overrides,
            "custom_servers": custom_servers,
            "managed_client_keys": [k for k in managed_keys if k],
        }
    except (OSError, ValueError):
        # Malformed policy must never enable writes accidentally.
        return {
            **DEFAULTS,
            "mcp_enabled": False,
            "disabled_tools": [],
            "builtin_overrides": {},
            "custom_servers": [],
            "managed_client_keys": [],
            "config_error": "設定檔無法讀取，請重新儲存",
        }


def _persist_store(store):
    payload = {
        "mcp_enabled": bool(store.get("mcp_enabled", True)),
        "mcp_read_only": bool(store.get("mcp_read_only", False)),
        "disabled_tools": sorted(set(store.get("disabled_tools", []))),
    }
    if store.get("builtin_overrides"):
        payload["builtin_overrides"] = store["builtin_overrides"]
    if store.get("custom_servers"):
        payload["custom_servers"] = store["custom_servers"]
    if store.get("managed_client_keys"):
        payload["managed_client_keys"] = sorted(set(store["managed_client_keys"]))
    atomic_json(config_path(), payload, durable=True)


def load():
    store = _load_store()
    result = {key: store.get(key, default) for key, default in DEFAULTS.items()}
    if "config_error" in store:
        result["config_error"] = store["config_error"]
    return result


def _sidecar_server_name():
    import database
    try:
        from antigravity_bridge import mcp_server_name
        return mcp_server_name(database.GALLERY_ROOT)
    except Exception:
        return "gallerypub"


def _sidecar_bridge_status():
    try:
        import agent_jobs
        return agent_jobs.bridge_status()
    except Exception:
        return {"connected": False, "model_id": "", "model_verified": False, "error": "", "updated_at": 0}


def list_servers(store=None):
    import database
    store = store or _load_store()
    overrides = store.get("builtin_overrides", {})
    gallery_override = overrides.get(CLIENT_SERVER_NAME, {})
    gallery_tools = [
        {"name": name, "label": label, "writes": writes, "enabled": allowed(name, store)}
        for name, (label, writes) in TOOLS.items()
    ]
    live_sessions = sessions()
    gallery_card = {
        "id": CLIENT_SERVER_NAME,
        "server_name": CLIENT_SERVER_NAME,
        "builtin": True,
        "name": gallery_override.get("name") or "Antigravity 圖庫 MCP",
        "category": gallery_override.get("category") or "核心圖庫工具 · Antigravity",
        "description": gallery_override.get("description") or "由 Antigravity 呼叫圖庫工具（搜尋、標籤校正、歸檔與復原）。登入與模型選擇在客戶端管理。",
        "transport": "stdio",
        "enabled": bool(store["mcp_enabled"]),
        "read_only": bool(store["mcp_read_only"]),
        "disabled_tools": list(store["disabled_tools"]),
        "command": sys.executable,
        "args": [str(Path(__file__).with_name("mcp_server.py"))],
        "cwd": str(Path(__file__).parent),
        "env": {"TWITTER_GALLERY_ROOT": str(database.GALLERY_ROOT)},
        "url": "",
        "headers": {},
        "custom_config": {"client": "Antigravity", **gallery_override.get("custom_config", {})},
        "sessions": live_sessions,
        "last_test": gallery_override.get("last_test"),
        "tool_count": len(gallery_tools),
        "enabled_tool_count": sum(1 for item in gallery_tools if item["enabled"]),
        "tools": gallery_tools,
    }

    pub_override = overrides.get(SIDECAR_SERVER_ID, {})
    pub_enabled = bool(pub_override.get("enabled", True))
    pub_read_only = bool(pub_override.get("read_only", False))
    pub_disabled = [name for name in pub_override.get("disabled_tools", []) if name in TOOLS]
    pub_tools = []
    for name in SIDECAR_TOOLS:
        if name not in TOOLS:
            continue
        label, writes = TOOLS[name]
        tool_enabled = bool(pub_enabled and name not in pub_disabled and not (pub_read_only and writes))
        pub_tools.append({"name": name, "label": label, "writes": writes, "enabled": tool_enabled})
    bridge = _sidecar_bridge_status()
    pub_card = {
        "id": SIDECAR_SERVER_ID,
        "server_name": _sidecar_server_name(),
        "builtin": True,
        "name": pub_override.get("name") or "Antigravity 背景查證 MCP",
        "category": pub_override.get("category") or "桌面背景 Sidecar · Gemini 3.8 Flash",
        "description": pub_override.get("description") or "搭配桌面版 gallery-publication Sidecar，在背景啟動專用對話（固定驗證 Gemini 3.8 Flash），自動執行原始發布日期追查與畫師跨平台身分查證。",
        "transport": "stdio",
        "enabled": pub_enabled,
        "read_only": pub_read_only,
        "disabled_tools": pub_disabled,
        "command": sys.executable,
        "args": [str(Path(__file__).with_name("mcp_server.py"))],
        "cwd": str(Path(__file__).parent),
        "env": {"TWITTER_GALLERY_ROOT": str(database.GALLERY_ROOT), "PYTHONUTF8": "1"},
        "url": "",
        "headers": {},
        "custom_config": {
            "sidecar_id": "gallery-publication",
            "required_model": "gemini-3.8-flash",
            "project_id": "",
            **pub_override.get("custom_config", {}),
        },
        "bridge": bridge,
        "last_test": pub_override.get("last_test"),
        "tool_count": len(pub_tools),
        "enabled_tool_count": sum(1 for item in pub_tools if item["enabled"]),
        "tools": pub_tools,
    }

    cards = [gallery_card, pub_card]
    for custom in store.get("custom_servers", []):
        discovered = custom.get("discovered_tools") or []
        disabled_set = set(custom.get("disabled_tools") or [])
        tool_items = []
        seen_names = set()
        for t in discovered:
            tname = t["name"]
            seen_names.add(tname)
            twrites = bool(t.get("writes", False))
            tenabled = bool(custom["enabled"] and tname not in disabled_set and not (custom["read_only"] and twrites))
            tool_items.append({"name": tname, "label": t.get("label") or tname, "writes": twrites, "enabled": tenabled})
        for tname in custom.get("disabled_tools") or []:
            if tname not in seen_names:
                tool_items.append({"name": tname, "label": tname, "writes": False, "enabled": False})
        cards.append({
            **custom,
            "builtin": False,
            "tool_count": len(tool_items),
            "enabled_tool_count": sum(1 for item in tool_items if item["enabled"]),
            "tools": tool_items,
        })
    return cards


def get_server(server_id, store=None):
    for server in list_servers(store):
        if server["id"] == server_id or server["server_name"] == server_id:
            return server
    raise KeyError(f"找不到 MCP 項目：{server_id}")


def public_config():
    store = _load_store()
    result = {key: store.get(key) for key in DEFAULTS}
    result["config_error"] = store.get("config_error", "")
    result["tools"] = [
        {"name": name, "label": label, "writes": writes, "enabled": allowed(name, store)}
        for name, (label, writes) in TOOLS.items()
    ]
    result["transport"] = "stdio"
    result["servers"] = list_servers(store)
    return result


def save(values):
    if set(values) - set(DEFAULTS):
        raise ValueError("僅支援 MCP 設定；模型登入請在 Antigravity 管理")
    lock = FileLock(config_path().with_suffix(".lock"))
    if not lock.acquire():
        raise ValueError("其他設定正在儲存，請稍後重試")
    try:
        store = _load_store()
        for name in ("mcp_enabled", "mcp_read_only"):
            if name in values:
                if not isinstance(values[name], bool):
                    raise ValueError(f"{name} 必須為布林值")
                store[name] = values[name]
        if "disabled_tools" in values:
            tools = values["disabled_tools"]
            if not isinstance(tools, list) or any(name not in TOOLS for name in tools):
                raise ValueError("包含未知的 MCP 工具")
            store["disabled_tools"] = sorted(set(tools))
        store.pop("config_error", None)
        _persist_store(store)
    finally:
        lock.release()
    return public_config()


def save_server(server_id, values, *, create_only=False, sync_client=False, config_root=None):
    if not isinstance(values, dict):
        raise ValueError("MCP 設定必須為 JSON 物件")
    target_id = str(server_id or values.get("id") or values.get("server_id") or "").strip()
    if not SERVER_ID_PATTERN.fullmatch(target_id):
        raise ValueError("MCP ID 需為 1–64 字元的英數字、底線或連字號")

    lock = FileLock(config_path().with_suffix(".lock"))
    if not lock.acquire():
        raise ValueError("其他設定正在儲存，請稍後重試")
    try:
        store = _load_store()
        if target_id == _sidecar_server_name():
            target_id = SIDECAR_SERVER_ID

        if target_id in BUILTIN_IDS:
            if create_only:
                raise ValueError(f"{target_id} 為內建 MCP 項目，請直接修改既有卡片")
            overrides = dict(store.get("builtin_overrides", {}))
            current = dict(overrides.get(target_id, {}))
            if "name" in values and values["name"] is not None:
                name = str(values["name"]).strip()
                if not name or len(name) > 100:
                    raise ValueError("MCP 名稱需為 1–100 字")
                current["name"] = name
            if "category" in values and values["category"] is not None:
                current["category"] = str(values["category"]).strip()[:100]
            if "description" in values and values["description"] is not None:
                current["description"] = str(values["description"]).strip()[:1000]
            if "custom_config" in values and values["custom_config"] is not None:
                current["custom_config"] = _validate_custom_config(values["custom_config"])

            if target_id == CLIENT_SERVER_NAME:
                if "enabled" in values and values["enabled"] is not None:
                    if not isinstance(values["enabled"], bool):
                        raise ValueError("enabled 必須為布林值")
                    store["mcp_enabled"] = values["enabled"]
                if "read_only" in values and values["read_only"] is not None:
                    if not isinstance(values["read_only"], bool):
                        raise ValueError("read_only 必須為布林值")
                    store["mcp_read_only"] = values["read_only"]
                if "disabled_tools" in values and values["disabled_tools"] is not None:
                    tools = values["disabled_tools"]
                    if not isinstance(tools, list) or any(name not in TOOLS for name in tools):
                        raise ValueError("包含未知的 MCP 工具")
                    store["disabled_tools"] = sorted(set(tools))
            else:
                if "enabled" in values and values["enabled"] is not None:
                    if not isinstance(values["enabled"], bool):
                        raise ValueError("enabled 必須為布林值")
                    current["enabled"] = values["enabled"]
                if "read_only" in values and values["read_only"] is not None:
                    if not isinstance(values["read_only"], bool):
                        raise ValueError("read_only 必須為布林值")
                    current["read_only"] = values["read_only"]
                if "disabled_tools" in values and values["disabled_tools"] is not None:
                    tools = values["disabled_tools"]
                    if not isinstance(tools, list) or any(name not in TOOLS for name in tools):
                        raise ValueError("包含未知的 MCP 工具")
                    current["disabled_tools"] = sorted(set(tools))

            overrides[target_id] = current
            store["builtin_overrides"] = overrides
        else:
            custom_list = list(store.get("custom_servers", []))
            existing_index = next((idx for idx, item in enumerate(custom_list) if item["id"] == target_id), None)
            if create_only and existing_index is not None:
                raise ValueError(f"MCP ID '{target_id}' 已存在，請更換 ID 或直接編輯該卡片")
            existing = dict(custom_list[existing_index]) if existing_index is not None else {
                "id": target_id,
                "server_name": target_id,
                "name": target_id,
                "category": "外部／自訂 MCP 串連",
                "description": "",
                "transport": "stdio",
                "enabled": True,
                "read_only": False,
                "disabled_tools": [],
                "command": "",
                "args": [],
                "cwd": "",
                "env": {},
                "url": "",
                "headers": {},
                "custom_config": {},
                "discovered_tools": [],
                "last_test": None,
            }
            if "name" in values and values["name"] is not None:
                name = str(values["name"]).strip()
                if not name or len(name) > 100:
                    raise ValueError("MCP 名稱需為 1–100 字")
                existing["name"] = name
            elif existing_index is None:
                raise ValueError("新增 MCP 卡片時必須填寫名稱")

            if "category" in values and values["category"] is not None:
                existing["category"] = str(values["category"]).strip()[:100] or "外部／自訂 MCP 串連"
            if "description" in values and values["description"] is not None:
                existing["description"] = str(values["description"]).strip()[:1000]
            if "transport" in values and values["transport"] is not None:
                if values["transport"] not in TRANSPORTS:
                    raise ValueError("不支援的傳輸協定（僅支援 stdio、http、sse）")
                existing["transport"] = values["transport"]
            if "enabled" in values and values["enabled"] is not None:
                if not isinstance(values["enabled"], bool):
                    raise ValueError("enabled 必須為布林值")
                existing["enabled"] = values["enabled"]
            if "read_only" in values and values["read_only"] is not None:
                if not isinstance(values["read_only"], bool):
                    raise ValueError("read_only 必須為布林值")
                existing["read_only"] = values["read_only"]
            if "disabled_tools" in values and values["disabled_tools"] is not None:
                tools = values["disabled_tools"]
                if not isinstance(tools, list) or any(not isinstance(t, str) or not t.strip() for t in tools):
                    raise ValueError("disabled_tools 必須為工具名稱字串陣列")
                existing["disabled_tools"] = sorted({t.strip() for t in tools})[:200]
            if "command" in values and values["command"] is not None:
                existing["command"] = str(values["command"]).strip()[:1000]
            if "args" in values and values["args"] is not None:
                if not isinstance(values["args"], list) or any(not isinstance(a, str) for a in values["args"]):
                    raise ValueError("args 必須為字串陣列")
                existing["args"] = [str(a) for a in values["args"][:100]]
            if "cwd" in values and values["cwd"] is not None:
                existing["cwd"] = str(values["cwd"]).strip()[:1000]
            if "env" in values and values["env"] is not None:
                existing["env"] = _validate_string_dict(values["env"], "env")
            if "url" in values and values["url"] is not None:
                existing["url"] = str(values["url"]).strip()[:2000]
            if "headers" in values and values["headers"] is not None:
                existing["headers"] = _validate_string_dict(values["headers"], "headers")
            if "custom_config" in values and values["custom_config"] is not None:
                existing["custom_config"] = _validate_custom_config(values["custom_config"])

            if existing["transport"] == "stdio":
                if not existing["command"]:
                    raise ValueError("stdio 類型的 MCP 必須填寫啟動指令（command）")
            else:
                if not existing["url"].startswith(("http://", "https://")):
                    raise ValueError("HTTP / SSE 類型的 MCP 必須填寫 http:// 或 https:// 開頭的連線網址（url）")

            existing["updated_at"] = time.time()
            sanitized = _sanitize_custom_server(existing)
            if existing_index is None:
                custom_list.append(sanitized)
            else:
                custom_list[existing_index] = sanitized
            store["custom_servers"] = custom_list

        store.pop("config_error", None)
        _persist_store(store)
    finally:
        lock.release()

    synced_info = sync_client_config(config_root=config_root) if sync_client else None
    updated_store = _load_store()
    server = get_server(target_id, updated_store)
    return {
        "server": server,
        "servers": list_servers(updated_store),
        "synced": synced_info,
        "message": f"已儲存「{server['name']}」設定" + ("並同步至 Antigravity。" if synced_info else "。"),
    }


def delete_server(server_id, *, sync_client=False, config_root=None):
    target_id = str(server_id or "").strip()
    if target_id in BUILTIN_IDS or target_id == _sidecar_server_name():
        raise ValueError("內建 MCP 項目無法刪除，請改用停用開關")
    lock = FileLock(config_path().with_suffix(".lock"))
    if not lock.acquire():
        raise ValueError("其他設定正在儲存，請稍後重試")
    try:
        store = _load_store()
        custom_list = list(store.get("custom_servers", []))
        removed = next((item for item in custom_list if item["id"] == target_id), None)
        if removed is None:
            raise KeyError(f"找不到可刪除的自訂 MCP：{target_id}")
        store["custom_servers"] = [item for item in custom_list if item["id"] != target_id]
        _persist_store(store)
    finally:
        lock.release()

    synced_info = sync_client_config(config_root=config_root) if sync_client else None
    updated_store = _load_store()
    return {
        "deleted_id": target_id,
        "servers": list_servers(updated_store),
        "synced": synced_info,
        "message": f"已刪除「{removed['name']}」" + ("並同步更新 Antigravity 設定。" if synced_info else "。"),
    }


def allowed(name, config=None):
    config = config or load()
    return bool(config["mcp_enabled"] and name in TOOLS and name not in config["disabled_tools"] and not (config["mcp_read_only"] and TOOLS[name][1]))


def require_tool(name):
    if not allowed(name):
        raise PermissionError("此 MCP 工具已停用或受唯讀模式限制")


def _export_entry(server):
    if server["id"] == CLIENT_SERVER_NAME:
        entry = {
            "command": server["command"],
            "args": server["args"],
            "cwd": server["cwd"],
            "env": server["env"],
            "disabled": not server["enabled"],
            "disabledTools": [name for name in TOOLS if not allowed(name)],
        }
        return CLIENT_SERVER_NAME, entry

    if server["id"] == SIDECAR_SERVER_ID:
        disabled_tools = [t["name"] for t in server["tools"] if not t["enabled"]]
        entry = {
            "command": server["command"],
            "args": server["args"],
            "cwd": server["cwd"],
            "env": server["env"],
            "disabled": not server["enabled"],
        }
        if disabled_tools:
            entry["disabledTools"] = disabled_tools
        return server["server_name"], entry

    key = server["server_name"] or server["id"]
    if server["transport"] == "stdio":
        entry = {
            "command": server["command"],
            "args": list(server.get("args") or []),
            "disabled": not server["enabled"],
            "disabledTools": list(server.get("disabled_tools") or []),
        }
        if server.get("cwd"):
            entry["cwd"] = server["cwd"]
        if server.get("env"):
            entry["env"] = dict(server["env"])
    else:
        entry = {
            "transport": server["transport"],
            "url": server["url"],
            "disabled": not server["enabled"],
            "disabledTools": list(server.get("disabled_tools") or []),
        }
        if server.get("headers"):
            entry["headers"] = dict(server["headers"])
    if server.get("custom_config"):
        entry["customConfig"] = dict(server["custom_config"])
    return key, entry


def client_config(client="antigravity", *, server_id=None, include_all=False):
    if client != "antigravity":
        raise ValueError("不支援的 MCP 客戶端")
    store = _load_store()
    if server_id:
        server = get_server(server_id, store)
        key, entry = _export_entry(server)
        return {"mcpServers": {key: entry}}
    if include_all:
        servers = {}
        for item in list_servers(store):
            key, entry = _export_entry(item)
            servers[key] = entry
        return {"mcpServers": servers}
    # Default export preserves the canonical 'gallery' key and any custom servers with short names.
    gallery_server = get_server(CLIENT_SERVER_NAME, store)
    key, entry = _export_entry(gallery_server)
    return {"mcpServers": {key: entry}}


def sync_client_config(config_root=None):
    lock = FileLock(config_path().with_suffix(".lock"))
    if not lock.acquire():
        raise ValueError("其他設定正在儲存，請稍後重試")
    try:
        store = _load_store()
        mcp_path = antigravity_mcp_config_path(config_root)
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        current_mcp = {}
        if mcp_path.exists():
            try:
                current_mcp = json.loads(mcp_path.read_text(encoding="utf-8-sig"))
                if not isinstance(current_mcp, dict):
                    current_mcp = {}
            except (OSError, ValueError):
                current_mcp = {}
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(mcp_path, mcp_path.with_name(f"mcp_config.json.before-mcp-sync-{stamp}.bak"))

        mcp_servers = current_mcp.setdefault("mcpServers", {})
        if not isinstance(mcp_servers, dict):
            mcp_servers = {}
            current_mcp["mcpServers"] = mcp_servers

        previous_managed = set(store.get("managed_client_keys", []))
        current_managed = set()
        for server in list_servers(store):
            key, entry = _export_entry(server)
            existing_entry = mcp_servers.get(key)
            if isinstance(existing_entry, dict) and key == CLIENT_SERVER_NAME:
                merged_env = dict(existing_entry.get("env") or {})
                merged_env.update(entry.get("env") or {})
                entry["env"] = merged_env
            mcp_servers[key] = entry
            current_managed.add(key)

        for obsolete_key in previous_managed - current_managed:
            if obsolete_key not in (CLIENT_SERVER_NAME, _sidecar_server_name()):
                mcp_servers.pop(obsolete_key, None)

        store["managed_client_keys"] = sorted(current_managed)
        _persist_store(store)
        atomic_json(mcp_path, current_mcp, durable=True)
        return {
            "ok": True,
            "path": str(mcp_path),
            "server_count": len(current_managed),
            "servers": sorted(current_managed),
            "message": f"已同步 {len(current_managed)} 個 MCP 設定至 Antigravity（mcp_config.json）。",
        }
    finally:
        lock.release()


def record_session(action, tool=None):
    from database import DATA_DIR
    directory = Path(DATA_DIR) / "mcp_sessions"
    atomic_json(directory / f"{os.getpid()}.json", {"pid": os.getpid(), "last_seen": time.time(), "action": action, "tool": tool})


def sessions():
    from database import DATA_DIR
    result = []
    for path in (Path(DATA_DIR) / "mcp_sessions").glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - value["last_seen"] < 86400:
                result.append(value)
        except (OSError, ValueError, KeyError):
            continue
    return sorted(result, key=lambda item: item["last_seen"], reverse=True)[:20]


def _stdio_handshake(command, args, cwd=None, extra_env=None, label="本機 MCP"):
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "gallery-settings-diagnostic", "version": "1.0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    env = os.environ.copy()
    env.update(PYTHONIOENCODING="utf-8", TWITTER_GALLERY_MCP_DIAGNOSTIC="1")
    if extra_env:
        for k, v in extra_env.items():
            env[str(k)] = str(v)
    run_cwd = cwd if cwd and Path(cwd).is_dir() else None
    try:
        result = subprocess.run(
            [command, *args],
            input="".join(json.dumps(req) + "\n" for req in requests),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            env=env,
            cwd=run_cwd,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        responses = []
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("{"):
                try:
                    responses.append(json.loads(stripped))
                except ValueError:
                    continue
        tool_response = next((response for response in responses if response.get("id") == 2), {})
        tools = tool_response.get("result", {}).get("tools")
        if tools is None:
            detail = (result.stderr or result.stdout or "").strip()[:240]
            return {
                "ok": False,
                "message": f"{label} 啟動或握手失敗，請檢查指令與環境" + (f"（{detail}）" if detail else ""),
                "exit_code": result.returncode,
            }
        tool_names = [tool["name"] for tool in tools if isinstance(tool, dict) and tool.get("name")]
        discovered = [
            {
                "name": tool["name"],
                "label": tool.get("title") or tool.get("name"),
                "writes": not bool(tool.get("annotations", {}).get("readOnlyHint", True)),
            }
            for tool in tools if isinstance(tool, dict) and tool.get("name")
        ]
        return {
            "ok": True,
            "message": f"{label} 握手成功，可用工具 {len(tool_names)} 個",
            "tool_count": len(tool_names),
            "tools": tool_names,
            "discovered_tools": discovered,
        }
    except FileNotFoundError:
        return {"ok": False, "message": f"{label} 找不到執行檔：{command}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"{label} 連線診斷逾時（15 秒）"}
    except OSError as exc:
        return {"ok": False, "message": f"{label} 啟動失敗：{exc}"}


def _http_handshake(url, headers=None, transport="http", label="遠端 MCP"):
    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "gallery-settings-diagnostic", "version": "1.0"}},
    }
    req_headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if headers:
        req_headers.update(headers)
    try:
        request = urllib.request.Request(url, data=json.dumps(init_req).encode("utf-8"), headers=req_headers, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(65536).decode("utf-8", errors="replace")
            status_code = response.status
        parsed = None
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("data:"):
                stripped = stripped[5:].strip()
            if stripped.startswith("{"):
                try:
                    parsed = json.loads(stripped)
                    break
                except ValueError:
                    continue
        if isinstance(parsed, dict) and ("result" in parsed or "jsonrpc" in parsed):
            return {
                "ok": True,
                "message": f"{label}（{transport.upper()}）握手成功（HTTP {status_code}）",
                "tool_count": 0,
                "tools": [],
                "discovered_tools": [],
            }
        return {
            "ok": True,
            "message": f"{label} 端點連線成功（HTTP {status_code}）",
            "tool_count": 0,
            "tools": [],
            "discovered_tools": [],
        }
    except urllib.error.HTTPError as exc:
        if transport == "sse" and exc.code in (400, 405):
            try:
                get_headers = {"Accept": "text/event-stream"}
                if headers:
                    get_headers.update(headers)
                req_get = urllib.request.Request(url, headers=get_headers, method="GET")
                with urllib.request.urlopen(req_get, timeout=8) as resp:
                    return {
                        "ok": True,
                        "message": f"{label}（SSE）端點連線成功（HTTP {resp.status}）",
                        "tool_count": 0,
                        "tools": [],
                        "discovered_tools": [],
                    }
            except Exception as inner_exc:
                return {"ok": False, "message": f"{label} SSE 連線失敗：{inner_exc}"}
        return {"ok": False, "message": f"{label} 回應 HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "message": f"{label} 連線失敗：{exc}"}


def _record_test_result(server_id, result):
    lock = FileLock(config_path().with_suffix(".lock"))
    if not lock.acquire():
        return
    try:
        store = _load_store()
        summary = {
            "ok": bool(result.get("ok")),
            "message": str(result.get("message") or ""),
            "tool_count": int(result.get("tool_count") or 0),
            "tested_at": time.time(),
        }
        if server_id in BUILTIN_IDS:
            overrides = dict(store.get("builtin_overrides", {}))
            current = dict(overrides.get(server_id, {}))
            current["last_test"] = summary
            overrides[server_id] = current
            store["builtin_overrides"] = overrides
        else:
            custom_list = list(store.get("custom_servers", []))
            for item in custom_list:
                if item["id"] == server_id:
                    item["last_test"] = summary
                    if result.get("discovered_tools"):
                        item["discovered_tools"] = result["discovered_tools"][:200]
                    break
            store["custom_servers"] = custom_list
        _persist_store(store)
    finally:
        lock.release()


def diagnose_mcp():
    import database
    result = _stdio_handshake(
        sys.executable,
        ["-B", str(Path(__file__).with_name("mcp_server.py"))],
        cwd=str(Path(__file__).parent),
        extra_env={"TWITTER_GALLERY_ROOT": str(database.GALLERY_ROOT)},
        label="本機 MCP",
    )
    result.pop("discovered_tools", None)
    _record_test_result(CLIENT_SERVER_NAME, result)
    return result


def diagnose_server(server_id):
    store = _load_store()
    server = get_server(server_id, store)
    target_id = server["id"]
    if target_id == CLIENT_SERVER_NAME:
        return diagnose_mcp()
    if target_id == SIDECAR_SERVER_ID:
        import database
        result = _stdio_handshake(
            sys.executable,
            ["-B", str(Path(__file__).with_name("mcp_server.py"))],
            cwd=str(Path(__file__).parent),
            extra_env={"TWITTER_GALLERY_ROOT": str(database.GALLERY_ROOT)},
            label="背景查證 MCP",
        )
        result.pop("discovered_tools", None)
        if result["ok"]:
            bridge = _sidecar_bridge_status()
            bridge_note = (
                "Sidecar 已連線並驗證 Gemini 3.8 Flash"
                if bridge.get("connected") and bridge.get("model_verified")
                else "Sidecar 已連線（模型待驗證）"
                if bridge.get("connected")
                else "桌面 Sidecar 目前待命"
            )
            result["tool_count"] = server["enabled_tool_count"]
            result["tools"] = [t["name"] for t in server["tools"] if t["enabled"]]
            result["message"] = f"背景查證 MCP 握手成功（啟用 {server['enabled_tool_count']} 個查證工具 · {bridge_note}）"
        _record_test_result(SIDECAR_SERVER_ID, result)
        return result

    if server["transport"] == "stdio":
        result = _stdio_handshake(
            server["command"],
            server.get("args") or [],
            cwd=server.get("cwd") or None,
            extra_env=server.get("env") or {},
            label=server["name"],
        )
    else:
        result = _http_handshake(
            server["url"],
            headers=server.get("headers") or {},
            transport=server["transport"],
            label=server["name"],
        )
    _record_test_result(target_id, result)
    result.pop("discovered_tools", None)
    return result
