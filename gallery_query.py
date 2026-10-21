"""Paginated image search shared by HTTP and MCP, without a web-app import."""
from typing import Any, Dict, List
import database
from background_work import mark_interactive
from query_cache import natural_order
from renamer import win_natural_key
from zh_dict import get_tag_display_name
from gallery_artist_service import artist_filter_names


def search_images(include_tags="", exclude_tags="", folder="__ALL__", media_type="all",
                  ai_status="all", q="", sort="natural_asc", page=1, page_size=60,
                  *, connect=None, prefetch=None, compact=False):
    import datetime as _dt
    import time as _time
    mark_interactive()
    conn = (connect or database.get_db)()
    try:
        conn.execute("BEGIN")  # Keep counts, pagination and tag rows in one snapshot.
        where = ["i.missing_since IS NULL"]
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
                i.id IN (
                    SELECT id FROM images WHERE filename LIKE ? OR folder LIKE ?
                    UNION SELECT image_id FROM image_tags WHERE tag_id IN (
                        SELECT id FROM tags WHERE name LIKE ? OR zh_name LIKE ?
                    )
                )
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

        inc_list = [t.strip() for t in (include_tags.split(",") if isinstance(include_tags, str) else include_tags) if t.strip()]
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

            aliases = artist_filter_names(conn, tname)
            placeholders = ",".join("?" for _ in aliases)
            where.append(f"""
                i.id IN (
                    SELECT image_id FROM image_tags
                    WHERE tag_id IN (SELECT id FROM tags WHERE name IN ({placeholders}))
                )
            """)
            params.extend(aliases)

        if force_mtime_desc:
            sort = "mtime_desc"

        exc_list = [t.strip() for t in (exclude_tags.split(",") if isinstance(exclude_tags, str) else exclude_tags) if t.strip()]
        for tname in exc_list:
            if tname == "最近" or tname in recent_bucket_days:
                continue
            aliases = artist_filter_names(conn, tname)
            placeholders = ",".join("?" for _ in aliases)
            where.append(f"""
                i.id NOT IN (
                    SELECT image_id FROM image_tags
                    WHERE tag_id IN (SELECT id FROM tags WHERE name IN ({placeholders}))
                )
            """)
            params.extend(aliases)

        where_sql = " AND ".join(where)
        offset = (page - 1) * page_size

        if sort in ("natural_asc", "name_asc"):
            # Fetch matching rows and sort via Windows native StrCmpLogicalW so _2 is always before _10!
            def load_order():
                all_rows = conn.execute(
                f"""
                SELECT i.id, i.rel_path
                FROM images i NOT INDEXED
                WHERE {where_sql}
                """,
                params,
                ).fetchall()
                return sorted(all_rows, key=lambda r: win_natural_key(r["rel_path"]))
            sorted_all = natural_order.get((where_sql,tuple(params)),load_order)
            total_matches = len(sorted_all)
            page_ids = [r["id"] for r in sorted_all[offset : offset + page_size]]
            rows = []
            if page_ids:
                placeholders = ",".join("?" for _ in page_ids)
                page_rows = conn.execute(f"SELECT * FROM images WHERE id IN ({placeholders})", page_ids).fetchall()
                by_id = {r["id"]: r for r in page_rows}
                rows = [by_id[iid] for iid in page_ids]
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
        tag_counts = {}

        if img_ids:
            placeholders = ",".join(["?"] * len(img_ids))
            if compact:
                # Read link keys from the covering primary-key index first.
                # Materialization keeps SQLite from fetching general-tag payloads
                # or probing every artist against every image on the page.
                tag_rows = conn.execute(
                    f"""
                    WITH card_links AS MATERIALIZED (
                        SELECT it.image_id, it.tag_id
                        FROM image_tags it
                        CROSS JOIN tags t ON t.id = it.tag_id
                        WHERE it.image_id IN ({placeholders})
                          AND t.category IN ('artist', 'rating')
                    )
                    SELECT it.image_id, t.name, t.category, t.zh_name,
                           it.confidence, it.source
                    FROM card_links l
                    JOIN image_tags it ON it.image_id = l.image_id AND it.tag_id = l.tag_id
                    JOIN tags t ON t.id = l.tag_id
                    ORDER BY it.confidence DESC, it.tag_id ASC
                    """,
                    img_ids,
                ).fetchall()
                tag_counts = {r["image_id"]: r["total"] for r in conn.execute(
                    f"SELECT image_id, COUNT(*) AS total FROM image_tags "
                    f"WHERE image_id IN ({placeholders}) GROUP BY image_id", img_ids,
                )}
            else:
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
                if compact and any(t["category"] == tr["category"] for t in tags_by_img[tr["image_id"]]):
                    continue  # One artist and one rating are enough for a card.
                zh = get_tag_display_name(tr["name"], tr["category"], tr["zh_name"])
                tags_by_img[tr["image_id"]].append({
                    "name": tr["name"],
                    "category": tr["category"],
                    "zh_name": zh,
                    "confidence": tr["confidence"],
                    "source": tr["source"],
                })

    finally:
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
            "total_tags": tag_counts.get(iid, 0) if compact else len(all_t),
        })

    if prefetch is not None:
        prefetch(img_ids)
    return {
        "total": total_matches,
        "page": page,
        "page_size": page_size,
        "items": items,
    }
