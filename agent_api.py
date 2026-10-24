"""Desktop bridge, prompt editing and model-workflow HTTP endpoints."""
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import agent_jobs
import prompt_store

router = APIRouter()


class CreateJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow: Literal["publication", "artist_identity"] = "publication"
    folder: str = Field(default="__ALL_ARTIST_FOLDERS__", max_length=500)
    pad_digits: int = Field(default=4, ge=3, le=6)
    artist_name: str = Field(default="", max_length=300)
    profile_urls: list[str] = Field(default_factory=list, max_length=10)


class PromptUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    usage_prompt: str = Field(default="", max_length=16000)
    task_prompt: str | None = Field(default=None, max_length=24000)
    revision: int = Field(ge=0)
    reset: bool = False


class BridgeUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["heartbeat", "claim", "launched", "launch_failed", "interrupted"]
    model_id: str = Field(default="", max_length=150)
    error: str = Field(default="", max_length=1000)
    job_id: str = Field(default="", max_length=32)
    conversation_id: str = Field(default="", max_length=100)


def call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/agent/jobs", status_code=202)
def create_job(request: CreateJob):
    context = {"folder": request.folder, "pad_digits": request.pad_digits} if request.workflow == "publication" else {"artist_name": request.artist_name, "profile_urls": request.profile_urls}
    if request.workflow == "artist_identity" and not request.artist_name and not request.profile_urls:
        raise HTTPException(422, "需要畫師名稱或帳號")
    return call(agent_jobs.create, request.workflow, context)


@router.get("/api/agent/jobs")
def agent_history():
    return {"jobs": agent_jobs.history(), "bridge": agent_jobs.bridge_status()}


@router.get("/api/agent/jobs/{job_id}")
def get_agent_job(job_id: str):
    return call(agent_jobs.get, job_id)


@router.post("/api/agent/jobs/{job_id}/{action}")
def control_agent_job(job_id: str, action: Literal["stop", "resume"]):
    return call(agent_jobs.control, job_id, action)


@router.get("/api/rename/publication-preview")
def publication_preview(folder: str = "__ALL_ARTIST_FOLDERS__"):
    from publication_dates import overview
    return call(overview, folder)


@router.put("/api/settings/mcp/tools/{name}/prompt")
def save_prompt(name: str, request: PromptUpdate):
    return call(prompt_store.save, name, request.usage_prompt, request.task_prompt, request.revision, request.reset)


@router.post("/api/agent/bridge")
def update_bridge(request: BridgeUpdate, x_gallery_bridge_token: str = Header(default="")):
    call(agent_jobs.require_bridge, x_gallery_bridge_token)
    if request.action == "heartbeat":
        return agent_jobs.heartbeat(request.model_id, request.error)
    if request.action == "claim":
        return {"job": agent_jobs.claim()}
    if request.action == "launched":
        return call(agent_jobs.launched, request.job_id, request.conversation_id)
    if request.action == "launch_failed":
        agent_jobs.launch_failed(request.job_id, request.error)
        return {"ok": True}
    if request.action == "interrupted":
        import time
        with agent_jobs.connection() as conn, conn:
            conn.execute("UPDATE agent_jobs SET status='interrupted',error=?,updated_at=? WHERE id=? AND status='running'", (request.error, time.time(), request.job_id))
        return {"ok": True}
