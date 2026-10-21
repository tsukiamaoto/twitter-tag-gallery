"""Model management only; these routes never transform gallery images."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from gguf_models import manager, start_download, stop_download

router = APIRouter(prefix="/api/models/gguf", tags=["GGUF models"])


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=500)
    profile: str | None = Field(default=None, max_length=30)


class Download(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profiles: list[str] = Field(default_factory=lambda: ["klein4b", "qwen21"], min_length=1, max_length=2)


def action(function, *arguments):
    try:
        return function(*arguments)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    except OSError as error:
        raise HTTPException(500, "模型檔案或執行環境無法存取，請檢查資料夾權限") from error


@router.get("")
def status():
    return action(manager.status)


@router.post("/select")
def select(request: Selection):
    return {"selection": action(manager.selection, request.model_id, request.profile), "message": "已儲存模型選擇；按載入才會使用記憶體"}


@router.post("/load", status_code=202)
def load(request: Selection):
    return action(manager.load, request.model_id, request.profile)


@router.post("/unload")
def unload():
    return action(manager.unload)


@router.post("/download", status_code=202)
def download(request: Download):
    return action(start_download, request.profiles)


@router.post("/download/stop")
def stop():
    return action(stop_download)
