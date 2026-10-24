"""Official desktop Sidecar: claim gallery work and launch dedicated conversations.

Run inside Antigravity's Sidecar environment, not as an unrelated shell command.
No gallery database writes occur in this process. Model results return through MCP.
"""
import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("GALLERY_BRIDGE_URL", "http://127.0.0.1:8765")


class DesktopRequestRejected(RuntimeError):
    pass


def api(payload=None, path="/api/agent/bridge"):
    token_file = Path(os.environ["GALLERY_BRIDGE_TOKEN_FILE"])
    token = json.loads(token_file.read_text(encoding="utf-8"))["token"]
    request = urllib.request.Request(BASE + path, data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", "X-Gallery-Bridge-Token": token})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def command(*args):
    executable = os.environ.get("GALLERY_ANTIGRAVITY_EXECUTABLE", "")
    if not executable or not Path(executable).is_file():
        raise RuntimeError("找不到 Antigravity 桌面版 language_server.exe")
    argv = [executable, "agentapi", *args]
    if len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 > 30000:
        raise ValueError("Prompt 超過桌面啟動入口長度限制")
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Antigravity agentapi 呼叫失敗")[:1000])
    try:
        payload = json.loads(result.stdout)
        if isinstance(payload, dict) and isinstance(payload.get("response"), dict):
            if payload.get("error"):
                raise RuntimeError(str(payload["error"])[:1000])
            return payload["response"]
        return payload
    except ValueError:
        # Some desktop builds print just the new conversation UUID.
        match = re.fullmatch(r"\s*([a-f0-9-]{36})\s*", result.stdout)
        if match:
            return {"conversation_id": match[1]}
        raise RuntimeError("桌面入口未回傳可辨識的 JSON 或對話 ID") from None


def conversation_id(value):
    if isinstance(value, dict):
        for key in ("conversation_id", "conversationId", "cascade_id", "cascadeId", "id"):
            if isinstance(value.get(key), str) and re.fullmatch(r"[a-f0-9-]{36}", value[key]):
                return value[key]
        for item in value.values():
            found = conversation_id(item)
            if found:
                return found
    return None


def launch_arguments(title, prompt):
    profile = os.environ.get("GALLERY_ANTIGRAVITY_PROFILE", "")
    model = os.environ.get("GALLERY_ANTIGRAVITY_MODEL", "flash")
    selection = "--profile=" + profile if profile else "--model=" + model
    return ("new-conversation", selection, "--title=" + title, prompt)


def catalog_models(value, include_all=False):
    """Bind an opaque desktop enum only to the label returned by its own catalog."""
    result = {}
    if isinstance(value, dict):
        labels = [value.get(k, "") for k in ("label", "displayName", "display_name", "modelName", "name")]
        is_flash38 = any(isinstance(label, str) and re.search(r"\bGemini[\s_-]*3[._]8[\s_-]*Flash\b", label, re.I) for label in labels)
        if is_flash38 or include_all and any(isinstance(label, str) and label for label in labels):
            display = next((label for label in labels if isinstance(label, str) and label), "")
            def identifiers(item):
                if isinstance(item, dict):
                    for key, child in item.items():
                        if key in ("model", "modelId", "model_id", "modelEnum", "modelOrAlias", "model_or_alias", "id"):
                            if isinstance(child, str):
                                result[child] = display if include_all else "gemini-3.8-flash"
                            elif isinstance(child, dict):
                                identifiers(child)
            identifiers(value)
        for child in value.values():
            if isinstance(child, (dict, list)):
                result.update(catalog_models(child, include_all))
    elif isinstance(value, list):
        for child in value:
            result.update(catalog_models(child, include_all))
    return result


def desktop_rpc(method, payload):
    # Sidecars receive this authenticated loopback context from the desktop app.
    # This is a read-only, version-dependent adapter; failure keeps the queue gated.
    from urllib.parse import urlparse
    address = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "")
    token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
    if not address or not token:
        raise RuntimeError("桌面未提供模型目錄查詢環境")
    if not address.startswith(("http://", "https://")):
        address = "http://" + address
    parsed = urlparse(address)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("桌面模型目錄必須為本機服務")
    request = urllib.request.Request(address.rstrip("/") + "/exa.language_server_pb.LanguageServerService/" + method,
        data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "x-codeium-csrf-token": token})
    try:
        with urllib.request.urlopen(request, timeout=8, context=ssl._create_unverified_context()) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(1500).decode("utf-8", errors="replace")
        if exc.code in (400, 404, 422):
            raise DesktopRequestRejected(f"桌面 {method} 拒絕請求 ({exc.code}): {detail}") from None
        raise RuntimeError(f"桌面 {method} 失敗 ({exc.code})") from None


def desktop_catalog():
    payload = desktop_rpc("GetCascadeModelConfigData", {})
    def redact(value):
        if isinstance(value, dict):
            return {k: redact(v) for k, v in value.items() if not any(s in k.lower() for s in ("secret", "token", "credential", "apikey", "api_key", "email"))}
        if isinstance(value, list):
            return [redact(v) for v in value]
        return value
    candidates = [c for c in payload.get("clientModelConfigs", [])
                  if re.fullmatch(r"gemini-3\.8-flash-(medium|high|low)", c.get("modelId", ""))]
    candidates.sort(key=lambda c: ("medium" not in c["modelId"], "high" not in c["modelId"]))
    selected = candidates[0].get("modelOrAlias", {}).get("model", "") if candidates else ""
    return {"verified": catalog_models(payload), "labels": catalog_models(payload, include_all=True),
            "diagnostic": redact(payload), "selected": selected}

def native_start(model):
    """Versioned desktop adapter: select the exact model offered by this app.

    agentapi's tier alias can lag the catalogue. The app's own local RPC accepts
    its model enum, and the created conversation is checked before sending work.
    """
    config = native_config(model)
    result = desktop_rpc("StartCascade", {
        "source": "CORTEX_TRAJECTORY_SOURCE_AGENT_API",
        "sourceMetadata": {"sidecar": {"sidecarId": "gallery-publication", "config": {
            "command": os.environ.get("GALLERY_ANTIGRAVITY_EXECUTABLE", ""),
            "agentPermissions": {"accessGrants": access_grants(), "allowsUserInteractions": False}}}},
        "requestedModel": model,
        "workspaceUris": [Path(__file__).resolve().parent.as_uri()],
        "projectEnvConfig": {"projectId": os.environ["GALLERY_ANTIGRAVITY_PROJECT_ID"]},
        "customAgentSpec": {"codingAgent": {"googleMode": True}, "cascadeConfig": config,
                "mcpServers": [{"serverName": mcp_server_name(), "command": sys.executable,
                "args": [str(Path(__file__).resolve().parent / "mcp_server.py")],
                "env": {"TWITTER_GALLERY_ROOT": os.environ["TWITTER_GALLERY_ROOT"], "PYTHONUTF8": "1"}}]},
        "tags": ["gallery-publication"],
    })
    cid = conversation_id(result)
    if not cid:
        raise RuntimeError("桌面原生入口未回傳對話 ID")
    return cid


def mcp_server_name(root=None):
    resolved = Path(root or os.environ.get("TWITTER_GALLERY_ROOT", r"D:\圖檔\twitter")).resolve().as_posix().lower()
    return "gallerypub_" + hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:8]


def access_grants(root=None):
    tools = ("gallery_get_overview", "gallery_get_agent_job", "gallery_get_image_details",
             "gallery_get_tool_run", "gallery_trace_publication_dates", "gallery_submit_publication_sources",
             "gallery_complete_agent_batch", "gallery_submit_artist_identity")
    return ["mcp(" + mcp_server_name(root) + "/" + name + ")" for name in tools] + [
        "read_file(" + str(root or os.environ.get("TWITTER_GALLERY_ROOT", r"D:\圖檔\twitter")) + ")",
        *["read_url(" + host + ")" for host in ("x.com", "twitter.com", "pixiv.net", "fanbox.cc", "danbooru.donmai.us")]]


def native_send(cid, prompt, model):
    return desktop_rpc("SendUserCascadeMessage", {"cascadeId": cid, "items": [{"text": prompt}],
        "cascadeConfig": native_config(model)})


def native_config(model):
    # Only these job tools are authorized for unattended work. Preserve explicit
    # project/global Ask and Deny rules; do not grant terminal or file writes.
    grants = {"allow": access_grants(), "deny": ["command(*)", "write_file(*)"], "ask": []}
    root = Path.home() / ".gemini" / "config"
    sources = [(root / "config.json", "userSettings")]
    project = os.environ.get("GALLERY_ANTIGRAVITY_PROJECT_ID", "")
    if project:
        sources.append((root / "projects" / (project + ".json"), "settings"))
    for path, field in sources:
        if not path.exists():
            continue
        settings = json.loads(path.read_text(encoding="utf-8-sig")).get(field, {})
        for key in ("globalPermissionGrants", "permissionGrants", "permission_grants"):
            configured = settings.get(key, {})
            if isinstance(configured, dict):
                for category in grants:
                    grants[category].extend(configured.get(category, []))
    return {"plannerConfig": {"planModel": model, "requestedModel": {"model": model},
            "toolConfig": {"permissionConfig": {"effectiveGrants": grants, "permissionsV2": True}}},
            "applyModelDefaultOverride": False}


def actual_model(metadata, catalog=None):
    """Only model metadata counts. Never trust prompts, titles or a model's answer."""
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if re.fullmatch(r"(?:(?:actual_?|resolved_?|effective_?)?model(?:_?id|_?name)?|planModel)", key, re.I) and isinstance(value, str):
                if catalog and value in catalog:
                    return catalog[value]
                normalized = re.sub(r"[^a-z0-9]", "", value.lower())
                if normalized.startswith("gemini38flash"):
                    return "gemini-3.8-flash"
            if key.lower() not in ("prompt", "messages", "steps", "title", "response", "content", "requestedmodel", "availablemodels", "clientmodelconfigs", "modelcatalog") and isinstance(value, (dict, list)):
                found = actual_model(value, catalog)
                if found:
                    return found
    elif isinstance(metadata, list):
        for value in metadata:
            found = actual_model(value, catalog)
            if found:
                return found
    return ""


def probe_summary(execution, catalog, server_name=None):
    steps = execution.get("trajectory", {}).get("steps", [])
    models = [s.get("metadata", {}).get("modelUsage", {}).get("model") for s in steps]
    models = [model for model in models if model]
    runtime_verified = bool(models) and all(catalog.get(model) == "gemini-3.8-flash" for model in models)
    mcp_ready = any(s.get("type") == "CORTEX_STEP_TYPE_MCP_TOOL" and s.get("status") == "CORTEX_STEP_STATUS_DONE"
        and s.get("mcpTool", {}).get("serverName") == (server_name or mcp_server_name())
        and s.get("mcpTool", {}).get("toolCall", {}).get("name") == "gallery_get_overview" for s in steps)
    return {"status": execution.get("status", ""), "models": sorted(set(models)),
            "runtime_verified": runtime_verified, "mcp_ready": mcp_ready,
            "errors": [s["error"].get("shortError", "")[:500] for s in steps if s.get("error")],
            "tools": [{"type": s.get("type"), "status": s.get("status"),
                "calls": [c.get("name") for c in s.get("plannerResponse", {}).get("toolCalls", [])]}
                for s in steps if s.get("type") != "CORTEX_STEP_TYPE_USER_INPUT"],
            "last_response": next((s.get("plannerResponse", {}).get("response", "")[:1000] for s in reversed(steps)
                                   if s.get("plannerResponse", {}).get("response")), "")}


def main():
    runtime = Path(os.environ.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR", Path(__file__).parent / ".tag_gallery" / "bridge-runtime"))
    runtime.mkdir(parents=True, exist_ok=True)
    state_path = runtime / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    def save():
        from background_work import atomic_json
        atomic_json(state_path, state, durable=True)
    while True:
        try:
            catalog_error = ""
            selected = ""
            try:
                reply = desktop_catalog()
                catalog = reply["verified"]
                selected = reply["selected"]
                state["model_catalog"] = catalog
                state["model_labels"] = reply["labels"]
                from background_work import atomic_json
                atomic_json(runtime / "model-catalog.json", reply["diagnostic"])
            except Exception as exc:
                catalog, catalog_error = {}, str(exc)
            state["model_catalog_error"] = catalog_error
            save()
            native = os.environ.get("GALLERY_ANTIGRAVITY_TRANSPORT") == "native-v1"
            profile = "native-v1-registered:" + selected if native else os.environ.get("GALLERY_ANTIGRAVITY_PROFILE", "") + ":" + os.environ.get("GALLERY_ANTIGRAVITY_MODEL", "flash")
            if state.get("probe_profile", "") != profile:
                state.pop("probe_id", None)
                state.pop("probe_launching", None)
                state["probe_profile"] = profile
                save()
            # A harmless, read-only probe establishes the desktop's actual Flash model.
            if not state.get("probe_id"):
                if native and not selected:
                    raise RuntimeError("桌面模型目錄未提供 Gemini 3.8 Flash，工作保留待命。" + catalog_error)
                if state.get("probe_launching"):
                    raise RuntimeError("模型檢查對話啟動結果不明，請先核對 Sidecar events，不會自動重建對話")
                state["probe_launching"] = True
                save()
                try:
                    state["probe_id"] = native_start(selected) if native else conversation_id(command(*launch_arguments("圖庫 Flash 模型連線檢查", "這是圖庫背景連線檢查。只回覆 READY；不要呼叫工具、修改檔案或執行任務。")))
                except DesktopRequestRejected:
                    state.pop("probe_launching", None)
                    save()
                    raise
                if not state["probe_id"]:
                    raise RuntimeError("無法取得模型檢查對話 ID")
                state.pop("probe_launching", None)
                save()
            metadata = command("get-conversation-metadata", state["probe_id"])
            state["metadata_keys"] = list(metadata) if isinstance(metadata, dict) else str(type(metadata))
            model = actual_model(metadata, catalog)
            save()
            if native and model and state.get("probe_sent_id") != state["probe_id"]:
                state["probe_sent_id"] = state["probe_id"]
                save()
                native_send(state["probe_id"], "這是圖庫 MCP 唯讀連線測試。唯一圖庫 MCP ServerName=" + mcp_server_name() + "。只透過此 ServerName 呼叫一次 gallery_get_overview，讀取圖庫總數後回覆 READY。不要呼叫其他服務、寫入工具、修改檔案、執行命令或啟動其他工作。", selected)
            probe = state.get("probe_summary", {}) if state.get("summary_probe_id") == state["probe_id"] else {}
            if native and model and not (probe.get("mcp_ready") and probe.get("runtime_verified")):
                diagnostic = desktop_rpc("GetCascadeTrajectory", {"cascadeId": state["probe_id"]})
                probe = probe_summary(diagnostic, catalog)
                state["probe_summary"] = probe
                state["summary_probe_id"] = state["probe_id"]
                from background_work import atomic_json
                atomic_json(runtime / "probe-result.json", probe)
                save()
            if native and model and not (probe.get("mcp_ready") and probe.get("runtime_verified")):
                model = ""
                catalog_error = "實際模型或圖庫 MCP 唯讀呼叫尚未驗證成功，請查看 Antigravity 連線檢查對話。"
            error = "" if model else "桌面 Flash 模型版本尚無法確認為 Gemini 3.8 Flash；任務保留待命。" + catalog_error
            api({"action": "heartbeat", "model_id": model, "error": error})
            if model:
                job = api({"action": "claim"}).get("job")
                if job:
                    try:
                        state["launching_job"] = job["id"]
                        save()
                        cid = native_start(selected) if native else conversation_id(command(*launch_arguments("圖庫查證 " + job["id"][:10], job["prompt"]["rendered_prompt"])))
                        if not cid:
                            raise RuntimeError("啟動結果缺少對話 ID，請先在 Antigravity 核對")
                        state.setdefault("pending_launches", {})[job["id"]] = {"id": cid, "phase": "created"}
                        save()
                        state.pop("launching_job", None)
                        save()
                    except Exception as exc:
                        api({"action": "launch_failed", "job_id": job["id"], "error": str(exc)[:1000]})
            for job_id, pending in list(state.get("pending_launches", {}).items()):
                cid = pending["id"] if isinstance(pending, dict) else pending
                job = api(path="/api/agent/jobs/" + job_id)
                if job["status"] not in ("launching", "launch_uncertain", "running"):
                    state["pending_launches"].pop(job_id, None)
                    save()
                    continue
                if isinstance(pending, dict) and pending.get("phase") == "dispatching":
                    api({"action": "interrupted", "job_id": job_id, "error": "桌面啟動訊息結果不明；請檢查對話後續跑，避免重複送出。"})
                    state["pending_launches"].pop(job_id, None)
                    save()
                    continue
                if actual_model(command("get-conversation-metadata", cid), catalog) != "gemini-3.8-flash":
                    continue
                recovered = api({"action": "launched", "job_id": job_id, "conversation_id": cid})
                if native and recovered.get("status") == "running":
                    state["pending_launches"][job_id] = {"id": cid, "phase": "dispatching"}
                    save()
                    try:
                        native_send(cid, recovered["prompt"]["rendered_prompt"], selected)
                    except Exception as exc:
                        api({"action": "interrupted", "job_id": job_id, "error": "桌面訊息傳送失敗或結果不明：" + str(exc)[:700]})
                state["pending_launches"].pop(job_id, None)
                save()
            jobs = api(path="/api/agent/jobs")["jobs"]
            for job in jobs:
                if job["status"] != "running" or not job["conversation_id"]:
                    continue
                meta = command("get-conversation-metadata", job["conversation_id"])
                if actual_model(meta, catalog) != "gemini-3.8-flash":
                    api({"action": "interrupted", "job_id": job["id"], "error": "工作對話的實際模型無法確認為 Gemini 3.8 Flash"})
                    continue
                execution = desktop_rpc("GetCascadeTrajectory", {"cascadeId": job["conversation_id"]}) if native else meta
                if execution.get("status") in ("IDLE", "CASCADE_RUN_STATUS_IDLE"):
                    last = state.get("continued", {}).get(job["id"], 0)
                    if time.time() - last >= 60:
                        continuation = "請用 gallery_get_agent_job 檢查此工作的待辦，接續完成尚未處理的批次；工作已停止時不要繼續。job_id=" + job["id"]
                        if native:
                            native_send(job["conversation_id"], continuation, selected)
                        else:
                            command("send-message", job["conversation_id"], continuation)
                        state.setdefault("continued", {})[job["id"]] = time.time()
                        save()
        except Exception as exc:
            try:
                api({"action": "heartbeat", "model_id": "", "error": str(exc)[:1000]})
            except Exception:
                pass
            print(type(exc).__name__ + ": " + str(exc)[:300], flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
