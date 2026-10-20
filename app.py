# -*- coding: utf-8 -*-
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, HTMLResponse
from PIL import Image
from pydantic import BaseModel

from database import (
    GALLERY_ROOT,
    THUMB_DIR,
    NON_ARTIST_FOLDERS,
    RE_DATE_SEQ,
    get_db,
    init_db,
    scan_library,
    ensure_tag,
    refresh_tag_counts,
)
from ai_tagger import tagger_engine, preprocess_image_fast, lookup_pixiv_artists_batch
from zh_dict import get_tag_zh, resolve_artist_folder_name, DANBOORU_ARTIST_TO_FOLDER
from renamer import (
    win_natural_key,
    generate_rename_plan_for_folder,
    execute_rename_plan,
    list_rename_logs,
    undo_rename_batch,
    get_next_sequence_filename_for_folder,
)

app = FastAPI(title="Twitter & Pixiv AI 智慧標籤圖庫系統")

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


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
    pad_digits: int = 4
    custom_date: Optional[str] = None
    custom_artist: Optional[str] = None


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
    for r in rows:
        iid = r["id"]
        tname = r["name"]
        folder_name = resolve_artist_folder_name(tname)
        if not folder_name:
            continue
        if iid not in best_by_img:
            best_by_img[iid] = folder_name
        else:
            if tname.lower() in DANBOORU_ARTIST_TO_FOLDER:
                best_by_img[iid] = DANBOORU_ARTIST_TO_FOLDER[tname.lower()]

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
    conn = get_db()
    cnt = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    conn.close()
    if cnt == 0:
        print("初次啟動，正在建立 D:\\圖檔\\twitter 索引資料庫...")
        res = scan_library()
        print(f"索引完成：{res}")
    sync_root_suggested_folders()
    from scheduler import scheduler_engine
    scheduler_engine.start_daemon()




@app.get("/", response_class=HTMLResponse)
def index():
    html_file = STATIC_DIR / "index.html"
    if not html_file.exists():
        return HTMLResponse("<h1>UI loading...</h1>")
    return HTMLResponse(html_file.read_text(encoding="utf-8"))


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


@app.post("/api/scan")
def api_scan():
    return scan_library()


@app.get("/api/folders")
def api_folders():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT folder, COUNT(*) as count
        FROM images
        WHERE folder != ''
        GROUP BY folder
        ORDER BY count DESC, folder ASC
        """
    ).fetchall()
    conn.close()
    return [{"name": r["folder"], "count": r["count"]} for r in rows]


@app.get("/api/tags")
def api_tags(
    category: str = Query("all"),
    q: str = Query(""),
    limit: int = Query(250),
):
    import time as _time
    conn = get_db()
    total_all = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]

    now_ts = _time.time()
    recent_special_items = []
    if category in ("all", "recent"):
        q_lower = q.strip().lower()
        if not q_lower or "最近" in q_lower or "recent" in q_lower or "最新" in q_lower:
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
            recent_tag_rows = conn.execute(
                """
                SELECT t.id, t.name, t.category, t.zh_name, t.post_count, MAX(i.mtime) as latest_mtime
                FROM tags t
                JOIN image_tags it ON t.id = it.tag_id
                JOIN images i ON i.id = it.image_id
                WHERE t.post_count > 0
                GROUP BY t.id
                ORDER BY latest_mtime DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            conn.close()
            for r in recent_tag_rows:
                zh = r["zh_name"] or get_tag_zh(r["name"], r["category"])
                recent_special_items.append({
                    "id": r["id"],
                    "name": r["name"],
                    "category": r["category"],
                    "zh_name": zh,
                    "count": r["post_count"],
                })
            return recent_special_items

    params = []
    where = ["post_count > 0"]

    if category and category != "all":
        where.append("category = ?")
        params.append(category)

    if q.strip():
        q_like = f"%{q.strip()}%"
        where.append("(name LIKE ? OR zh_name LIKE ?)")
        params.extend([q_like, q_like])

    sql = f"""
        SELECT id, name, category, zh_name, post_count
        FROM tags
        WHERE {' AND '.join(where)}
        ORDER BY post_count DESC, name ASC
        LIMIT ?
    """
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    result = list(recent_special_items)
    for r in rows:
        zh = r["zh_name"] or get_tag_zh(r["name"], r["category"])
        result.append({
            "id": r["id"],
            "name": r["name"],
            "category": r["category"],
            "zh_name": zh,
            "count": r["post_count"],
        })
    return result


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
):
    import datetime as _dt
    import time as _time
    conn = get_db()
    where = ["1=1"]
    params: List[Any] = []

    if folder == "__ROOT__":
        where.append("i.folder = ''")
    elif folder == "__FOLDERS__":
        where.append("i.folder != ''")
    elif folder == "__SUGGESTED__":
        where.append("i.folder = '' AND i.suggested_folder IS NOT NULL AND i.suggested_folder != ''")
    elif folder != "__ALL__":
        where.append("i.folder = ?")
        params.append(folder)

    if media_type == "gif_video":
        where.append("i.media_type IN ('gif', 'video')")
    elif media_type in ("image", "gif", "video"):
        where.append("i.media_type = ?")
        params.append(media_type)

    if ai_status == "tagged":
        where.append("i.ai_tagged = 1")
    elif ai_status == "untagged":
        where.append("i.ai_tagged = 0")

    if q.strip():
        q_clean = f"%{q.strip()}%"
        where.append("""
            (i.filename LIKE ? OR i.folder LIKE ? OR EXISTS (
                SELECT 1 FROM image_tags it
                JOIN tags t ON t.id = it.tag_id
                WHERE it.image_id = i.id AND (t.name LIKE ? OR t.zh_name LIKE ?)
            ))
        """)
        params.extend([q_clean, q_clean, q_clean, q_clean])

    now_ts = _time.time()
    recent_bucket_days = {
        "最近:24小時內": 1,
        "最近:7天內": 7,
        "最近:30天內": 30,
        "最近:90天內": 90,
        "最近:1年內": 365,
    }

    inc_list = [t.strip() for t in include_tags.split(",") if t.strip()]
    force_mtime_desc = False
    for tname in inc_list:
        if tname == "最近":
            # 「最近」代表包含所有圖片，並強制使用編輯時間 (mtime) 由最新排到最舊
            force_mtime_desc = True
            continue
        elif tname in recent_bucket_days:
            force_mtime_desc = True
            days = recent_bucket_days[tname]
            where.append("i.mtime >= ?")
            params.append(now_ts - 86400 * days)
            continue

        where.append("""
            EXISTS (
                SELECT 1 FROM image_tags it
                JOIN tags t ON t.id = it.tag_id
                WHERE it.image_id = i.id AND t.name = ?
            )
        """)
        params.append(tname)

    if force_mtime_desc:
        sort = "mtime_desc"

    exc_list = [t.strip() for t in exclude_tags.split(",") if t.strip()]
    for tname in exc_list:
        if tname == "最近" or tname in recent_bucket_days:
            continue
        where.append("""
            NOT EXISTS (
                SELECT 1 FROM image_tags it
                JOIN tags t ON t.id = it.tag_id
                WHERE it.image_id = i.id AND t.name = ?
            )
        """)
        params.append(tname)

    where_sql = " AND ".join(where)
    offset = (page - 1) * page_size

    if sort in ("natural_asc", "name_asc"):
        # Fetch matching rows and sort via Windows native StrCmpLogicalW so _2 is always before _10!
        all_rows = conn.execute(
            f"""
            SELECT i.id, i.rel_path, i.filename, i.folder, i.ext, i.size_bytes,
                   i.mtime, i.media_type, i.pixiv_id, i.ai_tagged, i.suggested_folder
            FROM images i
            WHERE {where_sql}
            """,
            params,
        ).fetchall()
        total_matches = len(all_rows)
        sorted_all = sorted(all_rows, key=lambda r: win_natural_key(r["rel_path"]))
        rows = sorted_all[offset : offset + page_size]
    else:
        order_by = "i.mtime DESC, i.id DESC"
        if sort == "mtime_asc":
            order_by = "i.mtime ASC, i.id ASC"
        elif sort == "size_desc":
            order_by = "i.size_bytes DESC"

        count_sql = f"SELECT COUNT(*) FROM images i WHERE {where_sql}"
        total_matches = conn.execute(count_sql, params).fetchone()[0]

        data_sql = f"""
            SELECT i.id, i.rel_path, i.filename, i.folder, i.ext, i.size_bytes,
                   i.mtime, i.media_type, i.pixiv_id, i.ai_tagged, i.suggested_folder
            FROM images i
            WHERE {where_sql}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(data_sql, params + [page_size, offset]).fetchall()


    img_ids = [r["id"] for r in rows]
    tags_by_img: Dict[int, List[Dict[str, Any]]] = {iid: [] for iid in img_ids}

    if img_ids:
        placeholders = ",".join(["?"] * len(img_ids))
        tag_rows = conn.execute(
            f"""
            SELECT it.image_id, t.name, t.category, t.zh_name, it.confidence, it.source
            FROM image_tags it
            JOIN tags t ON t.id = it.tag_id
            WHERE it.image_id IN ({placeholders})
            ORDER BY
                CASE t.category
                    WHEN 'artist' THEN 1
                    WHEN 'character' THEN 2
                    WHEN 'copyright' THEN 3
                    WHEN 'rating' THEN 4
                    ELSE 5
                END,
                it.confidence DESC
            """,
            img_ids,
        ).fetchall()

        for tr in tag_rows:
            zh = tr["zh_name"] or get_tag_zh(tr["name"], tr["category"])
            tags_by_img[tr["image_id"]].append({
                "name": tr["name"],
                "category": tr["category"],
                "zh_name": zh,
                "confidence": tr["confidence"],
                "source": tr["source"],
            })

    conn.close()

    items = []
    for r in rows:
        iid = r["id"]
        all_t = tags_by_img.get(iid, [])
        artists = [t for t in all_t if t["category"] == "artist"]
        chars = [t for t in all_t if t["category"] == "character"]
        copyrights = [t for t in all_t if t["category"] == "copyright"]
        ratings = [t for t in all_t if t["category"] == "rating"]
        generals = [t for t in all_t if t["category"] == "general"][:6]

        items.append({
            "id": iid,
            "rel_path": r["rel_path"],
            "filename": r["filename"],
            "folder": r["folder"],
            "ext": r["ext"],
            "size_kb": round(r["size_bytes"] / 1024, 1),
            "mtime": r["mtime"],
            "media_type": r["media_type"],
            "pixiv_id": r["pixiv_id"],
            "ai_tagged": bool(r["ai_tagged"]),
            "suggested_folder": r["suggested_folder"],
            "artists": artists,
            "characters": chars,
            "copyrights": copyrights,
            "rating": ratings[0]["name"] if ratings else None,
            "preview_tags": generals,
            "total_tags": len(all_t),
        })

    return {
        "total": total_matches,
        "page": page,
        "page_size": page_size,
        "items": items,
    }


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
        zh = tr["zh_name"] or get_tag_zh(tr["name"], tr["category"])
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
        "folder": row["folder"],
        "size_kb": round(row["size_bytes"] / 1024, 1),
        "media_type": row["media_type"],
        "pixiv_id": row["pixiv_id"],
        "ai_tagged": bool(row["ai_tagged"]),
        "suggested_folder": row["suggested_folder"],
        "tags": tags,
    }


@app.get("/api/thumb/{image_id}")
def api_thumb(image_id: int):
    thumb_file = THUMB_DIR / f"{image_id}.jpg"
    if thumb_file.exists():
        return FileResponse(str(thumb_file), media_type="image/jpeg")

    conn = get_db()
    row = conn.execute("SELECT rel_path, media_type FROM images WHERE id = ?", (image_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Image not found")

    full_path = GALLERY_ROOT / row["rel_path"]
    if not full_path.exists():
        raise HTTPException(status_code=404, detail="File missing on disk")

    if row["media_type"] == "video":
        svg = """<svg xmlns="http://www.w3.org/2000/svg" width="320" height="320" viewBox="0 0 320 320">
        <rect width="320" height="320" fill="#1e1e2e"/>
        <circle cx="160" cy="160" r="48" fill="#6366f1" opacity="0.9"/>
        <polygon points="145,135 145,185 188,160" fill="#ffffff"/>
        <text x="160" y="245" text-anchor="middle" fill="#a1a1aa" font-family="sans-serif" font-size="16">MP4 影片 (點擊播放)</text>
        </svg>"""
        return Response(content=svg, media_type="image/svg+xml")

    try:
        with Image.open(str(full_path)) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.thumbnail((380, 380), Image.Resampling.BILINEAR)
            img.save(str(thumb_file), "JPEG", quality=82)
        return FileResponse(str(thumb_file), media_type="image/jpeg")
    except Exception:
        return FileResponse(str(full_path))


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
def api_batch_tags(req: BatchTagRequest):
    if not req.image_ids:
        return {"updated": 0}

    conn = get_db()
    tag_cache: Dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM tags"):
        tag_cache[r["name"]] = r["id"]

    with conn:
        links = []
        for t_info in req.add_tags:
            tname = t_info.get("name", "").strip()
            tcat = t_info.get("category", "general").strip() or "general"
            if not tname:
                continue
            tid = ensure_tag(conn, tag_cache, tname, tcat)
            if tid > 0:
                for iid in req.image_ids:
                    links.append((iid, tid, 1.0, "manual"))

        if links:
            conn.executemany(
                """
                INSERT INTO image_tags (image_id, tag_id, confidence, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(image_id, tag_id) DO UPDATE SET
                confidence = 1.0, source = 'manual'
                """,
                links,
            )

        for rname in req.remove_tag_names:
            row_t = conn.execute("SELECT id FROM tags WHERE name = ?", (rname.strip(),)).fetchone()
            if row_t:
                tid = row_t["id"]
                placeholders = ",".join(["?"] * len(req.image_ids))
                conn.execute(
                    f"DELETE FROM image_tags WHERE tag_id = ? AND image_id IN ({placeholders})",
                    [tid] + req.image_ids,
                )

        refresh_tag_counts(conn)

    conn.close()

    moved_count = 0
    if req.move_to_artist_folder and req.move_to_artist_folder.strip():
        move_res = move_images_to_folder(req.image_ids, req.move_to_artist_folder.strip(), auto_rename=True)
        moved_count = move_res.get("moved", 0)

    return {"updated": len(req.image_ids), "moved": moved_count}


def _safe_move_file(src_full: Path, target_dir: Path, fname: str) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    dest_full = target_dir / fname
    if dest_full.exists() and dest_full.resolve() != src_full.resolve():
        stem = dest_full.stem
        ext = dest_full.suffix
        counter = 1
        while dest_full.exists():
            dest_full = target_dir / f"{stem}_{counter}{ext}"
            counter += 1
    if src_full.resolve() != dest_full.resolve():
        shutil.move(str(src_full), str(dest_full))
    return dest_full


def move_images_to_folder(image_ids: List[int], target_folder: str, auto_rename: bool = True) -> Dict[str, Any]:
    clean_folder = re.sub(r'[<>:"/\\|?*]', "_", target_folder.strip())
    if not clean_folder or clean_folder == "__ROOT__":
        target_dir = GALLERY_ROOT
        clean_folder = ""
    else:
        target_dir = GALLERY_ROOT / clean_folder

    conn = get_db()
    tag_cache: Dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM tags"):
        tag_cache[r["name"]] = r["id"]

    placeholders = ",".join(["?"] * len(image_ids))
    rows = conn.execute(
        f"SELECT id, rel_path, filename, folder, ext FROM images WHERE id IN ({placeholders})",
        image_ids,
    ).fetchall()

    # Preserve Windows natural sort order of the moved batch!
    sorted_rows = sorted(rows, key=lambda r: win_natural_key(r["rel_path"]))

    moved = 0
    for r in sorted_rows:
        src_full = GALLERY_ROOT / r["rel_path"]
        if not src_full.exists():
            continue
        try:
            if auto_rename and clean_folder and clean_folder not in NON_ARTIST_FOLDERS:
                target_fname = get_next_sequence_filename_for_folder(clean_folder, r["ext"], pad_digits=4)
            else:
                target_fname = r["filename"]

            dest_full = _safe_move_file(src_full, target_dir, target_fname)
            new_rel = str(dest_full.relative_to(GALLERY_ROOT)).replace("\\", "/")
            with conn:
                conn.execute(
                    "UPDATE images SET rel_path = ?, filename = ?, folder = ?, suggested_folder = NULL WHERE id = ?",
                    (new_rel, dest_full.name, clean_folder, r["id"]),
                )
                if clean_folder and clean_folder not in NON_ARTIST_FOLDERS:
                    tid = ensure_tag(conn, tag_cache, clean_folder, "artist")
                    conn.execute(
                        "INSERT OR IGNORE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, 1.0, 'folder')",
                        (r["id"], tid),
                    )
            moved += 1
        except Exception:
            continue

    with conn:
        refresh_tag_counts(conn)
    conn.close()
    return {"moved": moved, "target_folder": clean_folder}


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
        "SELECT COUNT(*) FROM images WHERE folder = '' AND ai_tagged = 0"
    ).fetchone()[0]
    unqueried_pixiv_root_count = conn.execute(
        """
        SELECT COUNT(*) FROM images
        WHERE folder = '' AND pixiv_id IS NOT NULL
          AND id NOT IN (SELECT image_id FROM image_tags WHERE source = 'pixiv')
        """
    ).fetchone()[0]
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
        "total_artist_folders": len(folder_items),
        "new_folders_to_create": new_folders_count,
        "existing_folders_to_update": existing_folders_count,
        "untagged_root_remaining": untagged_root_count,
        "unqueried_pixiv_remaining": unqueried_pixiv_root_count,
        "folders": folder_items,
    }


@app.post("/api/organize/move")
def api_organize_move(req: MoveFilesRequest):
    if req.mode == "selected":
        if not req.image_ids:
            raise HTTPException(status_code=400, detail="未選取任何圖片")
        return move_images_to_folder(req.image_ids, req.target_folder, auto_rename=req.auto_rename_on_move)

    elif req.mode in ("ai_suggested", "auto_artist_folders"):
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
            res = move_images_to_folder(ids, folder_name, auto_rename=req.auto_rename_on_move)
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
        return {
            "moved": total_moved,
            "folders_updated": len(moved_details),
            "new_folders_created": new_created,
            "existing_folders_updated": existing_updated,
            "details": moved_details,
        }

    elif req.mode == "by_date_ym":
        conn = get_db()
        rows = conn.execute("SELECT id, rel_path, filename FROM images WHERE folder = ''").fetchall()
        moved = 0
        with conn:
            for r in rows:
                base = os.path.splitext(r["filename"])[0]
                m = RE_DATE_SEQ.match(base)
                if not m:
                    continue
                yr, mo = m.group(1), m.group(2)
                target_dir = GALLERY_ROOT / "_按日期歸檔" / f"{yr}-{mo}"
                src_full = GALLERY_ROOT / r["rel_path"]
                if not src_full.exists():
                    continue
                try:
                    dest_full = _safe_move_file(src_full, target_dir, r["filename"])
                    new_rel = str(dest_full.relative_to(GALLERY_ROOT)).replace("\\", "/")
                    conn.execute(
                        "UPDATE images SET rel_path = ?, filename = ?, folder = ? WHERE id = ?",
                        (new_rel, dest_full.name, "_按日期歸檔", r["id"]),
                    )
                    moved += 1
                except Exception:
                    continue
        conn.close()
        return {"moved": moved, "target_folder": "_按日期歸檔"}

    raise HTTPException(status_code=400, detail="Unknown organize mode")



# === Rename Endpoints ===
@app.get("/api/rename/preview")
def api_rename_preview(
    folder: str = Query(...),
    pad_digits: int = Query(4, ge=3, le=6),
    custom_date: Optional[str] = Query(None),
    custom_artist: Optional[str] = Query(None),
):
    if folder == "__ALL_ARTIST_FOLDERS__":
        conn = get_db()
        f_rows = conn.execute(
            "SELECT DISTINCT folder FROM images WHERE folder != '' ORDER BY folder ASC"
        ).fetchall()
        conn.close()
        summary_items = []
        total_files = 0
        total_changed = 0
        for fr in f_rows:
            fname = fr["folder"]
            if fname in NON_ARTIST_FOLDERS:
                continue
            plan = generate_rename_plan_for_folder(fname, pad_digits=pad_digits)
            total_files += plan["total"]
            total_changed += plan["changed_count"]
            if plan["items"]:
                first_it = plan["items"][0]
                last_it = plan["items"][-1]
                summary_items.append({
                    "folder": fname,
                    "base_date": plan["base_date"],
                    "total": plan["total"],
                    "changed_count": plan["changed_count"],
                    "sample_first_old": first_it["old_filename"],
                    "sample_first_new": first_it["new_filename"],
                    "sample_last_old": last_it["old_filename"],
                    "sample_last_new": last_it["new_filename"],
                })
        return {
            "mode": "all_folders",
            "folder_count": len(summary_items),
            "total": total_files,
            "changed_count": total_changed,
            "folders": summary_items,
        }
    else:
        plan = generate_rename_plan_for_folder(
            folder_name=folder,
            pad_digits=pad_digits,
            custom_date=custom_date,
            custom_artist=custom_artist,
        )
        # Return up to 300 preview rows so browser stays snappy
        return {
            "mode": "single_folder",
            "folder": plan["folder"],
            "artist_name": plan.get("artist_name", ""),
            "base_date": plan.get("base_date", ""),
            "total": plan["total"],
            "changed_count": plan["changed_count"],
            "items": plan["items"][:300],
        }


@app.post("/api/rename/execute")
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
    run_semantic_themes: bool = Query(True),
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
def api_decensor_apply(image_id: int, req: Optional[DecensorRequest] = None):
    from decensor_engine import decensor_engine
    r = req or DecensorRequest()
    return decensor_engine.apply_and_save_decensor(
        image_id=image_id,
        save_mode=r.save_mode,
        mode=r.mode,
        conf_thresh=r.conf_thresh,
        custom_boxes=r.custom_boxes,
    )


@app.post("/api/ai/stop")
def api_ai_stop():
    return tagger_engine.stop_batch_tagging()


@app.post("/api/ai/load")
def api_ai_load():
    return tagger_engine.load_model()


@app.post("/api/ai/unload")
def api_ai_unload(delete_disk_files: bool = Query(False)):
    return tagger_engine.unload_model(delete_disk_files=delete_disk_files)


@app.get("/api/ai/status")
def api_ai_status():
    return tagger_engine.get_status()


@app.post("/api/ai/single/{image_id}")
def api_ai_single(image_id: int):
    conn = get_db()
    row = conn.execute("SELECT id, rel_path FROM images WHERE id = ?", (image_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Image not found")

    full_path = str(GALLERY_ROOT / row["rel_path"])
    tagger_engine.ensure_model_loaded()
    tensor = preprocess_image_fast(full_path, 512)
    if tensor is None:
        conn.close()
        raise HTTPException(status_code=400, detail="無法讀取該圖片進行 AI 分析")

    preds, sug_folder = tagger_engine.predict_single(tensor)
    tag_cache: Dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM tags"):
        tag_cache[r["name"]] = r["id"]

    from zh_dict import DANBOORU_ARTIST_TO_FOLDER
    links = []
    for tname, cat, conf in preds:
        tid = ensure_tag(conn, tag_cache, tname, cat)
        if tid > 0:
            links.append((image_id, tid, round(conf, 4), "ai"))
            if cat == "artist" and tname.lower() in DANBOORU_ARTIST_TO_FOLDER:
                fa = DANBOORU_ARTIST_TO_FOLDER[tname.lower()]
                tid_fa = ensure_tag(conn, tag_cache, fa, "artist")
                if tid_fa > 0:
                    links.append((image_id, tid_fa, round(conf, 4), "ai"))

    with conn:
        if links:
            conn.executemany(
                """
                INSERT INTO image_tags (image_id, tag_id, confidence, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(image_id, tag_id) DO UPDATE SET
                confidence = MAX(image_tags.confidence, excluded.confidence)
                """,
                links,
            )
        conn.execute(
            "UPDATE images SET ai_tagged = 1, suggested_folder = COALESCE(?, suggested_folder) WHERE id = ?",
            (sug_folder, image_id),
        )
        refresh_tag_counts(conn)
    conn.close()
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
    row = conn.execute("SELECT rel_path FROM images WHERE id = ?", (image_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Image not found")
    full_path = str(GALLERY_ROOT / row["rel_path"]).replace("/", "\\")
    subprocess.Popen(["explorer.exe", f"/select,{full_path}"])
    return {"status": "opened", "path": full_path}


# === Semantic Correlation & Gemini Flash / MCP Endpoints ===
from semantic_engine import (
    compute_tag_correlations,
    semantic_search_expand,
    apply_semantic_theme_tags_batch,
    call_gemini_flash_for_tags,
)


class GeminiFlashRequest(BaseModel):
    prompt_task: str
    api_key: Optional[str] = None


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
    return apply_semantic_theme_tags_batch()


@app.post("/api/semantic/gemini_flash")
def api_semantic_gemini_flash(req: GeminiFlashRequest):
    return call_gemini_flash_for_tags(req.prompt_task, req.api_key)


# === Reverse Image Search (SauceNAO + IQDB + Twitter/Pixiv) Endpoints ===
from reverse_search import reverse_search_and_tag_image, reverse_search_worker


@app.post("/api/reverse_search/single/{image_id}")
def api_reverse_search_single(
    image_id: int,
    min_similarity: float = Query(72.0),
    fallback_to_ai: bool = Query(False),
):
    search_info = reverse_search_and_tag_image(
        image_id=image_id,
        min_similarity=min_similarity,
        fallback_to_ai=fallback_to_ai,
        include_google_lens_url=True,
    )
    detail = api_image_detail(image_id)
    detail["reverse_search_result"] = search_info
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
    return tool_gallery_purge_all_low_conf_ai_artists(
        max_ai_conf_to_purge=max_ai_conf_to_purge,
        revert_unverified_ai_folders=revert_unverified_ai_folders,
    )


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
    selected = pick_folder_dialog(initial_dir=initial_dir)
    return {"selected_path": selected}


@app.post("/api/schedule/run_now")
def api_schedule_run_now():
    scheduler_engine.start_daemon()
    return scheduler_engine.trigger_pipeline_async(trigger_reason="手動立即執行")



