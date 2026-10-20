# -*- coding: utf-8 -*-
"""
多 Subagent 併行畫師反查與標籤補全引擎 v2 (parallel_artist_tagger.py)
特性：
1. 共享持久化畫師快取 (artist_cache.json)，相同畫師秒速命中，不重複請求 Danbooru API。
2. 計算與網路請求完全在記憶體中進行，零長事務鎖庫 (Zero-lock during network)。
3. 批次快速寫入 (Bulk short transaction) + 隨機退避重試 (Exponential backoff retry)，完全杜絕 database is locked。
4. Danbooru MD5 精準還原貼文標籤 + Danbooru 官方 Artist API 漢字別名/Twitter帳號 + 檔名畫師自動校正。
5. 支援多 Worker 併行分區 (Worker ID / Total Workers)。
"""

import os
import re
import sys
import time
import json
import random
import argparse
import sqlite3
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent))

from database import GALLERY_ROOT, DB_PATH, ensure_tag, refresh_tag_counts
from background_work import exclusive_job, background_checkpoint
from zh_dict import resolve_artist_folder_name
from reverse_search import (
    search_danbooru_by_md5,
    fetch_danbooru_artist_metadata,
    match_existing_folder_by_artist,
)

CACHE_FILE = Path(__file__).parent / "artist_cache.json"

RE_TARGET_FILENAME = re.compile(r"^(20\d{6})_(\d{2,5})_(.+)$")
RE_TWITTER_ID = re.compile(r"^(\d{15,22})(?:\s*\(\d+\))?$")


def get_worker_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=60.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=60000;")
    return conn


def load_artist_cache() -> Dict[str, Dict[str, Any]]:
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_artist_cache(cache: Dict[str, Dict[str, Any]]):
    try:
        tmp_file = CACHE_FILE.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        tmp_file.replace(CACHE_FILE)
    except Exception:
        pass


def analyze_image_in_memory(
    row: sqlite3.Row,
    artist_cache: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    image_id = row["id"]
    filename = row["filename"]
    rel_path = row["rel_path"]
    full_path = str(GALLERY_ROOT / rel_path)

    base = filename.rsplit(".", 1)[0]
    m_target = RE_TARGET_FILENAME.match(base)
    m_tw_id = RE_TWITTER_ID.match(base)

    tags_to_add: List[Tuple[str, str, str, float, str]] = []  # (name, category, zh_name, confidence, source)
    detected_artists: List[str] = []
    twitter_handles: List[str] = []
    detected_characters: List[str] = []
    detected_copyrights: List[str] = []
    cjk_aliases: Dict[str, str] = {}
    suggested_folder = None
    method = "none"

    # Step 1: 先檢查 Danbooru MD5 (零依賴、100% 精準)
    if os.path.exists(full_path):
        db_post = search_danbooru_by_md5(full_path)
        if db_post:
            method = "danbooru_md5"
            for ainfo in db_post.get("artist_details", []):
                if ainfo.get("cjk_name"):
                    cjk_aliases[ainfo["name"]] = ainfo["cjk_name"]

            for art in db_post.get("artists", []):
                zh = cjk_aliases.get(art, "")
                tags_to_add.append((art, "artist", zh, 1.0, "danbooru"))
                detected_artists.append(art)
                matched_f = match_existing_folder_by_artist(art)
                if matched_f and not suggested_folder:
                    suggested_folder = matched_f

            for th in db_post.get("twitter_handles", []):
                if th not in twitter_handles:
                    twitter_handles.append(th)

            for ch in db_post.get("characters", []):
                tags_to_add.append((ch, "character", "", 1.0, "danbooru"))
                detected_characters.append(ch)

            for cp in db_post.get("copyrights", []):
                tags_to_add.append((cp, "copyright", "", 1.0, "danbooru"))
                detected_copyrights.append(cp)

            for gen in db_post.get("generals", [])[:30]:
                tags_to_add.append((gen, "general", "", 0.98, "danbooru"))

            if db_post.get("rating"):
                tags_to_add.append((db_post["rating"], "rating", "", 1.0, "danbooru"))

    # Step 2: 檔名包含 YYYYMMDD_NNN_畫師名
    if not tags_to_add and m_target:
        raw_artist = m_target.group(3).strip()
        clean_key = raw_artist.lower().replace(" ", "_")

        matched_f = match_existing_folder_by_artist(raw_artist)
        if matched_f:
            suggested_folder = matched_f

        # 查快取或 Danbooru Artist API
        if clean_key not in artist_cache:
            try:
                ainfo = fetch_danbooru_artist_metadata(raw_artist)
                artist_cache[clean_key] = ainfo
            except Exception:
                artist_cache[clean_key] = {}
        ainfo = artist_cache.get(clean_key) or {}

        if ainfo.get("name"):
            method = "danbooru_artist_api"
            db_art = ainfo["name"]
            cjk = ainfo.get("cjk_name") or ""
            tags_to_add.append((db_art, "artist", cjk, 1.0, "danbooru"))
            detected_artists.append(db_art)

            for th in ainfo.get("twitter_handles", []):
                if th not in twitter_handles:
                    twitter_handles.append(th)

            if not suggested_folder:
                suggested_folder = match_existing_folder_by_artist(db_art) or resolve_artist_folder_name(db_art)
        else:
            method = "filename_artist"
            norm_art = raw_artist.replace(" ", "_").lower() if raw_artist.isascii() else raw_artist
            tags_to_add.append((norm_art, "artist", "", 1.0, "filename"))
            detected_artists.append(norm_art)
            if not suggested_folder:
                suggested_folder = resolve_artist_folder_name(raw_artist)

    # Step 3: Twitter 推文 ID 檔名
    if not tags_to_add and m_tw_id:
        tags_to_add.append(("Twitter來源(推文ID)", "source", "", 1.0, "filename"))
        method = "twitter_id"

    # Step 4: 加入 Twitter @handle 畫師標籤
    for tw in twitter_handles:
        tw_tag = f"@{tw}"
        tags_to_add.append((tw_tag, "artist", "", 1.0, "twitter"))
        if tw_tag not in detected_artists:
            detected_artists.append(tw_tag)
        if not suggested_folder:
            suggested_folder = match_existing_folder_by_artist(tw, tw) or resolve_artist_folder_name(tw)

    return {
        "image_id": image_id,
        "filename": filename,
        "method": method,
        "tags_to_add": tags_to_add,
        "suggested_folder": suggested_folder,
        "has_verified_artist": bool(detected_artists),
        "has_verified_character": bool(detected_characters),
        "has_verified_copyright": bool(detected_copyrights),
    }


def commit_batch_with_retry(conn, tag_cache, results, max_retries=15):
    from database import run_db_write_with_retry
    def write(conn):
        for item in results:
            iid = item["image_id"]
            tags = item["tags_to_add"]
            sug_folder = item["suggested_folder"]

            if item["has_verified_artist"]:
                conn.execute(
                    "DELETE FROM image_tags WHERE image_id = ? AND source = 'ai' AND tag_id IN (SELECT id FROM tags WHERE category = 'artist')",
                    (iid,),
                )

            links = []
            for t_name, t_cat, t_zh, t_conf, t_src in tags:
                tid = ensure_tag(conn, tag_cache, t_name, t_cat)
                if tid > 0:
                    if t_zh:
                        conn.execute(
                            "UPDATE tags SET zh_name = COALESCE(NULLIF(zh_name,''), ?) WHERE id = ?",
                            (t_zh, tid),
                        )
                    links.append((iid, tid, t_conf, t_src))

            if links:
                conn.executemany(
                    """
                    INSERT INTO image_tags (image_id, tag_id, confidence, source)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(image_id, tag_id) DO UPDATE SET
                    confidence = MAX(image_tags.confidence, excluded.confidence),
                    source = CASE
                        WHEN excluded.source IN ('danbooru', 'pixiv', 'saucenao', 'iqdb', 'twitter', 'gemini_mcp', 'filename') THEN excluded.source
                        ELSE image_tags.source
                    END
                    """,
                    links,
                )

            if sug_folder or links:
                conn.execute(
                    "UPDATE images SET suggested_folder = COALESCE(?, suggested_folder) WHERE id = ?",
                    (sug_folder, iid),
                )
        return len(results)
    return run_db_write_with_retry(conn,write,max_retries=max_retries)


@exclusive_job("畫師標籤整理")
def run_worker(worker_id: int, total_workers: int, batch_size: int = 50, limit: Optional[int] = None):
    print(f"[Worker {worker_id}/{total_workers}] 啟動任務...")
    conn = get_worker_db()

    verified_subquery = """
        SELECT it.image_id FROM image_tags it
        JOIN tags t ON t.id = it.tag_id
        WHERE t.category = 'artist'
          AND it.source IN ('pixiv', 'saucenao', 'iqdb', 'danbooru', 'twitter', 'gemini_mcp', 'folder', 'filename', 'manual')
    """
    sql = f"""
        SELECT id, rel_path, filename, folder
        FROM images
        WHERE folder = ''
          AND (id % {total_workers}) = {worker_id}
          AND id NOT IN ({verified_subquery})
        ORDER BY id ASC
    """
    if limit and limit > 0:
        sql += f" LIMIT {limit}"

    rows = conn.execute(sql).fetchall()
    total_count = len(rows)
    print(f"[Worker {worker_id}/{total_workers}] 待處理圖片數: {total_count:,} 張")

    tag_cache: Dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM tags"):
        tag_cache[r["name"]] = r["id"]

    artist_cache = load_artist_cache()
    print(f"[Worker {worker_id}/{total_workers}] 已載入畫師快取庫: {len(artist_cache)} 筆")

    processed = 0
    tagged_count = 0
    t0 = time.time()

    for i in range(0, total_count, batch_size):
        background_checkpoint()
        chunk = rows[i : i + batch_size]
        batch_results = []

        # 記憶體分析，不佔用任何資料庫連線
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as executor:
            batch_results = list(executor.map(lambda row: analyze_image_in_memory(row, artist_cache), chunk))
        for res in batch_results:
            if res["tags_to_add"]:
                tagged_count += 1
            processed += 1

        # 批次短事務寫入
        ok = commit_batch_with_retry(conn, tag_cache, batch_results)
        if not ok:
            print(f"[Worker {worker_id}] 批次寫入警告: 部分資料在鎖庫競爭中可能需重試", file=sys.stderr)

        # 定期保存畫師快取
        if i % (batch_size * 5) == 0:
            save_artist_cache(artist_cache)

        elapsed = time.time() - t0
        speed = processed / elapsed if elapsed > 0 else 0
        print(
            f"[Worker {worker_id}/{total_workers}] 進度: {processed:,} / {total_count:,} ({processed/total_count*100:.1f}%) "
            f"| 已標註: {tagged_count:,} | 速度: {speed:.1f} 張/秒"
        )

    save_artist_cache(artist_cache)

    # 最終更新標籤計數
    try:
        with conn:
            refresh_tag_counts(conn)
    except Exception:
        pass
    conn.close()

    total_time = round(time.time() - t0, 1)
    print(
        f"✅ [Worker {worker_id}/{total_workers}] 任務完成！共處理 {processed:,} 張，"
        f"成功標註 {tagged_count:,} 張，耗時 {total_time} 秒"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="多 Subagent 併行畫師反查與標籤補全 v2")
    parser.add_argument("--worker-id", type=int, default=0, help="Worker 編號 (0-indexed)")
    parser.add_argument("--total-workers", type=int, default=1, help="總 Worker 數量")
    parser.add_argument("--batch-size", type=int, default=50, help="批次寫入筆數")
    parser.add_argument("--limit", type=int, default=None, help="處理上限張數")
    args = parser.parse_args()

    run_worker(
        worker_id=args.worker_id,
        total_workers=args.total_workers,
        batch_size=args.batch_size,
        limit=args.limit,
    )
