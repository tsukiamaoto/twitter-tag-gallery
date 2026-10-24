"""Install the gallery Sidecar into the user's existing Antigravity desktop app."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import sys

from background_work import atomic_json


def install(project_id, *, config_root=None):
    import agent_jobs
    import database
    root = Path(__file__).resolve().parent
    config_root = Path(config_root) if config_root else Path.home() / ".gemini" / "config"
    project = config_root / "projects" / (project_id + ".json")
    if not project.is_file():
        raise ValueError("Antigravity 專案不存在，請選擇目前圖庫專案 ID")
    executable = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Antigravity" / "resources" / "bin" / "language_server.exe"
    if not executable.is_file():
        raise FileNotFoundError("找不到已安裝的 Antigravity 桌面版")
    config_path = config_root / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8-sig")) if config_path.exists() else {}
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if config_path.exists():
        shutil.copy2(config_path, config_path.with_name("config.json.before-gallery-sidecar-" + stamp + ".bak"))
    directory = config_root / "sidecars" / "gallery-publication"
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "sidecar.json"
    if manifest.exists():
        shutil.copy2(manifest, directory / ("sidecar.before-" + stamp + ".json"))
    agent_jobs.bridge_token(create=True)
    from antigravity_bridge import access_grants, mcp_server_name
    atomic_json(manifest, {
        "command": sys.executable,
        "args": [str(root / "antigravity_bridge.py")],
        "restart_policy": "on-failure", "display_name": "圖庫背景查證 · Gemini 3.8 Flash",
        "description": "領取圖庫來源與畫師查證工作，透過目前 Antigravity 桌面對話和圖庫 MCP 完成。",
        "agent_permissions": {"access_grants": access_grants(database.GALLERY_ROOT), "allows_user_interactions": False},
        "env": {"GALLERY_BRIDGE_URL": "http://127.0.0.1:8765", "GALLERY_BRIDGE_TOKEN_FILE": str(agent_jobs.token_path()),
                "GALLERY_ANTIGRAVITY_EXECUTABLE": str(executable), "PYTHONUTF8": "1",
                "GALLERY_ANTIGRAVITY_TRANSPORT": "native-v1",
                "GALLERY_ANTIGRAVITY_PROJECT_ID": project_id,
                "TWITTER_GALLERY_ROOT": str(database.GALLERY_ROOT)}}, durable=True)
    config.setdefault("sidecars", {})["gallery-publication"] = {"enabled": True, "projectId": project_id}
    atomic_json(config_path, config, durable=True)
    mcp_path = config_root / "mcp_config.json"
    mcp = json.loads(mcp_path.read_text(encoding="utf-8-sig")) if mcp_path.exists() else {}
    if mcp_path.exists():
        shutil.copy2(mcp_path, mcp_path.with_name("mcp_config.json.before-publication-" + stamp + ".bak"))
    servers = mcp.setdefault("mcpServers", {})
    server = servers.get("gallery")
    if server:
        server.setdefault("env", {})["GALLERY_AGENT_TOOLS_VERSION"] = "1"
    servers[mcp_server_name(database.GALLERY_ROOT)] = {"command": sys.executable,
        "args": [str(root / "mcp_server.py")],
        "env": {"TWITTER_GALLERY_ROOT": str(database.GALLERY_ROOT), "PYTHONUTF8": "1"}}
    atomic_json(mcp_path, mcp, durable=True)
    return {"installed": True, "project_id": project_id, "sidecar_path": str(manifest), "credentials_copied": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    options = parser.parse_args()
    print(json.dumps(install(options.project_id), ensure_ascii=False))
