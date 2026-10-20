# -*- coding: utf-8 -*-
"""
超高速畫師快取反查與全庫標籤補全引擎 (fast_bulk_artist_tagger.py)
策略：
1. 預先彙整圖庫中所有唯一的畫師字串（約 1,600 位）。
2. 多執行緒並行預取 Danbooru 官方 Artist API (漢字別名、Twitter @handle、Pixiv ID) 並對齊現有繪師資料夾。
3. 萬張圖片在記憶體中毫秒級秒速配對，以 500 筆為單位快寫進 SQLite，耗時僅需數十秒。
4. 自動清除舊有本機 AI 亂猜的畫師標籤 (Tag Calibration)。
"""

import os
import re
import sys
import time
import json
import sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List, Optional, Tuple

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent))

from database import GALLERY_ROOT, DB_PATH, ensure_tag, refresh_tag_counts
from zh_dict import resolve_artist_folder_name
from background_work import exclusive_job, background_checkpoint
from reverse_search import (
    search_danbooru_by_md5,
    fetch_danbooru_artist_metadata,
    match_existing_folder_by_artist,
)

CACHE_FILE = Path(__file__).parent / "artist_cache.json"

RE_TARGET_FILENAME = re.compile(r"^(20\d{6})_(\d{2,5})_(.+)$")
RE_TWITTER_ID = re.compile(r"^(\d{15,22})(?:\s*\(\d+\))?$")


def get_db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=60.0)
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


def resolve_single_artist(raw_artist: str, artist_cache: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    clean_key = raw_artist.lower().replace(" ", "_")
    if clean_key in artist_cache and artist_cache[clean_key].get("resolved"):
        return artist_cache[clean_key]

    matched_f = match_existing_folder_by_artist(raw_artist)

    # 查 Danbooru 官方 API
    ainfo = fetch_danbooru_artist_metadata(raw_artist)
    db_name = ainfo.get("name") if ainfo else None
    cjk_name = ainfo.get("cjk_name") if ainfo else None
    twitter_handles = ainfo.get("twitter_handles") if ainfo else []

    suggested_folder = matched_f
    if not suggested_folder and db_name:
        suggested_folder = match_existing_folder_by_artist(db_name) or resolve_artist_folder_name(db_name)
    if not suggested_folder:
        suggested_folder = resolve_artist_folder_name(raw_artist)

    resolved_data = {
        "raw_artist": raw_artist,
        "clean_key": clean_key,
        "db_name": db_name,
        "cjk_name": cjk_name,
        "twitter_handles": twitter_handles,
        "suggested_folder": suggested_folder,
        "matched_existing_folder": bool(matched_f),
        "resolved": True,
    }
    artist_cache[clean_key] = resolved_data
    return resolved_data


@exclusive_job("批次畫師標籤整理")
def main():
    print("=" * 60)
    print("🚀 開始執行 Twitter 圖庫超高速畫師反查與標籤補全")
    print("=" * 60)

    conn = get_db_conn()

    # 1. 取得未具備已驗證畫師標籤的根目錄圖片
    verified_subquery = """
        SELECT it.image_id FROM image_tags it
        JOIN tags t ON t.id = it.tag_id
        WHERE t.category = 'artist'
          AND it.source IN ('pixiv', 'saucenao', 'iqdb', 'danbooru', 'twitter', 'gemini_mcp', 'folder', 'filename', 'manual')
    """
    rows = conn.execute(
        f"""
        SELECT id, rel_path, filename
        FROM images
        WHERE folder = ''
          AND id NOT IN ({verified_subquery})
        ORDER BY id ASC
        """
    ).fetchall()

    total_images = len(rows)
    print(f"[*] 待處理圖片總數: {total_images:,} 張")

    if total_images == 0:
        print("✅ 所有圖片皆已有認證畫師標籤，無需處理！")
        conn.close()
        return

    # 2. 提取所有唯一的畫師字串
    artist_cache = load_artist_cache()
    print(f"[*] 現有本機畫師快取: {len(artist_cache):,} 筆")

    unique_artists = set()
    for r in rows:
        base = r["filename"].rsplit(".", 1)[0]
        m = RE_TARGET_FILENAME.match(base)
        if m:
            unique_artists.add(m.group(3).strip())

    unresolved_artists = [a for a in unique_artists if a.lower().replace(" ", "_") not in artist_cache]
    print(f"[*] 涵蓋唯一畫師名稱: {len(unique_artists):,} 位 (其中待解析新畫師: {len(unresolved_artists):,} 位)")

    # 3. 多執行緒並行預取新畫師 metadata
    if unresolved_artists:
        print(f"[*] 正在並行查詢 Danbooru 官方 Artist API 與對齊繪師字典...")
        t_pre = time.time()
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_to_art = {
                executor.submit(resolve_single_artist, art, artist_cache): art
                for art in unresolved_artists
            }
            done_cnt = 0
            for fut in as_completed(future_to_art):
                try:
                    fut.result()
                except Exception as e:
                    pass
                done_cnt += 1
                if done_cnt % 200 == 0 or done_cnt == len(unresolved_artists):
                    print(f"    - 畫師解析進度: {done_cnt:,} / {len(unresolved_artists):,} ({done_cnt/len(unresolved_artists)*100:.1f}%)")

        save_artist_cache(artist_cache)
        print(f"[*] 畫師快取庫預熱完成！耗時 {time.time() - t_pre:.2f} 秒，目前快取總數: {len(artist_cache):,} 筆")

    # 4. 載入標籤快取
    tag_cache: Dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM tags"):
        tag_cache[r["name"]] = r["id"]

    # 5. 萬張圖片快速純記憶體組裝 + 批次短交易寫入
    print(f"[*] 開始批次寫入圖片標籤與建議資料夾 (500 筆/批次)...")
    t_start = time.time()

    batch_size = 500
    processed = 0
    tagged_count = 0
    purged_ai_total = 0

    for i in range(0, total_images, batch_size):
        background_checkpoint()
        chunk = rows[i : i + batch_size]
        chunk_img_ids = [r["id"] for r in chunk]
        placeholders = ",".join(["?"] * len(chunk_img_ids))

        with conn:
            cur_purge = conn.execute(
                f"""
                DELETE FROM image_tags
                WHERE image_id IN ({placeholders})
                  AND source = 'ai'
                  AND tag_id IN (SELECT id FROM tags WHERE category = 'artist')
                """,
                chunk_img_ids,
            )
            purged_ai_total += cur_purge.rowcount

            image_tag_links = []
            image_updates = []
            zh_updates = {}

            for r in chunk:
                iid = r["id"]
                fname = r["filename"]
                base = fname.rsplit(".", 1)[0]
                m_target = RE_TARGET_FILENAME.match(base)
                m_tw_id = RE_TWITTER_ID.match(base)

                sug_folder = None
                has_artist = False

                if m_target:
                    raw_art = m_target.group(3).strip()
                    clean_k = raw_art.lower().replace(" ", "_")
                    info = artist_cache.get(clean_k) or resolve_single_artist(raw_art, artist_cache)

                    sug_folder = info.get("suggested_folder")
                    db_name = info.get("db_name")
                    cjk_name = info.get("cjk_name")
                    tw_handles = info.get("twitter_handles") or []

                    if db_name:
                        tid_a = ensure_tag(conn, tag_cache, db_name, "artist")
                        if tid_a > 0:
                            image_tag_links.append((iid, tid_a, 1.0, "danbooru"))
                            has_artist = True
                            if cjk_name:
                                zh_updates[tid_a] = cjk_name
                    else:
                        norm_art = raw_art.replace(" ", "_").lower() if raw_art.isascii() else raw_art
                        tid_a = ensure_tag(conn, tag_cache, norm_art, "artist")
                        if tid_a > 0:
                            image_tag_links.append((iid, tid_a, 1.0, "filename"))
                            has_artist = True

                    for th in tw_handles:
                        tw_tag = f"@{th}"
                        tid_tw = ensure_tag(conn, tag_cache, tw_tag, "artist")
                        if tid_tw > 0:
                            image_tag_links.append((iid, tid_tw, 1.0, "twitter"))
                            has_artist = True

                elif m_tw_id:
                    tid_tw = ensure_tag(conn, tag_cache, "Twitter來源(推文ID)", "source")
                    image_tag_links.append((iid, tid_tw, 1.0, "filename"))

                if has_artist:
                    tagged_count += 1

                image_updates.append((sug_folder, iid))
                processed += 1

            # 寫入繁中/漢字別名
            for tid, zh_val in zh_updates.items():
                conn.execute(
                    "UPDATE tags SET zh_name = COALESCE(NULLIF(zh_name,''), ?) WHERE id = ?",
                    (zh_val, tid),
                )

            # 寫入圖片標籤關聯
            if image_tag_links:
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
                    image_tag_links,
                )

            # 更新圖片 suggested_folder（不偽造 ai_tagged = 1，保留給真正的視覺內容打標）
            conn.executemany(
                "UPDATE images SET suggested_folder = COALESCE(?, suggested_folder) WHERE id = ?",
                image_updates,
            )

        elapsed = time.time() - t_start
        speed = processed / elapsed if elapsed > 0 else 0
        pct = (processed / total_images) * 100
        print(f"[*] 進度: {processed:,} / {total_images:,} ({pct:.1f}%) | 已上標籤: {tagged_count:,} | 速度: {speed:,.1f} 張/秒")

    # 6. 重新整理資料庫標籤統計計數
    print("[*] 正在重新整理標籤統計資料庫計數 (refresh_tag_counts)...")
    with conn:
        refresh_tag_counts(conn)

    total_time = time.time() - t_start
    print("=" * 60)
    print(f"🎉 全部處理完成！")
    print(f"    - 處理圖片總數: {processed:,} 張")
    print(f"    - 成功建立/校正畫師標籤: {tagged_count:,} 張")
    print(f"    - 清除錯誤本機 AI 猜測: {purged_ai_total:,} 筆")
    print(f"    - 總耗時: {total_time:.2f} 秒 (平均每秒處理 {processed/total_time:,.1f} 張)")
    print("=" * 60)

    conn.close()


if __name__ == "__main__":
    main()
