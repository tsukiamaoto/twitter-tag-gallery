"""Settings and runtime routes, independent of the gallery presentation layer."""
import os
from pathlib import Path
import time
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import database
import integration_settings as integrations
from background_work import background_status, job_history,request_cancel
from query_cache import QueryCache

router = APIRouter()
_health_cache=QueryCache(capacity=2,ttl=30)


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








def library_health():
    def read():
        conn=database.get_db()
        try:
            missing=conn.execute("SELECT COUNT(*) FROM images WHERE missing_since IS NOT NULL").fetchone()[0]
            hashed,total=conn.execute("SELECT COUNT(content_hash),COUNT(*) FROM images WHERE missing_since IS NULL").fetchone()
            failed=conn.execute("SELECT COUNT(*) FROM image_processing WHERE status='failed'").fetchone()[0]
            return {"missing":missing,"hashed":hashed,"total":total,"failed_stages":failed}
        finally:conn.close()
    return _health_cache.get("health",read)


class IntegrationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mcp_enabled: bool | None = None
    mcp_read_only: bool | None = None
    disabled_tools: list[str] | None = None



@router.get("/api/settings/integrations")
def integration_status():
    return {**integrations.public_config(),"sessions":integrations.sessions()}


@router.post("/api/settings/integrations")
def save_integrations(request: IntegrationUpdate):
    try:
        return integrations.save(request.model_dump(exclude_none=True))
    except ValueError as error:
        raise HTTPException(400,str(error)) from error


@router.get("/api/settings/mcp/client_config")
def mcp_client_config(client: Literal["antigravity", "gemini_cli"] = "antigravity"):
    return integrations.client_config(client)


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
    raise HTTPException(410, "已移除 Gemini API 直連；請在 Antigravity 或 Gemini CLI 管理模型登入")


@router.get("/api/runtime")
def runtime_status():
    from ai_tagger import tagger_engine
    from reverse_search import reverse_search_worker
    from scheduler import scheduler_engine
    from model_control import snapshot
    active=background_status()
    return {"ai":tagger_engine.get_status(),"reverse":reverse_search_worker.get_status(),
            "schedule":scheduler_engine.get_status(),"model":snapshot(),"active_job":active,
            "jobs":job_history(12),"process_id":os.getpid(),"library_health":library_health()}


@router.get("/api/jobs")
def jobs():
    return {"active":background_status(),"jobs":job_history()}


@router.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str):
    value=next((job for job in job_history(100) if job["id"]==job_id),None)
    if value is None or value["status"] not in ("failed","interrupted","cancelled"):
        raise HTTPException(400,"此任務無法重試")
    recipe=value.get("recipe",{})
    kind,options=recipe.get("kind"),recipe.get("options",{})
    if kind=="ai":
        from ai_tagger import tagger_engine
        options["scope"]="all_untagged" if options.get("scope")=="all_untagged" else "root_untagged"
        return tagger_engine.start_batch_tagging(**options)
    if kind=="reverse":
        from reverse_search import reverse_search_worker
        return reverse_search_worker.start(**options)
    if kind=="schedule":
        from scheduler import scheduler_engine
        return scheduler_engine.trigger_pipeline_async(trigger_reason="重試未完成的匯入與歸檔")
    raise HTTPException(400,"此操作請先執行檔案復原，再從原功能重試")


@router.post("/api/jobs/{job_id}/stop")
def stop_job(job_id: str):
    try:
        return request_cancel(job_id)
    except ValueError as error:
        raise HTTPException(400,str(error)) from error


@router.post("/api/maintenance/recover")
def recover_files():
    from file_operations import recover
    return recover(database.GALLERY_ROOT,database.DATA_DIR,database.get_db)


@router.post("/api/maintenance/backup")
def backup_database():
    from backup_service import backup
    return backup()
