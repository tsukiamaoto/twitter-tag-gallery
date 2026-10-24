"""Settings and runtime routes, independent of the gallery presentation layer."""
import os
from pathlib import Path
import time
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import database
import integration_settings as integrations
from background_work import background_status, job_history, request_cancel
from query_cache import QueryCache

router = APIRouter()
_health_cache = QueryCache(capacity=2, ttl=30)


class DeleteImagesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_ids: list[Annotated[int, Field(strict=True, gt=0)]] = Field(min_length=1, max_length=10000)


def _delete_result(action, *args):
    import sqlite3
    from background_work import BackgroundBusy
    try:
        return action(*args)
    except BackgroundBusy:
        raise
    except FileNotFoundError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(409, str(error)) from error
    except sqlite3.Error as error:
        raise HTTPException(503, "資料庫寫入失敗，檔案操作已嘗試還原；請稍後重試或執行檔案復原檢查") from error


@router.post("/api/images/delete")
def delete_image_files(request: DeleteImagesRequest):
    from gallery_deletion import delete_images
    return _delete_result(delete_images, request.image_ids)


class DedupExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["both", "exact", "visual"] = "both"
    scope: Literal["same_folder", "all"] = "same_folder"
    folder: str = Field(default="__ALL__", max_length=500)
    max_mae: float = Field(default=6.5, ge=0.5, le=20.0)
    min_ssim: float = Field(default=0.95, ge=0.80, le=1.0)
    max_diff_frac: float = Field(default=0.005, ge=0.0001, le=0.05)
    selected_delete_ids: list[Annotated[int, Field(strict=True, gt=0)]] | None = None
    groups: list[dict] | None = None


@router.get("/api/dedup/preview")
def dedup_preview(
    mode: Literal["both", "exact", "visual"] = "both",
    scope: Literal["same_folder", "all"] = "same_folder",
    folder: str = "__ALL__",
    max_mae: float = 6.5,
    min_ssim: float = 0.95,
    max_diff_frac: float = 0.005,
    max_groups: int = 300,
):
    from deduplicator import scan_duplicates
    try:
        return scan_duplicates(
            mode=mode,
            scope=scope,
            folder=folder,
            max_mae=max_mae,
            min_ssim=min_ssim,
            max_diff_frac=max_diff_frac,
            max_groups=max_groups,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/api/dedup/execute")
def dedup_execute(request: DedupExecuteRequest):
    from deduplicator import execute_dedup_groups, scan_duplicates

    def _run():
        groups = request.groups
        if groups is None:
            scan_res = scan_duplicates(
                mode=request.mode,
                scope=request.scope,
                folder=request.folder,
                max_mae=request.max_mae,
                min_ssim=request.min_ssim,
                max_diff_frac=request.max_diff_frac,
                max_groups=None,
            )
            groups = scan_res["groups"]
        return execute_dedup_groups(groups, selected_delete_ids=request.selected_delete_ids)

    return _delete_result(_run)



def library_health():
    def read():
        conn = database.get_db()
        try:
            missing = conn.execute("SELECT COUNT(*) FROM images WHERE missing_since IS NOT NULL").fetchone()[0]
            hashed, total = conn.execute("SELECT COUNT(content_hash),COUNT(*) FROM images WHERE missing_since IS NULL").fetchone()
            failed = conn.execute("SELECT COUNT(*) FROM image_processing WHERE status='failed'").fetchone()[0]
            return {"missing": missing, "hashed": hashed, "total": total, "failed_stages": failed}
        finally:
            conn.close()
    return _health_cache.get("health", read)


class IntegrationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mcp_enabled: bool | None = None
    mcp_read_only: bool | None = None
    disabled_tools: list[str] | None = None


class McpServerPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, max_length=64)
    server_id: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=100)
    category: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    transport: Literal["stdio", "http", "sse"] | None = None
    enabled: bool | None = None
    read_only: bool | None = None
    disabled_tools: list[str] | None = None
    command: str | None = Field(default=None, max_length=1000)
    args: list[str] | None = None
    cwd: str | None = Field(default=None, max_length=1000)
    env: dict[str, str | int | float | bool] | None = None
    url: str | None = Field(default=None, max_length=2000)
    headers: dict[str, str | int | float | bool] | None = None
    custom_config: dict | None = None
    sync_client: bool = False
    upsert: bool = False


class SidecarInstallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(min_length=1, max_length=200)


@router.get("/api/settings/artist-identity")
def artist_identity_settings():
    from artist_identity import status
    return {"config": {"mode": "mcp"}, **status()}


@router.get("/api/settings/integrations")
def integration_status():
    return {**integrations.public_config(), "sessions": integrations.sessions()}


@router.post("/api/settings/integrations")
def save_integrations(request: IntegrationUpdate):
    try:
        return integrations.save(request.model_dump(exclude_none=True))
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/settings/mcp/servers")
def list_mcp_servers():
    return {
        "servers": integrations.list_servers(),
        "client_config_path": str(integrations.antigravity_mcp_config_path()),
    }


@router.get("/api/settings/mcp/servers/{server_id}")
def get_mcp_server(server_id: str):
    try:
        return {"server": integrations.get_server(server_id)}
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@router.post("/api/settings/mcp/servers")
def create_mcp_server(request: McpServerPayload):
    payload = request.model_dump(exclude_none=True)
    sync_client = bool(payload.pop("sync_client", False))
    upsert = bool(payload.pop("upsert", False))
    target_id = payload.get("id") or payload.get("server_id") or ""
    try:
        return integrations.save_server(target_id, payload, create_only=not upsert, sync_client=sync_client)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@router.put("/api/settings/mcp/servers/{server_id}")
def update_mcp_server(server_id: str, request: McpServerPayload):
    payload = request.model_dump(exclude_none=True)
    sync_client = bool(payload.pop("sync_client", False))
    payload.pop("upsert", None)
    try:
        return integrations.save_server(server_id, payload, create_only=False, sync_client=sync_client)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@router.delete("/api/settings/mcp/servers/{server_id}")
def remove_mcp_server(server_id: str, sync_client: bool = False):
    try:
        return integrations.delete_server(server_id, sync_client=sync_client)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@router.post("/api/settings/mcp/servers/{server_id}/test")
def test_mcp_server(server_id: str):
    try:
        return integrations.diagnose_server(server_id)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error


@router.post("/api/settings/mcp/sync_client")
def sync_mcp_client():
    try:
        return integrations.sync_client_config()
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error


@router.post("/api/settings/mcp/sidecar/install")
def install_mcp_sidecar(request: SidecarInstallRequest):
    import antigravity_setup
    try:
        result = antigravity_setup.install(request.project_id.strip())
        integrations.save_server(
            integrations.SIDECAR_SERVER_ID,
            {"custom_config": {"sidecar_id": "gallery-publication", "required_model": "gemini-3.8-flash", "project_id": request.project_id.strip()}},
        )
        return {**result, "message": f"已安裝並綁定 Antigravity 專案（{request.project_id.strip()}）的桌面 Sidecar。"}
    except (ValueError, FileNotFoundError, OSError) as error:
        raise HTTPException(400, str(error)) from error


@router.get("/api/settings/mcp/client_config")
def mcp_client_config(
    client: Literal["antigravity"] = "antigravity",
    server_id: str | None = None,
    include_all: bool = False,
):
    try:
        return integrations.client_config(client, server_id=server_id, include_all=include_all)
    except KeyError as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/api/settings/mcp/test")
def test_mcp():
    return integrations.diagnose_mcp()


@router.get("/api/settings/mcp/tools")
def list_mcp_tools():
    from tool_registry import catalog
    return {"tools": catalog()}


class ToolRunRequest(BaseModel):
    arguments: dict = Field(default_factory=dict)


@router.post("/api/settings/mcp/tools/{name}/run", status_code=202)
def run_mcp_tool(name: str, request: ToolRunRequest):
    from background_work import BackgroundBusy
    from tool_runtime import start, ToolBusy
    try:
        return start(name, request.arguments)
    except KeyError as error:
        raise HTTPException(404, "未知的 MCP 工具") from error
    except PermissionError as error:
        raise HTTPException(403, str(error)) from error
    except (BackgroundBusy, ToolBusy) as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get("/api/settings/mcp/runs")
def mcp_tool_runs():
    from tool_runtime import history
    return {"runs": history()}


@router.get("/api/settings/mcp/runs/{run_id}")
def mcp_tool_run(run_id: str):
    from tool_runtime import get_run
    try:
        return {"run": get_run(run_id)}
    except KeyError as error:
        raise HTTPException(404, "找不到工具執行紀錄") from error


@router.post("/api/settings/gemini/test")
def test_gemini():
    raise HTTPException(410, "已移除 Gemini API 直連；請在 Antigravity 管理模型登入")


@router.get("/api/runtime")
def runtime_status():
    from ai_tagger import tagger_engine
    from reverse_search import reverse_search_worker
    from scheduler import scheduler_engine
    from model_control import snapshot
    active = background_status()
    return {
        "ai": tagger_engine.get_status(),
        "reverse": reverse_search_worker.get_status(),
        "schedule": scheduler_engine.get_status(),
        "model": snapshot(),
        "active_job": active,
        "jobs": job_history(12),
        "process_id": os.getpid(),
        "library_health": library_health(),
    }


@router.get("/api/jobs")
def jobs():
    return {"active": background_status(), "jobs": job_history()}


@router.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str):
    value = next((job for job in job_history(100) if job["id"] == job_id), None)
    if value is None or value["status"] not in ("failed", "interrupted", "cancelled"):
        raise HTTPException(400, "此任務無法重試")
    recipe = value.get("recipe", {})
    kind, options = recipe.get("kind"), recipe.get("options", {})
    if kind == "ai":
        from ai_tagger import tagger_engine
        options["scope"] = "all_untagged" if options.get("scope") == "all_untagged" else "root_untagged"
        return tagger_engine.start_batch_tagging(**options)
    if kind == "reverse":
        from reverse_search import reverse_search_worker
        return reverse_search_worker.start(**options)
    if kind == "schedule":
        from scheduler import scheduler_engine
        return scheduler_engine.trigger_pipeline_async(trigger_reason="重試未完成的匯入與歸檔")
    raise HTTPException(400, "此操作請先執行檔案復原，再從原功能重試")


@router.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str):
    try:
        return request_cancel(job_id)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/api/maintenance/recover")
def recover_files():
    from file_operations import recover
    return recover(database.GALLERY_ROOT, database.DATA_DIR, database.get_db)


@router.post("/api/maintenance/backup")
def backup_database():
    from backup_service import backup
    return backup()
