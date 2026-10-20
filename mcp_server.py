# -*- coding: utf-8 -*-
"""
Antigravity MCP Server for Twitter/Pixiv AI Tag Gallery (mcp_server.py)
透過 Model Context Protocol (Stdio) 將 D:\\圖檔\\twitter 圖庫暴露給 Antigravity (及 Gemini Flash 3.8 模型)：
提供工具：
1. gallery_get_overview: 取得圖庫統計與熱門標籤概況
2. gallery_find_tag_correlations: 計算並搜尋任意標籤/畫師的共現相關性 (Jaccard / 條件機率)
3. gallery_semantic_search: 透過多重關鍵字或語意概念搜尋相關標籤與圖片
4. gallery_list_untranslated_tags: 列出尚未有繁體中文翻譯的高頻英文 Danbooru 標籤，供 Gemini Flash 批次翻譯與歸類
5. gallery_apply_gemini_enrichments: 將 Gemini Flash 分析出的「繁體中文翻譯」、「高階語意主題群組」或「畫師關聯標籤」批次寫入圖庫 SQLite
6. gallery_batch_tag_or_move: 依條件批次為圖片加上標籤或歸檔至繪師資料夾
7. gallery_list_candidates_for_artist_calibration: 列出僅有本機 AI 低信心度畫師猜測或待驗證畫師的圖片清單 (含檔案絕對路徑供 Gemini Flash 3.8 視覺檢視)
8. gallery_deep_reverse_search_batch: 對指定圖片批次執行「Google Lens 以圖搜圖網址生成 + Danbooru MD5/Post/Artist API + IQDB + SauceNAO 深度反查」，自動回傳認證畫師、Twitter @帳號、角色、作品與 Google 搜尋建議關鍵字
9. gallery_calibrate_image_artist_and_tags: 供 Gemini Flash 3.8 在完成 Google 以圖搜圖/網路搜尋與視覺比對後，將認證後的真實畫師、Twitter @帳號、角色、作品與校正後標籤寫回圖庫，自動清除錯誤 AI 猜測並搬移至正確繪師資料夾
10. gallery_purge_all_low_conf_ai_artists: 批次清除資料庫中本機 AI 低信心度 (< 0.65) 亂猜的畫師標籤，並還原僅因低信心度 AI 猜測而誤建的資料夾
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

sys.path.insert(0, str(Path(__file__).parent))

from database import GALLERY_ROOT, NON_ARTIST_FOLDERS, get_db, init_db, ensure_tag, refresh_tag_counts, scan_library
from semantic_engine import (
    compute_tag_correlations,
    semantic_search_expand,
    apply_semantic_theme_tags_batch,
)
from zh_dict import resolve_artist_folder_name
from reverse_search import (
    reverse_search_and_tag_image,
    calibrate_image_tags_in_db,
    make_search_thumbnail,
    generate_google_lens_url,
    fetch_danbooru_artist_metadata,
)
from app import move_images_to_folder

init_db()


def tool_gallery_get_overview() -> Dict[str, Any]:
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    root_cnt = conn.execute("SELECT COUNT(*) FROM images WHERE folder = ''").fetchone()[0]
    ai_cnt = conn.execute("SELECT COUNT(*) FROM images WHERE ai_tagged = 1").fetchone()[0]

    top_artists = [
        dict(r) for r in conn.execute(
            "SELECT name, zh_name, post_count FROM tags WHERE category = 'artist' AND post_count > 0 ORDER BY post_count DESC LIMIT 30"
        ).fetchall()
    ]
    top_general = [
        dict(r) for r in conn.execute(
            "SELECT name, zh_name, post_count FROM tags WHERE category != 'artist' AND post_count > 0 ORDER BY post_count DESC LIMIT 40"
        ).fetchall()
    ]
    conn.close()
    return {
        "total_images": total,
        "root_unorganized": root_cnt,
        "folder_organized": total - root_cnt,
        "ai_tagged": ai_cnt,
        "top_artists": top_artists,
        "top_tags": top_general,
    }


def tool_gallery_find_tag_correlations(
    target_tag: str,
    min_cooccurrence: int = 2,
    top_k: int = 40,
) -> Dict[str, Any]:
    return compute_tag_correlations(
        target_tag=target_tag,
        min_cooccurrence=min_cooccurrence,
        top_k=top_k,
    )


def tool_gallery_semantic_search(query: str) -> Dict[str, Any]:
    return semantic_search_expand(query)


def tool_gallery_list_untranslated_tags(
    category: str = "all",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    conn = get_db()
    where = "post_count > 0 AND (zh_name IS NULL OR zh_name = '')"
    params: List[Any] = []
    if category and category != "all":
        where += " AND category = ?"
        params.append(category)

    rows = conn.execute(
        f"SELECT id, name, category, post_count FROM tags WHERE {where} ORDER BY post_count DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def tool_gallery_apply_gemini_enrichments(
    translations: Optional[Dict[str, str]] = None,
    semantic_theme_groups: Optional[List[Dict[str, Any]]] = None,
    auto_cluster_builtin_themes: bool = True,
) -> Dict[str, Any]:
    conn = get_db()
    tag_cache: Dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM tags"):
        tag_cache[r["name"]] = r["id"]

    tr_count = 0
    theme_img_links = 0

    with conn:
        if translations:
            for eng_name, zh_val in translations.items():
                cur = conn.execute(
                    "UPDATE tags SET zh_name = ? WHERE name = ?",
                    (zh_val.strip(), eng_name.strip()),
                )
                tr_count += cur.rowcount

        if semantic_theme_groups:
            for group in semantic_theme_groups:
                theme_name = group.get("theme_name", "").strip()
                member_tags = [m.strip() for m in group.get("member_tags", []) if m.strip()]
                if not theme_name or not member_tags:
                    continue
                placeholders = ",".join(["?"] * len(member_tags))
                img_rows = conn.execute(
                    f"""
                    SELECT DISTINCT it.image_id
                    FROM image_tags it
                    JOIN tags t ON t.id = it.tag_id
                    WHERE t.name IN ({placeholders})
                    """,
                    member_tags,
                ).fetchall()
                if img_rows:
                    tid = ensure_tag(conn, tag_cache, theme_name, "general")
                    conn.execute("UPDATE tags SET zh_name = ? WHERE id = ?", (theme_name, tid))
                    conn.executemany(
                        "INSERT OR IGNORE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, 1.0, 'gemini_mcp')",
                        [(r["image_id"], tid) for r in img_rows],
                    )
                    theme_img_links += len(img_rows)

        refresh_tag_counts(conn)
    conn.close()

    builtin_res = {}
    if auto_cluster_builtin_themes:
        builtin_res = apply_semantic_theme_tags_batch()

    return {
        "custom_translations_updated": tr_count,
        "custom_theme_links_added": theme_img_links,
        "builtin_clusters": builtin_res,
    }


def tool_gallery_batch_tag_or_move(
    include_tags: List[str],
    add_artist_tag: Optional[str] = None,
    add_general_tags: Optional[List[str]] = None,
    move_to_folder: Optional[str] = None,
    only_root_files: bool = True,
) -> Dict[str, Any]:
    conn = get_db()
    where = ["1=1"]
    params: List[Any] = []
    if only_root_files:
        where.append("i.folder = ''")

    for tname in include_tags:
        where.append("""
            EXISTS (
                SELECT 1 FROM image_tags it
                JOIN tags t ON t.id = it.tag_id
                WHERE it.image_id = i.id AND (t.name = ? OR t.zh_name = ?)
            )
        """)
        params.extend([tname, tname])

    rows = conn.execute(
        f"SELECT i.id FROM images i WHERE {' AND '.join(where)}",
        params,
    ).fetchall()
    img_ids = [r["id"] for r in rows]

    if not img_ids:
        conn.close()
        return {"matched_images": 0, "message": "沒有符合條件的圖片"}

    tag_cache: Dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM tags"):
        tag_cache[r["name"]] = r["id"]

    with conn:
        if add_artist_tag:
            tid_a = ensure_tag(conn, tag_cache, add_artist_tag.strip(), "artist")
            conn.executemany(
                "INSERT OR IGNORE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, 1.0, 'gemini_mcp')",
                [(iid, tid_a) for iid in img_ids],
            )
        if add_general_tags:
            for gtag in add_general_tags:
                tid_g = ensure_tag(conn, tag_cache, gtag.strip(), "general")
                conn.executemany(
                    "INSERT OR IGNORE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, 1.0, 'gemini_mcp')",
                    [(iid, tid_g) for iid in img_ids],
                )
        refresh_tag_counts(conn)
    conn.close()

    moved = 0
    if move_to_folder and move_to_folder.strip():
        mres = move_images_to_folder(img_ids, move_to_folder.strip(), auto_rename=True)
        moved = mres.get("moved", 0)

    return {
        "matched_images": len(img_ids),
        "moved_to_folder": moved,
        "target_folder": move_to_folder,
    }


def tool_gallery_auto_organize_artist_folders(
    execute_move: bool = False,
    min_confidence: float = 0.25,
    min_images_per_artist: int = 1,
    only_existing_folders: bool = False,
    auto_rename_on_move: bool = True,
) -> Dict[str, Any]:
    from app import api_organize_artist_folders_preview, api_organize_move, MoveFilesRequest
    if not execute_move:
        return api_organize_artist_folders_preview(
            min_confidence=min_confidence,
            min_images_per_artist=min_images_per_artist,
            only_existing_folders=only_existing_folders,
        )
    req = MoveFilesRequest(
        mode="auto_artist_folders",
        min_confidence=min_confidence,
        min_images_per_artist=min_images_per_artist,
        only_existing_folders=only_existing_folders,
        auto_rename_on_move=auto_rename_on_move,
    )
    return api_organize_move(req)


def tool_gallery_list_candidates_for_artist_calibration(
    scope: str = "ai_low_conf",
    limit: int = 30,
) -> List[Dict[str, Any]]:
    """
    列出需要透過「以圖搜圖 + Gemini Flash 3.8 Google 搜尋比對與校正」的圖片清單：
    - scope='ai_low_conf': 僅有本機 AI 猜測畫師 (source='ai') 且尚未經過 Pixiv/Danbooru/SauceNAO/Gemini MCP 認證的圖片
    - scope='recent_root': 根目錄中最近修改且尚未認證畫師的圖片
    - scope='small_folders_ai': 剛才僅憑本機 AI 低信心度猜測而歸檔至新資料夾 (圖片數 <= 3 且無 pixiv/danbooru/saucenao 認證) 的圖片
    """
    conn = get_db()
    verified_sql = """
        SELECT it.image_id FROM image_tags it
        JOIN tags t ON t.id = it.tag_id
        WHERE t.category = 'artist'
          AND it.source IN ('pixiv', 'danbooru', 'saucenao', 'iqdb', 'twitter', 'gemini_mcp')
    """
    if scope == "small_folders_ai":
        rows = conn.execute(
            f"""
            SELECT i.id, i.rel_path, i.filename, i.folder, i.pixiv_id
            FROM images i
            WHERE i.folder != ''
              AND i.id IN (
                  SELECT it.image_id FROM image_tags it
                  JOIN tags t ON t.id = it.tag_id
                  WHERE t.category = 'artist' AND it.source = 'ai'
              )
              AND i.id NOT IN ({verified_sql})
              AND i.folder IN (
                  SELECT folder FROM images WHERE folder != '' GROUP BY folder HAVING COUNT(*) <= 3
              )
            ORDER BY i.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    elif scope == "recent_root":
        rows = conn.execute(
            f"""
            SELECT i.id, i.rel_path, i.filename, i.folder, i.pixiv_id
            FROM images i
            WHERE i.folder = ''
              AND i.media_type = 'image'
              AND i.id NOT IN ({verified_sql})
            ORDER BY i.mtime DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT i.id, i.rel_path, i.filename, i.folder, i.pixiv_id
            FROM images i
            WHERE i.media_type = 'image'
              AND i.id IN (
                  SELECT it.image_id FROM image_tags it
                  JOIN tags t ON t.id = it.tag_id
                  WHERE t.category = 'artist' AND it.source = 'ai'
              )
              AND i.id NOT IN ({verified_sql})
            ORDER BY i.mtime DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    results = []
    for r in rows:
        t_rows = conn.execute(
            """
            SELECT t.name, t.zh_name, t.category, it.confidence, it.source
            FROM image_tags it
            JOIN tags t ON t.id = it.tag_id
            WHERE it.image_id = ?
            ORDER BY it.confidence DESC
            """,
            (r["id"],),
        ).fetchall()
        artists = [
            {"name": tr["name"], "zh_name": tr["zh_name"], "confidence": round(tr["confidence"], 3), "source": tr["source"]}
            for tr in t_rows if tr["category"] == "artist"
        ]
        chars = [tr["name"] for tr in t_rows if tr["category"] == "character"]
        copys = [tr["name"] for tr in t_rows if tr["category"] == "copyright"]
        gens = [tr["name"] for tr in t_rows if tr["category"] == "general"][:15]
        results.append({
            "image_id": r["id"],
            "rel_path": r["rel_path"],
            "full_path": str(GALLERY_ROOT / r["rel_path"]),
            "filename": r["filename"],
            "folder": r["folder"],
            "pixiv_id": r["pixiv_id"],
            "current_artists": artists,
            "current_characters": chars,
            "current_copyrights": copys,
            "current_general_sample": gens,
        })
    conn.close()
    return results


def tool_gallery_deep_reverse_search_batch(
    image_ids: Optional[List[int]] = None,
    scope: str = "ai_low_conf",
    limit: int = 15,
    min_similarity: float = 72.0,
    include_google_lens_url: bool = True,
    auto_move_verified: bool = True,
) -> Dict[str, Any]:
    """
    對指定圖片執行「Google Lens 以圖搜圖網址生成 + Danbooru MD5/Post/Artist API + IQDB + SauceNAO 深度反查與自動校正」：
    - 若命中認證畫師，自動清除錯誤的 AI 畫師猜測並寫入認證畫師、Twitter @帳號、角色、作品與完整標籤。
    - 若圖片原先被放在錯誤的 AI 猜測資料夾（或位於根目錄），且 `auto_move_verified=True`，自動搬移至正確繪師資料夾並清理空資料夾。
    - 回傳每張圖的反查結果、Google Lens 視覺搜尋網址與建議的 Google 搜尋關鍵字，供 Gemini Flash 3.8 進一步比對與校正。
    """
    if not image_ids:
        cands = tool_gallery_list_candidates_for_artist_calibration(scope=scope, limit=limit)
        image_ids = [c["image_id"] for c in cands]

    items_report = []
    verified_count = 0
    moved_count = 0
    purged_tags_total = 0

    for iid in image_ids:
        conn = get_db()
        row = conn.execute("SELECT id, rel_path, filename, folder FROM images WHERE id = ?", (iid,)).fetchone()
        conn.close()
        if not row:
            continue

        old_folder = row["folder"]
        full_path = str(GALLERY_ROOT / row["rel_path"])

        res = reverse_search_and_tag_image(
            image_id=iid,
            min_similarity=min_similarity,
            fallback_to_ai=False,
            include_google_lens_url=include_google_lens_url,
        )
        is_verified = bool(res.get("found") and res.get("artists"))
        if is_verified:
            verified_count += 1
            purged_tags_total += int(res.get("purged_wrong_ai_tags", 0))

        sug_folder = res.get("suggested_folder")
        moved_to = None
        if is_verified and auto_move_verified and sug_folder:
            if old_folder != sug_folder:
                m_res = move_images_to_folder([iid], sug_folder, auto_rename=True)
                if m_res.get("moved", 0) > 0:
                    moved_count += 1
                    moved_to = sug_folder
                    # If old_folder was a temporary AI-guessed folder and is now empty, remove it
                    if old_folder:
                        old_dir = GALLERY_ROOT / old_folder
                        if old_dir.exists() and old_dir.is_dir():
                            try:
                                if not any(old_dir.iterdir()):
                                    old_dir.rmdir()
                            except Exception:
                                pass

        # Build Google search query suggestion for Gemini Flash 3.8 verification
        q_parts = []
        if res.get("artists"):
            q_parts.extend(res["artists"][:2])
        if res.get("twitter_handles"):
            q_parts.append(f"@{res['twitter_handles'][0]}")
        if res.get("characters"):
            q_parts.extend(res["characters"][:2])
        if res.get("copyrights"):
            q_parts.extend(res["copyrights"][:1])

        items_report.append({
            "image_id": iid,
            "filename": row["filename"],
            "full_path": full_path if not moved_to else str(GALLERY_ROOT / moved_to),
            "old_folder": old_folder,
            "moved_to_folder": moved_to,
            "verified": is_verified,
            "method": res.get("method"),
            "similarity": res.get("similarity", 0.0),
            "artists": res.get("artists", []),
            "cjk_artist_aliases": res.get("cjk_artist_aliases", {}),
            "twitter_handles": res.get("twitter_handles", []),
            "characters": res.get("characters", []),
            "copyrights": res.get("copyrights", []),
            "source_urls": res.get("source_urls", []),
            "google_lens_url": res.get("google_lens_url"),
            "suggested_google_query": " ".join(q_parts) if q_parts else row["filename"],
            "purged_wrong_ai_tags": res.get("purged_wrong_ai_tags", 0),
        })
        time.sleep(0.8)

    return {
        "total_processed": len(items_report),
        "verified_artists_count": verified_count,
        "moved_to_correct_folder_count": moved_count,
        "purged_wrong_ai_tags_total": purged_tags_total,
        "items": items_report,
    }


def tool_gallery_calibrate_image_artist_and_tags(
    calibrations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    供 Gemini Flash 3.8 在透過 Google 以圖搜圖 / Web 搜尋與視覺特徵比對後，批次校正圖片的畫師與標籤：
    每個 calibration 物件支援：
    - image_id (int, 必填)
    - verified_artist (str, 選填): 經 Google / Danbooru / Twitter / Pixiv 確認的真實畫師名稱
    - artist_zh_name (str, 選填): 畫師的常用中文/日文名稱或別名 (例如 '飯桶君 (@sioconsole1)')
    - twitter_handle (str, 選填): 畫師的 Twitter @帳號 (不含 @ 或含 @ 皆可)
    - characters (list[str], 選填): 校正後的正確角色標籤
    - copyrights (list[str], 選填): 校正後的正確作品標籤
    - add_general_tags (list[str], 選填): 補充的內容標籤
    - zh_translations (dict[str, str], 選填): 標籤繁體中文翻譯對照
    - remove_tags (list[str], 選填): 欲移除的錯誤標籤名稱清單
    - purge_ai_artist_tags (bool, 預設 True): 是否清除該圖片上所有本機 AI 猜測的畫師標籤
    - move_to_artist_folder (bool, 預設 True): 若有 verified_artist，是否自動建立/搬移至該繪師資料夾並清除舊空資料夾
    """
    conn = get_db()
    tag_cache: Dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM tags"):
        tag_cache[r["name"]] = r["id"]

    calibrated_images = 0
    purged_total = 0
    moved_total = 0
    folders_to_move: List[Tuple[int, str, str]] = []

    with conn:
        for item in calibrations:
            iid = int(item["image_id"])
            row = conn.execute("SELECT id, folder FROM images WHERE id = ?", (iid,)).fetchone()
            if not row:
                continue

            v_artist = (item.get("verified_artist") or "").strip()
            a_zh = (item.get("artist_zh_name") or "").strip()
            tw_handle = (item.get("twitter_handle") or "").strip().lstrip("@")
            chars = item.get("characters") or []
            copys = item.get("copyrights") or []
            gens = item.get("add_general_tags") or []
            zh_map = item.get("zh_translations") or {}
            rem_tags = item.get("remove_tags") or []
            purge_ai = item.get("purge_ai_artist_tags", True)
            do_move = item.get("move_to_artist_folder", True)

            purged_total += calibrate_image_tags_in_db(
                conn,
                image_id=iid,
                has_verified_artist=bool(purge_ai or v_artist or tw_handle),
                has_verified_character=bool(chars),
                has_verified_copyright=bool(copys),
                remove_tag_names=rem_tags,
            )

            links = []
            if v_artist:
                tid_a = ensure_tag(conn, tag_cache, v_artist, "artist")
                if tid_a > 0:
                    links.append((iid, tid_a, 1.0, "gemini_mcp"))
                    if a_zh:
                        conn.execute("UPDATE tags SET zh_name = ? WHERE id = ?", (a_zh, tid_a))

            if tw_handle:
                tw_tag = f"@{tw_handle}"
                tid_tw = ensure_tag(conn, tag_cache, tw_tag, "artist")
                if tid_tw > 0:
                    links.append((iid, tid_tw, 1.0, "twitter"))

            for ch in chars:
                tid_c = ensure_tag(conn, tag_cache, ch.strip(), "character")
                if tid_c > 0:
                    links.append((iid, tid_c, 1.0, "gemini_mcp"))

            for cp in copys:
                tid_cp = ensure_tag(conn, tag_cache, cp.strip(), "copyright")
                if tid_cp > 0:
                    links.append((iid, tid_cp, 1.0, "gemini_mcp"))

            for g in gens:
                tid_g = ensure_tag(conn, tag_cache, g.strip(), "general")
                if tid_g > 0:
                    links.append((iid, tid_g, 1.0, "gemini_mcp"))

            for eng_k, zh_v in zh_map.items():
                conn.execute("UPDATE tags SET zh_name = ? WHERE name = ?", (zh_v.strip(), eng_k.strip()))

            if links:
                conn.executemany(
                    """
                    INSERT INTO image_tags (image_id, tag_id, confidence, source)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(image_id, tag_id) DO UPDATE SET
                    confidence = 1.0,
                    source = excluded.source
                    """,
                    links,
                )

            target_folder = None
            if v_artist:
                target_folder = resolve_artist_folder_name(v_artist)
            elif tw_handle:
                target_folder = resolve_artist_folder_name(tw_handle)

            if target_folder:
                conn.execute(
                    "UPDATE images SET ai_tagged = 1, suggested_folder = ? WHERE id = ?",
                    (target_folder, iid),
                )
                if do_move and row["folder"] != target_folder:
                    folders_to_move.append((iid, row["folder"], target_folder))

            calibrated_images += 1

        refresh_tag_counts(conn)
    conn.close()

    for iid, old_folder, target_folder in folders_to_move:
        m_res = move_images_to_folder([iid], target_folder, auto_rename=True)
        if m_res.get("moved", 0) > 0:
            moved_total += 1
            if old_folder:
                old_dir = GALLERY_ROOT / old_folder
                if old_dir.exists() and old_dir.is_dir():
                    try:
                        if not any(old_dir.iterdir()):
                            old_dir.rmdir()
                    except Exception:
                        pass

    return {
        "calibrated_images": calibrated_images,
        "purged_wrong_tags": purged_total,
        "moved_to_verified_folder": moved_total,
    }


def tool_gallery_purge_all_low_conf_ai_artists(
    max_ai_conf_to_purge: float = 0.75,
    revert_unverified_ai_folders: bool = True,
) -> Dict[str, Any]:
    """
    清理圖庫中所有由本機 AI (`source='ai'`) 低信心度 (< 0.75) 亂猜的畫師標籤，
    並將先前僅因低信心度 AI 猜測而誤搬入資料夾（`ai_tagged = 1` 且未經 Pixiv/Danbooru/SauceNAO/IQDB/Gemini 認證）的圖片移回根目錄、刪除空的誤建資料夾。
    注意：使用者原有的 18,163 張已整理資料夾圖片 (`ai_tagged = 0`) 與已認證畫師圖片完全不會受影響。
    """
    conn = get_db()

    reverted_images = 0
    removed_empty_folders = 0

    if revert_unverified_ai_folders:
        verified_sql = """
            SELECT it.image_id FROM image_tags it
            JOIN tags t ON t.id = it.tag_id
            WHERE t.category = 'artist'
              AND (
                  it.source IN ('pixiv', 'danbooru', 'saucenao', 'iqdb', 'twitter', 'gemini_mcp', 'manual')
                  OR (it.source = 'ai' AND it.confidence >= ?)
              )
        """
        cand_rows = conn.execute(
            f"""
            SELECT i.id, i.rel_path, i.filename, i.folder
            FROM images i
            WHERE i.folder != ''
              AND i.ai_tagged = 1
              AND i.id NOT IN ({verified_sql})
            """,
            (max_ai_conf_to_purge,),
        ).fetchall()

        affected_folders = {r["folder"] for r in cand_rows if r["folder"]}
        cand_ids = [r["id"] for r in cand_rows]

        if cand_ids:
            # Remove the 'folder' artist tag that was added when it was moved into the AI-guessed folder
            with conn:
                for chunk_start in range(0, len(cand_ids), 500):
                    chunk = cand_ids[chunk_start:chunk_start + 500]
                    placeholders = ",".join(["?"] * len(chunk))
                    conn.execute(
                        f"DELETE FROM image_tags WHERE source = 'folder' AND image_id IN ({placeholders})",
                        chunk,
                    )

            for chunk_start in range(0, len(cand_ids), 500):
                chunk = cand_ids[chunk_start:chunk_start + 500]
                m_res = move_images_to_folder(chunk, "__ROOT__", auto_rename=False)
                reverted_images += m_res.get("moved", 0)

        for fname in affected_folders:
            fdir = GALLERY_ROOT / fname
            if fdir.exists() and fdir.is_dir():
                try:
                    if not any(fdir.iterdir()):
                        fdir.rmdir()
                        removed_empty_folders += 1
                except Exception:
                    pass

    with conn:
        # 1. Delete all low-confidence AI artist tags (< max_ai_conf_to_purge)
        cur1 = conn.execute(
            """
            DELETE FROM image_tags
            WHERE source = 'ai'
              AND confidence < ?
              AND tag_id IN (SELECT id FROM tags WHERE category = 'artist')
            """,
            (max_ai_conf_to_purge,),
        )
        purged_low_conf = cur1.rowcount

        # 2. Delete ANY 'ai' artist tags on images that already have a verified artist tag
        cur2 = conn.execute(
            """
            DELETE FROM image_tags
            WHERE source = 'ai'
              AND tag_id IN (SELECT id FROM tags WHERE category = 'artist')
              AND image_id IN (
                  SELECT it2.image_id FROM image_tags it2
                  JOIN tags t2 ON t2.id = it2.tag_id
                  WHERE t2.category = 'artist'
                    AND it2.source IN ('folder', 'pixiv', 'danbooru', 'saucenao', 'iqdb', 'twitter', 'gemini_mcp', 'manual')
              )
            """
        )
        purged_redundant = cur2.rowcount

        # 3. Clear unverified suggested_folder on root images
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
        refresh_tag_counts(conn)

    conn.close()
    return {
        "purged_low_conf_ai_artist_links": purged_low_conf,
        "purged_redundant_ai_artist_links": purged_redundant,
        "reverted_misplaced_ai_images": reverted_images,
        "removed_empty_ai_folders": removed_empty_folders,
    }


# Expose via FastMCP if installed, else lightweight MCP JSON-RPC 2.0 Stdio Server
try:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("twitter-tag-gallery")

    @mcp.tool()
    def gallery_get_overview() -> dict:
        """取得 D:\\圖檔\\twitter 圖庫總覽統計、前 30 大畫師與前 40 大標籤清單"""
        return tool_gallery_get_overview()

    @mcp.tool()
    def gallery_find_tag_correlations(target_tag: str, min_cooccurrence: int = 2, top_k: int = 40) -> dict:
        """搜尋指定標籤或畫師 (target_tag) 在圖庫中的相關性與共現標籤 (計算 Jaccard 相似度與條件機率)"""
        return tool_gallery_find_tag_correlations(target_tag, min_cooccurrence, top_k)

    @mcp.tool()
    def gallery_semantic_search(query: str) -> dict:
        """輸入自然語言或關鍵字，透過標籤相關性與中英對照自動展開並搜尋最相關的圖片與關聯標籤"""
        return tool_gallery_semantic_search(query)

    @mcp.tool()
    def gallery_list_untranslated_tags(category: str = "all", limit: int = 100) -> list:
        """列出圖庫中尚未有繁體中文翻譯的高頻標籤，供 Gemini Flash 批次進行中文語意翻譯與歸類"""
        return tool_gallery_list_untranslated_tags(category, limit)

    @mcp.tool()
    def gallery_apply_gemini_enrichments(
        translations: dict = None,
        semantic_theme_groups: list = None,
        auto_cluster_builtin_themes: bool = True,
    ) -> dict:
        """將 Gemini Flash 整理出的標籤中文翻譯 (translations) 與高階主題相關性群組 (semantic_theme_groups) 寫入圖庫"""
        return tool_gallery_apply_gemini_enrichments(translations, semantic_theme_groups, auto_cluster_builtin_themes)

    @mcp.tool()
    def gallery_batch_tag_or_move(
        include_tags: list[str],
        add_artist_tag: str = None,
        add_general_tags: list[str] = None,
        move_to_folder: str = None,
        only_root_files: bool = True,
    ) -> dict:
        """根據相關性標籤組合篩選圖片，批次新增畫師/主題標籤，或自動編號歸檔至繪師資料夾"""
        return tool_gallery_batch_tag_or_move(
            include_tags, add_artist_tag, add_general_tags, move_to_folder, only_root_files
        )

    @mcp.tool()
    def gallery_reverse_image_search(
        image_id: int,
        min_similarity: float = 72.0,
        fallback_to_ai: bool = False,
    ) -> dict:
        """透過 Danbooru MD5/Post/Artist API、IQDB、SauceNAO 與 Google Lens 以圖搜圖反查單張圖片的認證畫師與完整標籤，並自動校正錯誤 AI 標籤"""
        return reverse_search_and_tag_image(
            image_id=image_id,
            min_similarity=min_similarity,
            fallback_to_ai=fallback_to_ai,
            include_google_lens_url=True,
        )

    @mcp.tool()
    def gallery_list_candidates_for_artist_calibration(
        scope: str = "ai_low_conf",
        limit: int = 30,
    ) -> list:
        """列出僅有本機 AI 低信心度畫師猜測或待驗證畫師的圖片清單 (含檔案路徑供 Gemini Flash 3.8 檢視)"""
        return tool_gallery_list_candidates_for_artist_calibration(scope=scope, limit=limit)

    @mcp.tool()
    def gallery_deep_reverse_search_batch(
        image_ids: list[int] = None,
        scope: str = "ai_low_conf",
        limit: int = 15,
        min_similarity: float = 72.0,
        include_google_lens_url: bool = True,
        auto_move_verified: bool = True,
    ) -> dict:
        """批次執行深度以圖搜圖 (Google Lens 網址生成 + Danbooru API + IQDB + SauceNAO)，自動清除錯誤 AI 猜測並回傳認證畫師與 Google 搜尋建議"""
        return tool_gallery_deep_reverse_search_batch(
            image_ids=image_ids,
            scope=scope,
            limit=limit,
            min_similarity=min_similarity,
            include_google_lens_url=include_google_lens_url,
            auto_move_verified=auto_move_verified,
        )

    @mcp.tool()
    def gallery_calibrate_image_artist_and_tags(
        calibrations: list[dict],
    ) -> dict:
        """供 Gemini Flash 3.8 完成 Google 以圖搜圖/搜尋比對後，寫入認證畫師、Twitter @帳號、角色、作品與校正標籤，並自動清除錯誤 AI 畫師猜測與歸檔"""
        return tool_gallery_calibrate_image_artist_and_tags(calibrations=calibrations)

    @mcp.tool()
    def gallery_purge_all_low_conf_ai_artists(
        max_ai_conf_to_purge: float = 0.65,
        revert_unverified_ai_folders: bool = True,
    ) -> dict:
        """批次清除資料庫中本機 AI 低信心度 (< 0.65) 亂猜的畫師標籤，並還原僅因低信心度 AI 猜測而誤建的資料夾"""
        return tool_gallery_purge_all_low_conf_ai_artists(
            max_ai_conf_to_purge=max_ai_conf_to_purge,
            revert_unverified_ai_folders=revert_unverified_ai_folders,
        )

    @mcp.tool()
    def gallery_auto_organize_artist_folders(
        execute_move: bool = False,
        min_confidence: float = 0.25,
        min_images_per_artist: int = 1,
        only_existing_folders: bool = False,
        auto_rename_on_move: bool = True,
    ) -> dict:
        """預覽或執行自動建立繪師資料夾 (D:\\圖檔\\twitter\\<繪師名稱>) 並將根目錄已認證畫師圖片移入歸檔"""
        return tool_gallery_auto_organize_artist_folders(
            execute_move=execute_move,
            min_confidence=min_confidence,
            min_images_per_artist=min_images_per_artist,
            only_existing_folders=only_existing_folders,
            auto_rename_on_move=auto_rename_on_move,
        )

    if __name__ == "__main__":
        mcp.run()


except ImportError:
    TOOLS_SCHEMA = [
        {
            "name": "gallery_get_overview",
            "description": "取得 D:\\圖檔\\twitter 圖庫總覽統計、前 30 大畫師與前 40 大標籤清單",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "gallery_find_tag_correlations",
            "description": "搜尋指定標籤或畫師 (target_tag) 在圖庫中的相關性與共現標籤",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target_tag": {"type": "string"},
                    "min_cooccurrence": {"type": "integer", "default": 2},
                    "top_k": {"type": "integer", "default": 40},
                },
                "required": ["target_tag"],
            },
        },
        {
            "name": "gallery_semantic_search",
            "description": "輸入自然語言或關鍵字，透過標籤相關性展開並搜尋最相關的圖片與關聯標籤",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
        {
            "name": "gallery_list_untranslated_tags",
            "description": "列出圖庫中尚未有繁體中文翻譯的高頻標籤，供 Gemini Flash 批次翻譯與歸類",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "default": "all"},
                    "limit": {"type": "integer", "default": 100},
                },
            },
        },
        {
            "name": "gallery_apply_gemini_enrichments",
            "description": "將 Gemini Flash 整理出的標籤中文翻譯與高階主題相關性群組寫入圖庫",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "translations": {"type": "object"},
                    "semantic_theme_groups": {"type": "array"},
                    "auto_cluster_builtin_themes": {"type": "boolean", "default": True},
                },
            },
        },
        {
            "name": "gallery_batch_tag_or_move",
            "description": "根據相關性標籤組合篩選圖片，批次新增畫師/主題標籤，或自動編號歸檔至繪師資料夾",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "include_tags": {"type": "array", "items": {"type": "string"}},
                    "add_artist_tag": {"type": "string"},
                    "add_general_tags": {"type": "array", "items": {"type": "string"}},
                    "move_to_folder": {"type": "string"},
                    "only_root_files": {"type": "boolean", "default": True},
                },
                "required": ["include_tags"],
            },
        },
        {
            "name": "gallery_list_candidates_for_artist_calibration",
            "description": "列出僅有本機 AI 低信心度畫師猜測或待驗證畫師的圖片清單",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "default": "ai_low_conf"},
                    "limit": {"type": "integer", "default": 30},
                },
            },
        },
        {
            "name": "gallery_deep_reverse_search_batch",
            "description": "批次執行深度以圖搜圖 (Google Lens 網址生成 + Danbooru API + IQDB + SauceNAO)，自動清除錯誤 AI 猜測並回傳認證畫師",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "image_ids": {"type": "array", "items": {"type": "integer"}},
                    "scope": {"type": "string", "default": "ai_low_conf"},
                    "limit": {"type": "integer", "default": 15},
                    "min_similarity": {"type": "number", "default": 72.0},
                    "include_google_lens_url": {"type": "boolean", "default": True},
                    "auto_move_verified": {"type": "boolean", "default": True},
                },
            },
        },
        {
            "name": "gallery_calibrate_image_artist_and_tags",
            "description": "供 Gemini Flash 3.8 完成 Google 以圖搜圖/搜尋比對後，寫入認證畫師、Twitter @帳號、角色、作品與校正標籤",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "calibrations": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["calibrations"],
            },
        },
        {
            "name": "gallery_purge_all_low_conf_ai_artists",
            "description": "批次清除資料庫中本機 AI 低信心度 (< 0.65) 亂猜的畫師標籤，並還原誤建的資料夾",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "max_ai_conf_to_purge": {"type": "number", "default": 0.65},
                    "revert_unverified_ai_folders": {"type": "boolean", "default": True},
                },
            },
        },
    ]

    DISPATCH = {
        "gallery_get_overview": lambda args: tool_gallery_get_overview(),
        "gallery_find_tag_correlations": lambda args: tool_gallery_find_tag_correlations(
            args["target_tag"], args.get("min_cooccurrence", 2), args.get("top_k", 40)
        ),
        "gallery_semantic_search": lambda args: tool_gallery_semantic_search(args["query"]),
        "gallery_list_untranslated_tags": lambda args: tool_gallery_list_untranslated_tags(
            args.get("category", "all"), args.get("limit", 100)
        ),
        "gallery_apply_gemini_enrichments": lambda args: tool_gallery_apply_gemini_enrichments(
            args.get("translations"), args.get("semantic_theme_groups"), args.get("auto_cluster_builtin_themes", True)
        ),
        "gallery_batch_tag_or_move": lambda args: tool_gallery_batch_tag_or_move(
            args["include_tags"],
            args.get("add_artist_tag"),
            args.get("add_general_tags"),
            args.get("move_to_folder"),
            args.get("only_root_files", True),
        ),
        "gallery_list_candidates_for_artist_calibration": lambda args: tool_gallery_list_candidates_for_artist_calibration(
            args.get("scope", "ai_low_conf"), args.get("limit", 30)
        ),
        "gallery_deep_reverse_search_batch": lambda args: tool_gallery_deep_reverse_search_batch(
            args.get("image_ids"),
            args.get("scope", "ai_low_conf"),
            args.get("limit", 15),
            args.get("min_similarity", 72.0),
            args.get("include_google_lens_url", True),
            args.get("auto_move_verified", True),
        ),
        "gallery_calibrate_image_artist_and_tags": lambda args: tool_gallery_calibrate_image_artist_and_tags(
            args["calibrations"]
        ),
        "gallery_purge_all_low_conf_ai_artists": lambda args: tool_gallery_purge_all_low_conf_ai_artists(
            args.get("max_ai_conf_to_purge", 0.65), args.get("revert_unverified_ai_folders", True)
        ),
    }

    def run_stdio_loop():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
                method = req.get("method")
                msg_id = req.get("id")
                if method == "initialize":
                    resp = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "twitter-tag-gallery", "version": "2.0.0"},
                        },
                    }
                    sys.stdout.write(json.dumps(resp) + "\n")
                    sys.stdout.flush()
                elif method == "tools/list":
                    resp = {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS_SCHEMA}}
                    sys.stdout.write(json.dumps(resp) + "\n")
                    sys.stdout.flush()
                elif method == "tools/call":
                    params = req.get("params", {})
                    tname = params.get("name")
                    targs = params.get("arguments", {})
                    fn = DISPATCH.get(tname)
                    if not fn:
                        raise ValueError(f"Unknown tool: {tname}")
                    res_data = fn(targs)
                    resp = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [{"type": "text", "text": json.dumps(res_data, ensure_ascii=False, indent=2)}]
                        },
                    }
                    sys.stdout.write(json.dumps(resp) + "\n")
                    sys.stdout.flush()
            except Exception as e:
                if "msg_id" in locals() and msg_id is not None:
                    err_resp = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32000, "message": str(e)},
                    }
                    sys.stdout.write(json.dumps(err_resp) + "\n")
                    sys.stdout.flush()

    if __name__ == "__main__":
        run_stdio_loop()
