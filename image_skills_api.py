from fastapi import APIRouter, HTTPException
import image_skills as store
from gguf_models import manager
from gguf_api import action

router = APIRouter(prefix="/api/image-skills", tags=["Image skills"])


def run(function, *arguments):
    try:
        return action(function, *arguments)
    except KeyError as error:
        raise HTTPException(404, "找不到技能") from error


@router.get("")
def status():
    return run(store.status)


@router.post("", status_code=201)
def create(request: store.SkillInput):
    return {"skill": run(store.save, request), "message": "已建立影像修補技能"}


@router.put("/{skill_id}")
def update(skill_id: str, request: store.SkillInput):
    return {"skill": run(store.save, request, skill_id), "message": "已儲存技能"}


@router.delete("/{skill_id}")
def archive(skill_id: str):
    run(store.archive, skill_id)
    return {"message": "已封存技能，內容保留於本機技能檔案"}


@router.get("/{skill_id}/prepare")
def prepare(skill_id: str):
    return run(store.prepare, skill_id)


@router.post("/{skill_id}/activate")
def activate(skill_id: str):
    return {"prepared": run(store.activate, skill_id, manager.selection),
            "message": "已套用技能與模型選擇；可到 AI 模型頁載入。影像尚未開始處理。"}
