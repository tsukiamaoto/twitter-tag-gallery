"""Atomic human edits and category-limited model edits with source protection."""
import database
from background_work import exclusive_job


@exclusive_job("編輯圖片標籤")
def edit_tags(image_ids, add_tags, remove_tag_names, *, source="manual"):
    if source != "manual":
        from tag_policy import require_model_tags
        require_model_tags(add_tags)
    ids = list(dict.fromkeys(image_ids))
    if not ids:
        return {"updated": 0, "links_written": 0, "links_removed": 0}
    additions = {tag["name"].strip(): tag.get("category", "general").strip() or "general" for tag in add_tags if tag.get("name", "").strip()}
    removals = list(dict.fromkeys(name.strip() for name in remove_tag_names if name.strip()))
    if additions.keys() & set(removals):
        raise ValueError("同一個標籤不可同時新增與移除")
    conn = database.get_db()
    cache = {}
    try:
        def write(c):
            for start in range(0, len(ids), 500):
                chunk = ids[start:start+500]
                count = c.execute("SELECT COUNT(*) FROM images WHERE id IN (" + ",".join("?" for _ in chunk) + ")", chunk).fetchone()[0]
                if count != len(chunk):
                    raise ValueError("包含不存在的圖片 ID；未修改任何標籤")
            added = removed = 0
            for name, category in additions.items():
                tid = database.ensure_tag(c, cache, name, category, source=source)
                if tid < 0:
                    from tag_policy import POLICY_MESSAGE
                    raise ValueError(POLICY_MESSAGE)
                added += c.executemany("""INSERT INTO image_tags(image_id,tag_id,confidence,source) VALUES(?,?,1,?)
                    ON CONFLICT(image_id,tag_id) DO UPDATE SET confidence=1,source=excluded.source
                    WHERE image_tags.source!='manual' OR excluded.source='manual'""", [(iid, tid, source) for iid in ids]).rowcount
            for start in range(0, len(ids), 500):
                chunk = ids[start:start+500]
                for name in removals:
                    removed += c.execute("DELETE FROM image_tags WHERE tag_id=(SELECT id FROM tags WHERE name=?) AND image_id IN (" + ",".join("?" for _ in chunk) + ") AND (source!='manual' OR ?='manual')", [name, *chunk, source]).rowcount
            database.refresh_tag_counts(c)
            return {"updated": len(ids), "links_written": added, "links_removed": removed}
        return database.run_db_write_with_retry(conn, write)
    finally:
        conn.close()
