# -*- coding: utf-8 -*-
import os
import re
import shutil
import subprocess
import asyncio
from pathlib import Path
from typing import Annotated, List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel,Field
from file_safety import UnsafePath,validate_source,gallery_path
from native_explorer import reveal_file
from gallery_artist_service import sidebar_tag_rows, recent_sidebar_tag_rows
from background_work import BackgroundBusy, mark_interactive, exclusive_job, background_checkpoint
from thumbnail_cache import ThumbnailCache, ThumbnailQueueFull

from database import (
    GALLERY_ROOT,
    THUMB_DIR,
    NON_ARTIST_FOLDERS,
    RE_DATE_SEQ,
    ACTIVITY_LOG_FILE,
    get_db,
    init_db,
    scan_library,
    ensure_tag,
    refresh_tag_counts,
    run_db_write_with_retry,
    log_operation,
    get_operation_logs,
    clear_operation_logs,
)
from ai_tagger import tagger_engine, preprocess_image_fast, lookup_pixiv_artists_batch
from zh_dict import get_tag_display_name, resolve_artist_folder_name, DANBOORU_ARTIST_TO_FOLDER
from renamer import (
    win_natural_key,
    generate_rename_plan_for_folder,
    generate_rename_preview,
    execute_rename_plan,
    list_rename_logs,
    undo_rename_batch,
    get_next_sequence_filename_for_folder,
    get_folder_sequence_state,
)

app = FastAPI(title="Twitter & Pixiv AI 智慧標籤圖庫系統")
from management_api import router as management_router
app.include_router(management_router)
from agent_api import router as agent_router
app.include_router(agent_router)
from gguf_api import router as gguf_router
app.include_router(gguf_router)
from image_skills_api import router as image_skills_router
app.include_router(image_skills_router)
from agent_skills_api import router as agent_skills_router
app.include_router(agent_skills_router)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
thumbnail_cache = ThumbnailCache(GALLERY_ROOT, THUMB_DIR, get_db)


@app.exception_handler(BackgroundBusy)
async def background_busy_response(request: Request, exc: BackgroundBusy):
    return JSONResponse(status_code=409, content=exc.response())


@app.exception_handler(UnsafePath)
async def unsafe_path_response(request: Request, exc: UnsafePath):
    return JSONResponse(status_code=400,content={"detail":str(exc)})


class BatchTagRequest(BaseModel):
    image_ids: List[int]
    add_tags: List[Dict[str, str]] = []
    remove_tag_names: List[str] = []
    move_to_artist_folder: Optional[str] = None


class MoveFilesRequest(BaseModel):
    image_ids: List[int] = []
    target_folder: str = ""
    mode: str = "selected"  # "selected" | "ai_suggested" | "by_date_ym"
    auto_rename_on_move: bool = True
    min_confidence: float = 0.20
    min_images_per_artist: int = 1
    only_existing_folders: bool = False


class RenameExecuteRequest(BaseModel):
    folder: str  # Specific folder name, "__ROOT__", or "__ALL_ARTIST_FOLDERS__"
    pad_digits: int = Field(4,ge=3,le=6)
    custom_date: Optional[str] = None
    custom_artist: Optional[str] = None


@exclusive_job("繪師歸檔索引同步")
def sync_root_suggested_folders(min_confidence: float = 0.25) -> int:
    """
    確保所有根目錄 (folder = '') 中已經擁有「已認證畫師標籤」（來自 Gemini MCP、Danbooru、Pixiv、SauceNAO、IQDB、Twitter 或高信心度 >= 0.75 AI）的圖片，
    都會自動解析出乾淨的繪師資料夾名稱並寫入 suggested_folder。
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT i.id, t.name, it.confidence, it.source
        FROM images i
        JOIN image_tags it ON i.id = it.image_id
        JOIN tags t ON t.id = it.tag_id
        WHERE i.folder = ''
          AND t.category = 'artist'
          AND t.name NOT LIKE 'pixiv_id:%'
          AND (
              (it.source IN ('gemini_mcp', 'danbooru', 'pixiv', 'saucenao', 'iqdb', 'twitter', 'manual', 'folder') AND it.confidence >= ?)
              OR (it.source = 'ai' AND it.confidence >= 0.75)
          )
        ORDER BY
            i.id,
            CASE it.source
                WHEN 'manual' THEN 1
                WHEN 'gemini_mcp' THEN 2
                WHEN 'danbooru' THEN 3
                WHEN 'pixiv' THEN 4
                WHEN 'saucenao' THEN 5
                WHEN 'twitter' THEN 6
                WHEN 'iqdb' THEN 7
                ELSE 8
            END ASC,
            it.confidence DESC
        """,
        (min_confidence,),
    ).fetchall()

    best_by_img: Dict[int, str] = {}
    from artist_identity import known_folder
    for r in rows:
        iid = r["id"]
        tname = r["name"]
        folder_name = known_folder(tname)
        if not folder_name:
            continue
        if iid not in best_by_img:
            best_by_img[iid] = folder_name

    with conn:
        # Clear stale suggested_folder on root images that only had low-confidence AI guesses
        conn.execute(
            """
            UPDATE images
            SET suggested_folder = NULL
            WHERE folder = ''
              AND id NOT IN (
                  SELECT it.image_id FROM image_tags it
                  JOIN tags t ON t.id = it.tag_id
                  WHERE t.category = 'artist'
                    AND (
                        it.source IN ('gemini_mcp', 'danbooru', 'pixiv', 'saucenao', 'iqdb', 'twitter', 'manual', 'folder')
                        OR (it.source = 'ai' AND it.confidence >= 0.75)
                    )
              )
            """
        )
        updates = [(fname, iid) for iid, fname in best_by_img.items()]
        conn.executemany("UPDATE images SET suggested_folder=NULL WHERE id=?",
                         [(iid,) for iid in {r["id"] for r in rows} - best_by_img.keys()])
        if updates:
            conn.executemany(
                "UPDATE images SET suggested_folder = ? WHERE id = ?",
                updates,
            )
    conn.close()
    return len(updates)


@app.on_event("startup")
def on_startup():
    init_db()
    from file_operations import recover
    from database import DATA_DIR
    try:
        app.state.recovery_report = recover(GALLERY_ROOT,DATA_DIR,get_db)
    except BackgroundBusy:
        app.state.recovery_report = {"deferred":True}
    conn = get_db()
    cnt = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    conn.close()
    if cnt == 0:
        print("初次啟動，正在建立 D:\\圖檔\\twitter 索引資料庫...")
        res = scan_library()
        print(f"索引完成：{res}")
    try:
        sync_root_suggested_folders()
    except BackgroundBusy:
        pass  # An existing pipeline owns this maintenance; browsing can start.
    from scheduler import scheduler_engine
    scheduler_engine.start_daemon()
    thumbnail_cache.start_warming()


@app.on_event("shutdown")
def on_shutdown():
    from database import close_db_pool
    thumbnail_cache.close()
    close_db_pool()




@app.get("/", response_class=HTMLResponse)
def index():
    html_file = STATIC_DIR / "index.html"
    if not html_file.exists():
        return HTMLResponse("<h1>UI loading...</h1>")
    html = html_file.read_text(encoding="utf-8")
    for name in ("gallery-core.js","gallery-app.js","gallery-base.css","gallery-shell.css","gallery-shell.js","integration-settings.js","gguf-models.js","gguf-models.css","image-skills.js","image-skills.css","workspace-navigation.js","workspace-navigation.css"):
        html = html.replace(f'/static/{name}"',f'/static/{name}?v={(STATIC_DIR/name).stat().st_mtime_ns}"')
    return HTMLResponse(html)


@app.get("/api/stats")
def api_stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    root_cnt = conn.execute("SELECT COUNT(*) FROM images WHERE folder = ''").fetchone()[0]
    ai_cnt = conn.execute("SELECT COUNT(*) FROM images WHERE ai_tagged = 1").fetchone()[0]
    sug_cnt = conn.execute(
        "SELECT COUNT(*) FROM images WHERE folder = '' AND suggested_folder IS NOT NULL AND suggested_folder != ''"
    ).fetchone()[0]
    media_cnt = conn.execute("SELECT COUNT(*) FROM images WHERE media_type != 'image'").fetchone()[0]
    pixiv_cnt = conn.execute("SELECT COUNT(*) FROM images WHERE pixiv_id IS NOT NULL").fetchone()[0]

    cat_counts = {}
    for row in conn.execute("SELECT category, COUNT(*) as c FROM tags WHERE post_count > 0 GROUP BY category"):
        cat_counts[row["category"]] = row["c"]

    conn.close()
    return {
        "total_images": total,
        "root_unorganized": root_cnt,
        "folder_organized": total - root_cnt,
        "ai_tagged": ai_cnt,
        "suggested_folder_count": sug_cnt,
        "media_gif_video": media_cnt,
        "pixiv_id_count": pixiv_cnt,
        "category_counts": cat_counts,
        "ai_status": tagger_engine.get_status(),
    }


@app.get("/api/folders")
def api_folders():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT folder, COUNT(*) as count, COALESCE(MAX(mtime), 0) as last_updated
        FROM images
        WHERE folder != ''
        GROUP BY folder
        ORDER BY count DESC, folder ASC
        """
    ).fetchall()
    conn.close()
    return [
        {"name": r["folder"], "count": r["count"], "last_updated": r["last_updated"]}
        for r in rows
    ]


@app.get("/api/tags")
def api_tags(
    category: str = Query("all"),
    q: str = Query(""),
    limit: int = Query(250, ge=1),
    offset: Annotated[int, Query(ge=0)] = 0,
    paged: bool = False,
):
    import time as _time
    page_limit = limit
    if paged:
        limit = offset + page_limit + 1  # One lookahead row determines whether more exist.

    def response(items):
        if not paged:
            return items
        page = items[offset:offset + page_limit]
        return {"items": page, "next_offset": offset + len(page),
                "has_more": len(items) > offset + page_limit}

    conn = get_db()
    try:
        recent_special_items = []
        if category in ("all", "recent"):
            now_ts = _time.time()
            q_lower = q.strip().lower()
            if not q_lower or "最近" in q_lower or "recent" in q_lower or "最新" in q_lower:
                total_all = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
                recent_special_items.append({
                    "id": -1,
                    "name": "最近",
                    "category": "recent",
                    "zh_name": "🕒 最近 (全部圖片：編輯時間 最新➔最舊)",
                    "count": total_all,
                })
            if category == "recent":
                buckets = [
                    ("最近:24小時內", "🕒 最近 24 小時內編輯", now_ts - 86400),
                    ("最近:7天內", "🕒 最近 7 天內編輯", now_ts - 86400 * 7),
                    ("最近:30天內", "🕒 最近 30 天內編輯", now_ts - 86400 * 30),
                    ("最近:90天內", "🕒 最近 90 天內編輯", now_ts - 86400 * 90),
                    ("最近:1年內", "🕒 最近 1 年內編輯", now_ts - 86400 * 365),
                ]
                for b_name, b_zh, since_ts in buckets:
                    cnt_b = conn.execute("SELECT COUNT(*) FROM images WHERE mtime >= ?", (since_ts,)).fetchone()[0]
                    if cnt_b > 0:
                        recent_special_items.append({
                            "id": -2,
                            "name": b_name,
                            "category": "recent",
                            "zh_name": b_zh,
                            "count": cnt_b,
                        })

                # 同時列出最近編輯圖片中最常出現的畫師與標籤（依圖片最新 mtime 排序）
                recent_tag_rows = recent_sidebar_tag_rows(conn, q, limit)
                for r in recent_tag_rows:
                    is_dict = isinstance(r, dict)
                    zh = r["zh_name"] if is_dict and r["category"] == "artist" else get_tag_display_name(r["name"], r["category"], r["zh_name"])
                    recent_special_items.append({
                        "id": r["id"],
                        "name": r["name"],
                        "category": r["category"],
                        "zh_name": zh,
                        "count": r["post_count"],
                        "aliases": r.get("aliases", [r["name"]]) if is_dict else [r["name"]],
                    })
                return response(recent_special_items)

        rows = sidebar_tag_rows(conn, category, q, limit)
    finally:
        conn.close()

    result = list(recent_special_items)
    for r in rows:
        is_dict = isinstance(r, dict)
        zh = r["zh_name"] if is_dict and r["category"] == "artist" else get_tag_display_name(r["name"], r["category"], r["zh_name"])
        result.append({
            "id": r["id"],
            "name": r["name"],
            "category": r["category"],
            "zh_name": zh,
            "count": r["post_count"],
            "aliases": r.get("aliases", [r["name"]]) if is_dict else [r["name"]],
        })
    return response(result)


@app.get("/api/images")
def api_images(
    include_tags: str = Query(""),
    exclude_tags: str = Query(""),
    folder: str = Query("__ALL__"),
    media_type: str = Query("all"),
    ai_status: str = Query("all"),
    q: str = Query(""),
    sort: str = Query("natural_asc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=10, le=200),
    compact: bool = False,
):
    from gallery_query import search_images
    return search_images(include_tags, exclude_tags, folder, media_type, ai_status,
                         q, sort, page, page_size, connect=get_db, prefetch=thumbnail_cache.prefetch,
                         compact=compact)


@app.get("/api/image/{image_id}")
def api_image_detail(image_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Image not found")

    tag_rows = conn.execute(
        """
        SELECT t.id, t.name, t.category, t.zh_name, it.confidence, it.source
        FROM image_tags it
        JOIN tags t ON t.id = it.tag_id
        WHERE it.image_id = ?
        ORDER BY
            CASE t.category
                WHEN 'artist' THEN 1
                WHEN 'copyright' THEN 2
                WHEN 'character' THEN 3
                WHEN 'rating' THEN 4
                WHEN 'general' THEN 5
                ELSE 6
            END,
            it.confidence DESC
        """,
        (image_id,),
    ).fetchall()
    conn.close()

    tags = []
    for tr in tag_rows:
        zh = get_tag_display_name(tr["name"], tr["category"], tr["zh_name"])
        tags.append({
            "id": tr["id"],
            "name": tr["name"],
            "category": tr["category"],
            "zh_name": zh,
            "confidence": round(tr["confidence"], 3),
            "source": tr["source"],
        })

    return {
        "id": row["id"],
        "rel_path": row["rel_path"],
        "full_path": str(GALLERY_ROOT / row["rel_path"]),
        "filename": row["filename"],
        "file_missing": not (GALLERY_ROOT / row["rel_path"]).is_file(),
        "folder": row["folder"],
        "size_kb": round(row["size_bytes"] / 1024, 1),
        "media_type": row["media_type"],
        "pixiv_id": row["pixiv_id"],
        "ai_tagged": bool(row["ai_tagged"]),
        "suggested_folder": row["suggested_folder"],
        "tags": tags,
    }


@app.get("/api/thumb/{image_id}")
async def api_thumb(image_id: int, request: Request):
    mark_interactive()
    try:
        result = await run_in_threadpool(thumbnail_cache.cached, image_id)
        if result is None:
            future = thumbnail_cache.submit(image_id, foreground=True)
            # A disconnected browser must not cancel work shared by other tabs.
            result = await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=30)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Image or file not found")
    except (ThumbnailQueueFull, asyncio.TimeoutError):
        raise HTTPException(status_code=503, detail="縮圖處理中，請稍後重試", headers={"Retry-After": "1"})
    except (OSError, ValueError, SyntaxError):
        raise HTTPException(status_code=422, detail="Unable to generate thumbnail")

    headers = {"ETag": result.etag, "Cache-Control": "public, max-age=600, stale-while-revalidate=86400"}
    if request.headers.get("if-none-match") == result.etag:
        return Response(status_code=304, headers=headers)
    if result.video:
        svg = """<svg xmlns="http://www.w3.org/2000/svg" width="320" height="320" viewBox="0 0 320 320">
        <rect width="320" height="320" fill="#1e1e2e"/>
        <circle cx="160" cy="160" r="48" fill="#6366f1" opacity="0.9"/>
        <polygon points="145,135 145,185 188,160" fill="#ffffff"/>
        <text x="160" y="245" text-anchor="middle" fill="#a1a1aa" font-family="sans-serif" font-size="16">MP4 影片 (點擊播放)</text>
        </svg>"""
        return Response(content=svg, media_type="image/svg+xml", headers=headers)
    return FileResponse(str(result.path), media_type="image/jpeg", headers=headers)


@app.get("/api/raw/{image_id}")
def api_raw(image_id: int):
    conn = get_db()
    row = conn.execute("SELECT rel_path FROM images WHERE id = ?", (image_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Image not found")
    full_path = GALLERY_ROOT / row["rel_path"]
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File missing")
    return FileResponse(str(full_path))


@app.post("/api/tags/batch")
@exclusive_job("編輯圖片標籤")
def api_batch_tags(req: BatchTagRequest):
    if not req.image_ids:
        return {"updated": 0}

    from gallery_tag_service import edit_tags
    try:
        edit_tags(req.image_ids, req.add_tags, req.remove_tag_names)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error

    moved_count = 0
    if req.move_to_artist_folder and req.move_to_artist_folder.strip():
        move_res = move_images_to_folder(req.image_ids, req.move_to_artist_folder.strip(), auto_rename=True)
        moved_count = move_res.get("moved", 0)

    added_names = [t.get("name", "").strip() for t in req.add_tags if t.get("name", "").strip()]
    removed_names = [r.strip() for r in req.remove_tag_names if r.strip()]
    log_operation(
        category="tag_edit",
        action="手動/批次變更標籤",
        summary=(
            f"對 {len(req.image_ids)} 張圖片變更標籤："
            + (f"新增 [{', '.join(added_names)}] " if added_names else "")
            + (f"移除 [{', '.join(removed_names)}] " if removed_names else "")
            + (f"並移入資料夾「{req.move_to_artist_folder.strip()}」({moved_count} 張)" if moved_count else "")
        ).strip(),
        status="success",
        affected_count=len(req.image_ids),
        details={
            "image_count": len(req.image_ids),
            "add_tags": added_names,
            "remove_tags": removed_names,
            "moved_to_folder": req.move_to_artist_folder,
            "moved_count": moved_count,
        },
    )
    return {"updated": len(req.image_ids), "moved": moved_count}




def move_images_to_folder(image_ids, target_folder, auto_rename=True, refresh_counts=True):
    from gallery_service import move_images_to_folder as move
    return move(image_ids, target_folder, auto_rename, refresh_counts)


@app.get("/api/organize/artist_folders_preview")
def api_organize_artist_folders_preview(
    min_confidence: float = Query(0.20, ge=0.05, le=1.0),
    min_images_per_artist: int = Query(1, ge=1, le=100),
    only_existing_folders: bool = Query(False),
):
    """
    預覽根目錄 (folder = '') 中所有已辨識出繪師的圖片，將如何對應至「既有繪師資料夾」或「自動新建繪師資料夾」。
    """
    sync_root_suggested_folders(min_confidence=min_confidence)
    conn = get_db()

    existing_folders_rows = conn.execute(
        "SELECT DISTINCT folder FROM images WHERE folder != ''"
    ).fetchall()
    existing_folders_set = {r["folder"] for r in existing_folders_rows}
    for d in GALLERY_ROOT.iterdir():
        if d.is_dir() and not d.name.startswith(".") and d.name not in NON_ARTIST_FOLDERS:
            existing_folders_set.add(d.name)
    existing_lower_map = {f.lower(): f for f in existing_folders_set}

    rows = conn.execute(
        """
        SELECT
            i.id,
            i.filename,
            i.suggested_folder,
            MAX(CASE WHEN t.category = 'artist' THEN it.confidence ELSE 0.25 END) AS max_conf,
            GROUP_CONCAT(DISTINCT CASE WHEN t.category = 'artist' THEN it.source END) AS sources
        FROM images i
        LEFT JOIN image_tags it ON i.id = it.image_id
        LEFT JOIN tags t ON t.id = it.tag_id
        WHERE i.folder = ''
          AND i.suggested_folder IS NOT NULL
          AND i.suggested_folder != ''
        GROUP BY i.id
        """
    ).fetchall()

    untagged_root_count = conn.execute(
        """
        SELECT COUNT(*) FROM images i
        WHERE i.folder = ''
          AND i.media_type IN ('image', 'gif', 'video')
          AND (
              i.ai_tagged = 0
              OR NOT EXISTS (
                  SELECT 1 FROM image_tags it
                  JOIN tags t ON it.tag_id = t.id
                  WHERE it.image_id = i.id AND t.category IN ('general', 'character', 'copyright')
              )
          )
        """
    ).fetchone()[0]
    unqueried_pixiv_root_count = conn.execute(
        """
        SELECT COUNT(*) FROM images
        WHERE folder = '' AND pixiv_id IS NOT NULL
          AND id NOT IN (SELECT image_id FROM image_tags WHERE source IN ('pixiv', 'pixiv_checked'))
        """
    ).fetchone()[0]
    pending_identity_count = conn.execute("""SELECT COUNT(*) FROM images i
        WHERE i.folder='' AND i.missing_since IS NULL AND i.suggested_folder IS NULL
          AND EXISTS (SELECT 1 FROM image_tags it JOIN tags t ON t.id=it.tag_id
                      WHERE it.image_id=i.id AND t.category='artist')""").fetchone()[0]
    conn.close()

    by_folder: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        conf = float(r["max_conf"] or 0.25)
        if conf < min_confidence:
            continue
        raw_sug = r["suggested_folder"].strip()
        resolved = existing_lower_map.get(raw_sug.lower(), raw_sug)
        by_folder.setdefault(resolved, []).append({
            "id": r["id"],
            "filename": r["filename"],
            "confidence": round(conf, 3),
            "sources": r["sources"] or "ai",
        })

    folder_items = []
    total_ready_images = 0
    new_folders_count = 0
    existing_folders_count = 0

    for fname, items in sorted(by_folder.items(), key=lambda kv: (-len(kv[1]), kv[0].lower())):
        is_existing = fname in existing_folders_set
        if only_existing_folders and not is_existing:
            continue
        if not is_existing and len(items) < min_images_per_artist:
            continue

        if is_existing:
            existing_folders_count += 1
        else:
            new_folders_count += 1
        total_ready_images += len(items)

        avg_conf = sum(x["confidence"] for x in items) / len(items)
        all_sources = sorted({s for x in items for s in (x["sources"] or "").split(",") if s})
        folder_items.append({
            "folder_name": fname,
            "is_new_folder": not is_existing,
            "image_count": len(items),
            "avg_confidence": round(avg_conf, 3),
            "sources": all_sources,
            "sample_filenames": [x["filename"] for x in items[:4]],
            "image_ids": [x["id"] for x in items],
        })

    return {
        "total_ready_images": total_ready_images,
        "pending_identity": pending_identity_count,
        "total_artist_folders": len(folder_items),
        "new_folders_to_create": new_folders_count,
        "existing_folders_to_update": existing_folders_count,
        "untagged_root_remaining": untagged_root_count,
        "unqueried_pixiv_remaining": unqueried_pixiv_root_count,
        "folders": folder_items,
    }


@app.post("/api/organize/move")
@exclusive_job("批次歸檔")
def api_organize_move(req: MoveFilesRequest):
    if req.mode == "selected":
        if not req.image_ids:
            raise HTTPException(status_code=400, detail="未選取任何圖片")
        res = move_images_to_folder(req.image_ids, req.target_folder, auto_rename=req.auto_rename_on_move, refresh_counts=True)
        log_operation(
            category="organize",
            action="手動選取圖片歸檔搬移",
            summary=f"將 {res.get('moved', 0)} 張選取圖片搬移至資料夾「{res.get('target_folder') or '根目錄'}」",
            status="success",
            affected_count=res.get("moved", 0),
            details={"mode": req.mode, "target_folder": res.get("target_folder"), "moved": res.get("moved", 0)},
        )
        return res

    elif req.mode in ("ai_suggested", "auto_artist_folders"):
        from artist_identity import resolve_image_folders
        conn = get_db()
        try:
            root_ids = [r[0] for r in conn.execute("SELECT id FROM images WHERE folder='' AND missing_since IS NULL")]
        finally:
            conn.close()
        identity_report = resolve_image_folders(root_ids)
        preview = api_organize_artist_folders_preview(
            min_confidence=req.min_confidence,
            min_images_per_artist=req.min_images_per_artist,
            only_existing_folders=req.only_existing_folders,
        )
        total_moved = 0
        new_created = 0
        existing_updated = 0
        moved_details = []
        for f_info in preview["folders"]:
            folder_name = f_info["folder_name"]
            ids = f_info["image_ids"]
            from gallery_service import move_images_to_artist_folder
            res = move_images_to_artist_folder(ids, folder_name, auto_rename=req.auto_rename_on_move, refresh_counts=False)
            moved_cnt = res.get("moved", 0)
            if moved_cnt > 0:
                total_moved += moved_cnt
                if f_info["is_new_folder"]:
                    new_created += 1
                else:
                    existing_updated += 1
                moved_details.append({
                    "folder": folder_name,
                    "is_new_folder": f_info["is_new_folder"],
                    "moved": moved_cnt,
                })
        if total_moved > 0:
            db_conn = get_db()
            try:
                run_db_write_with_retry(db_conn, lambda c: refresh_tag_counts(c))
            finally:
                db_conn.close()
        out_res = {
            "moved": total_moved,
            "pending_identity": len(identity_report["pending_image_ids"]),
            "folders_updated": len(moved_details),
            "new_folders_created": new_created,
            "existing_folders_updated": existing_updated,
            "details": moved_details,
        }
        log_operation(
            category="organize",
            action="智慧繪師資料夾批次歸檔",
            summary=f"自動歸檔 {total_moved} 張根目錄圖片至 {len(moved_details)} 個繪師資料夾 (新建 {new_created} 個、既有 {existing_updated} 個)",
            status="success",
            affected_count=total_moved,
            details=out_res,
        )
        return out_res

    elif req.mode == "by_date_ym":
        from gallery_service import organize_by_date
        result = organize_by_date()
        log_operation(category="organize",action="按年月歸檔",summary=f"完成 {result['moved']} 張圖片按年月歸檔",affected_count=result["moved"])
        return result

    raise HTTPException(status_code=400, detail="Unknown organize mode")



# === Rename Endpoints ===
@app.get("/api/rename/preview")
def api_rename_preview(
    folder: str = Query(...),
    pad_digits: int = Query(4, ge=3, le=6),
    custom_date: Optional[str] = Query(None),
    custom_artist: Optional[str] = Query(None),
):
    return generate_rename_preview(folder, pad_digits, custom_date, custom_artist)


@app.post("/api/rename/execute")
@exclusive_job("批次改名")
def api_rename_execute(req: RenameExecuteRequest):
    if req.folder == "__ALL_ARTIST_FOLDERS__":
        conn = get_db()
        f_rows = conn.execute(
            "SELECT DISTINCT folder FROM images WHERE folder != '' ORDER BY folder ASC"
        ).fetchall()
        conn.close()
        total_renamed = 0
        folders_done = 0
        for fr in f_rows:
            fname = fr["folder"]
            if fname in NON_ARTIST_FOLDERS:
                continue
            plan = generate_rename_plan_for_folder(fname, pad_digits=req.pad_digits)
            if plan["changed_count"] > 0:
                res = execute_rename_plan(plan)
                total_renamed += res.get("renamed", 0)
                folders_done += 1
        return {
            "renamed": total_renamed,
            "folders_updated": folders_done,
        }
    else:
        plan = generate_rename_plan_for_folder(
            folder_name=req.folder,
            pad_digits=req.pad_digits,
            custom_date=req.custom_date,
            custom_artist=req.custom_artist,
        )
        return execute_rename_plan(plan)


@app.get("/api/rename/logs")
def api_rename_logs():
    return list_rename_logs()


@app.post("/api/rename/undo/{batch_id}")
def api_rename_undo(batch_id: str):
    return undo_rename_batch(batch_id)


class DecensorRequest(BaseModel):
    mode: str = "auto"
    conf_thresh: float = 0.22
    save_mode: str = "overwrite"
    custom_boxes: Optional[List[List[float]]] = None


# === AI Tagger & Decensor Endpoints ===
@app.post("/api/ai/start")
def api_ai_start(
    scope: str = Query("root_untagged"),
    limit: int = Query(0),
    general_thresh: float = Query(0.38),
    artist_thresh: float = Query(0.65),
    auto_move_to_artist_folder: bool = Query(False),
    auto_unload: bool = Query(True),
    run_local_ai: bool = Query(True),
    run_reverse_search_calibrate: bool = Query(True),
    reverse_search_limit: int = Query(30),
    min_similarity: float = Query(72.0),
    saucenao_api_key: Optional[str] = Query(None),
    run_semantic_themes: bool = Query(False),
    run_decensor: bool = Query(False),
    decensor_save_mode: str = Query("new_copy"),
):
    return tagger_engine.start_batch_tagging(
        scope=scope,
        limit=limit,
        general_thresh=general_thresh,
        artist_thresh=artist_thresh,
        auto_move_to_artist_folder=auto_move_to_artist_folder,
        auto_unload=auto_unload,
        run_local_ai=run_local_ai,
        run_reverse_search_calibrate=run_reverse_search_calibrate,
        reverse_search_limit=reverse_search_limit,
        min_similarity=min_similarity,
        saucenao_api_key=saucenao_api_key,
        run_semantic_themes=run_semantic_themes,
        run_decensor=run_decensor,
        decensor_save_mode=decensor_save_mode,
    )


@app.post("/api/decensor/preview/{image_id}")
@exclusive_job("圖片修復預覽")
def api_decensor_preview(image_id: int, req: Optional[DecensorRequest] = None):
    from decensor_engine import decensor_engine
    r = req or DecensorRequest()
    return decensor_engine.preview_decensor_image(
        image_id=image_id,
        mode=r.mode,
        conf_thresh=r.conf_thresh,
        custom_boxes=r.custom_boxes,
    )


@app.post("/api/decensor/apply/{image_id}")
@exclusive_job("圖片修復儲存")
def api_decensor_apply(image_id: int, req: Optional[DecensorRequest] = None):
    from decensor_engine import decensor_engine
    r = req or DecensorRequest()
    res = decensor_engine.apply_and_save_decensor(
        image_id=image_id,
        save_mode=r.save_mode,
        mode=r.mode,
        conf_thresh=r.conf_thresh,
        custom_boxes=r.custom_boxes,
    )
    log_operation(
        category="decensor",
        action="AI 去馬賽克重繪儲存",
        summary=res.get("message", f"圖片 #{image_id} AI 去馬賽克處理完成 ({r.save_mode})"),
        status="success" if res.get("success") else "warning",
        affected_count=1 if res.get("success") else 0,
        details={"image_id": image_id, "save_mode": r.save_mode, "mode": r.mode, "result": res},
    )
    return res


@app.post("/api/ai/stop")
def api_ai_stop():
    return tagger_engine.stop_batch_tagging()


@app.post("/api/ai/load")
@exclusive_job("載入 AI 模型")
def api_ai_load():
    res = tagger_engine.load_model()
    log_operation(
        category="ai",
        action="載入 AI 視覺模型",
        summary=f"AI 視覺模型已載入至顯卡記憶體 ({res.get('provider', '')})",
        status="success",
        affected_count=1,
        details=res,
    )
    return res


@app.post("/api/ai/unload")
@exclusive_job("卸載 AI 模型")
def api_ai_unload(delete_disk_files: bool = Query(False)):
    res = tagger_engine.unload_model(delete_disk_files=delete_disk_files)
    log_operation(
        category="ai",
        action="卸載 AI 視覺模型",
        summary=res.get("message", "已卸載 AI 模型並釋放 VRAM"),
        status="success",
        affected_count=1,
        details=res,
    )
    return res


@app.get("/api/ai/status")
def api_ai_status():
    return tagger_engine.get_status()


class SingleFrameRequest(BaseModel):
    frame_data_url: Optional[str] = None


def _decode_frame_data_url(frame_data_url: Optional[str]) -> Optional[bytes]:
    if not frame_data_url or not isinstance(frame_data_url, str):
        return None
    import base64
    try:
        payload = frame_data_url.split(",", 1)[1] if "," in frame_data_url else frame_data_url
        return base64.b64decode(payload)
    except Exception:
        return None


@app.post("/api/ai/single/{image_id}")
def api_ai_single(image_id: int, req: Optional[SingleFrameRequest] = None):
    from vision_service import analyze
    ss_bytes = _decode_frame_data_url(req.frame_data_url) if req else None
    try:
        result = analyze(image_id, screenshot_bytes=ss_bytes)
    except FileNotFoundError as error:
        raise HTTPException(404,str(error)) from error
    except ValueError as error:
        raise HTTPException(400,str(error)) from error
    log_operation(category="ai",action="單張圖片 AI 分析",summary=f"完成 {result['filename']} 的視覺標籤",affected_count=1,details=result)
    return api_image_detail(image_id)


@app.post("/api/pixiv/lookup")
def api_pixiv_lookup(
    limit: int = Query(100),
    auto_move_to_artist_folder: bool = Query(False),
):
    return lookup_pixiv_artists_batch(limit=limit, auto_move_to_artist_folder=auto_move_to_artist_folder)


@app.post("/api/open_explorer/{image_id}")
def api_open_explorer(image_id: int):
    conn = get_db()
    try:
        row = conn.execute("SELECT rel_path FROM images WHERE id = ?", (image_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Image not found")
    path = gallery_path(GALLERY_ROOT, row["rel_path"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="圖片檔案不存在，可能已移動或刪除")
    full_path = str(path)
    try:
        reveal_file(path)
    except OSError as error:
        raise HTTPException(status_code=503, detail=f"無法開啟檔案總管：{error}") from error
    return {"status": "opened", "path": full_path}


# === Semantic Correlation & Gemini Flash / MCP Endpoints ===
from semantic_engine import (
    compute_tag_correlations,
    semantic_search_expand,
    apply_semantic_theme_tags_batch,
)



@app.get("/api/semantic/correlations")
def api_semantic_correlations(
    target_tag: str = Query(...),
    min_cooccurrence: int = Query(2, ge=1),
    top_k: int = Query(40, ge=5, le=100),
):
    return compute_tag_correlations(
        target_tag=target_tag,
        min_cooccurrence=min_cooccurrence,
        top_k=top_k,
    )


@app.get("/api/semantic/expand")
def api_semantic_expand(q: str = Query(...)):
    return semantic_search_expand(q)


@app.post("/api/semantic/apply_themes")
def api_semantic_apply_themes():
    from tag_policy import POLICY_MESSAGE
    raise HTTPException(410, POLICY_MESSAGE)


@app.post("/api/semantic/gemini_flash")
def api_semantic_gemini_flash():
    raise HTTPException(410, "已移除 Gemini API 直連；請由 Antigravity 透過圖庫 MCP 執行")


# === Reverse Image Search (SauceNAO + IQDB + Twitter/Pixiv) Endpoints ===
from reverse_search import reverse_search_and_tag_image, reverse_search_worker


@app.post("/api/reverse_search/single/{image_id}")
def api_reverse_search_single(
    image_id: int,
    min_similarity: float = Query(72.0),
    fallback_to_ai: bool = Query(False),
    req: Optional[SingleFrameRequest] = None,
):
    ss_bytes = _decode_frame_data_url(req.frame_data_url) if req else None
    search_info = reverse_search_and_tag_image(
        image_id=image_id,
        min_similarity=min_similarity,
        fallback_to_ai=fallback_to_ai,
        include_google_lens_url=True,
        screenshot_bytes=ss_bytes,
    )
    detail = api_image_detail(image_id)
    detail["reverse_search_result"] = search_info
    found = search_info.get("found", False)
    artists_str = ", ".join(search_info.get("artists", [])) if found else "未命中"
    log_operation(
        category="search_calibrate",
        action="單張圖片以圖搜圖校正",
        summary=f"對「{detail.get('filename', f'#{image_id}')}」執行以圖搜圖：{'命中畫師 ' + artists_str if found else '未命中認證畫師'}",
        status="success" if found else "warning",
        affected_count=1,
        details=search_info,
    )
    return detail


@app.post("/api/reverse_search/start")
def api_reverse_search_start(
    scope: str = Query("root_untagged"),
    limit: int = Query(50),
    min_similarity: float = Query(72.0),
    saucenao_api_key: Optional[str] = Query(None),
    fallback_to_ai: bool = Query(False),
    delay_sec: float = Query(1.8),
    auto_move_to_artist_folder: bool = Query(False),
):
    return reverse_search_worker.start(
        scope=scope,
        limit=limit,
        min_similarity=min_similarity,
        saucenao_api_key=saucenao_api_key,
        fallback_to_ai=fallback_to_ai,
        delay_sec=delay_sec,
        auto_move_to_artist_folder=auto_move_to_artist_folder,
    )


@app.post("/api/reverse_search/stop")
def api_reverse_search_stop():
    return reverse_search_worker.stop()


@app.get("/api/reverse_search/status")
def api_reverse_search_status():
    return reverse_search_worker.get_status()


@app.post("/api/reverse_search/purge_low_conf_ai")
def api_purge_low_conf_ai(
    max_ai_conf_to_purge: float = Query(0.65, ge=0.20, le=0.95),
    revert_unverified_ai_folders: bool = Query(True),
):
    from mcp_server import tool_gallery_purge_all_low_conf_ai_artists
    res = tool_gallery_purge_all_low_conf_ai_artists(
        max_ai_conf_to_purge=max_ai_conf_to_purge,
        revert_unverified_ai_folders=revert_unverified_ai_folders,
    )
    log_operation(
        category="search_calibrate",
        action="清除低信心度 AI 誤猜畫師",
        summary=f"已清除信心度 < {max_ai_conf_to_purge} 的 AI 誤猜畫師標籤共 {res.get('purged_ai_artist_links', 0)} 筆，還原 {res.get('reverted_images_to_root', 0)} 張未驗證資料夾圖片",
        status="success",
        affected_count=res.get("purged_ai_artist_links", 0) + res.get("reverted_images_to_root", 0),
        details=res,
    )
    return res


# === Auto-Ingest & AI Organize Scheduler Endpoints ===
from scheduler import (
    scheduler_engine,
    load_scheduler_config,
    save_scheduler_config,
    scan_source_directory_preview,
    browse_filesystem_directory,
    pick_folder_dialog,
    sync_windows_task_scheduler,
)


class ScheduleConfigUpdateRequest(BaseModel):
    enabled: bool = False
    schedule_times: List[str] = ["03:00"]
    source_dirs: List[str] = []
    remove_empty_subdirs: bool = True
    run_pixiv_lookup: bool = True
    run_reverse_search: bool = False
    reverse_search_limit: int = 30
    run_ai_tagger: bool = True
    general_thresh: float = 0.70
    artist_thresh: float = 0.70
    auto_create_artist_folders: bool = True
    min_artist_confidence: float = 0.70
    auto_rename_in_artist_folder: bool = True
    also_organize_all_root: bool = False
    sync_windows_task: bool = True


class SourcePreviewRequest(BaseModel):
    source_dirs: List[str] = []


@app.get("/api/schedule/status")
def api_schedule_status():
    scheduler_engine.start_daemon()
    return scheduler_engine.get_status()


@app.post("/api/schedule/config")
def api_schedule_save_config(req: ScheduleConfigUpdateRequest):
    for source in req.source_dirs:
        if source.strip():
            validate_source(GALLERY_ROOT,source)
    scheduler_engine.start_daemon()
    updated = save_scheduler_config({
        "enabled": req.enabled,
        "schedule_times": [t.strip() for t in req.schedule_times if t.strip()],
        "source_dirs": [p.strip() for p in req.source_dirs if p.strip()],
        "remove_empty_subdirs": req.remove_empty_subdirs,
        "run_pixiv_lookup": req.run_pixiv_lookup,
        "run_reverse_search": req.run_reverse_search,
        "reverse_search_limit": req.reverse_search_limit,
        "run_ai_tagger": req.run_ai_tagger,
        "general_thresh": req.general_thresh,
        "artist_thresh": req.artist_thresh,
        "auto_create_artist_folders": req.auto_create_artist_folders,
        "min_artist_confidence": req.min_artist_confidence,
        "auto_rename_in_artist_folder": req.auto_rename_in_artist_folder,
        "also_organize_all_root": req.also_organize_all_root,
    })
    win_task_info = None
    if req.sync_windows_task:
        win_task_info = sync_windows_task_scheduler(updated)
    log_operation(
        category="schedule",
        action="更新自動匯入排程設定",
        summary=f"排程已{'啟用' if req.enabled else '停用'} (時間: {', '.join(updated.get('schedule_times', [])) or '無'}，來源目錄: {len(updated.get('source_dirs', []))} 個)",
        status="success",
        affected_count=len(updated.get("source_dirs", [])),
        details={"enabled": req.enabled, "schedule_times": updated.get("schedule_times"), "source_dirs": updated.get("source_dirs")},
    )
    return {
        "status": "saved",
        "config": updated,
        "windows_task": win_task_info,
    }


@app.post("/api/schedule/preview_source")
def api_schedule_preview_source(req: SourcePreviewRequest):
    return scan_source_directory_preview(req.source_dirs)


@app.get("/api/schedule/browse_fs")
def api_schedule_browse_fs(path: str = Query("")):
    return browse_filesystem_directory(path)


@app.post("/api/schedule/pick_folder")
def api_schedule_pick_folder(initial_dir: str = Query("")):
    try:
        selected = pick_folder_dialog(initial_dir=initial_dir)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return {"selected_path": selected}


@app.post("/api/schedule/run_now")
def api_schedule_run_now():
    scheduler_engine.start_daemon()
    return scheduler_engine.trigger_pipeline_async(trigger_reason="手動立即執行")


# === Operation Activity Logs Endpoints ===
@app.get("/api/logs")
def api_get_logs(
    category: str = Query("all"),
    q: str = Query(""),
    limit: int = Query(120, ge=1, le=500),
):
    return get_operation_logs(category=category, q=q, limit=limit)


@app.post("/api/logs/clear")
def api_clear_logs():
    return clear_operation_logs()


@app.post("/api/logs/open_file")
def api_open_log_file():
    ACTIVITY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not ACTIVITY_LOG_FILE.exists():
        ACTIVITY_LOG_FILE.write_text("", encoding="utf-8")
    full_path = str(ACTIVITY_LOG_FILE.resolve()).replace("/", "\\")
    subprocess.Popen(["explorer.exe", f"/select,{full_path}"])
    return {"status": "opened", "path": full_path}
