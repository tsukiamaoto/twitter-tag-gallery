"""Delete into a private trash folder using the recoverable file transaction."""
import json
import re
import uuid

import database
from background_work import exclusive_job
from file_operations import execute
from file_safety import gallery_path, UnsafePath


@exclusive_job("刪除圖片檔案")
def delete_images(image_ids):
    ids = list(dict.fromkeys(image_ids))
    if not ids or any(type(iid) is not int or iid <= 0 for iid in ids):
        raise ValueError("請選取有效的圖片 ID")
    operation_id = "delete_" + uuid.uuid4().hex
    entries = []
    conn = database.get_db()
    try:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(f"SELECT * FROM images WHERE id IN ({placeholders})", chunk).fetchall()
            for row in rows:
                source = gallery_path(database.GALLERY_ROOT, row["rel_path"])
                if ".tag_gallery" in {part.casefold() for part in source.relative_to(database.GALLERY_ROOT.resolve()).parts}:
                    raise UnsafePath("不可刪除圖庫系統檔案")
                if not source.is_file():
                    raise FileNotFoundError(f"圖片檔案不存在：{row['filename']}；尚未刪除任何檔案")
                tags = [dict(tag) for tag in conn.execute(
                    "SELECT t.name,t.category,t.zh_name,it.confidence,it.source "
                    "FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE it.image_id=?", (row["id"],))]
                processing = [dict(stage) for stage in conn.execute(
                    "SELECT * FROM image_processing WHERE image_id=?", (row["id"],))]
                entries.append({
                    "id": row["id"], "old_rel_path": row["rel_path"],
                    "new_rel_path": f".tag_gallery/trash/{operation_id}/{row['id']}/{source.name}",
                    "image": dict(row), "tags": tags, "processing": processing,
                })
        if len(entries) != len(ids):
            raise ValueError("包含不存在的圖片 ID；尚未刪除任何檔案")
    finally:
        conn.close()

    def remove_rows(conn, records):
        conn.executemany("DELETE FROM images WHERE id=?", [(entry["id"],) for entry in records])
        database.refresh_tag_counts(conn)

    execute(database.GALLERY_ROOT, database.DATA_DIR, database.get_db, entries,
            kind="delete", operation_id=operation_id, after_write=remove_rows)
    database.log_operation(category="files", action="刪除圖片至圖庫回收區",
                           summary=f"已刪除 {len(entries)} 個檔案，可復原", affected_count=len(entries),
                           details={"operation_id": operation_id})
    return {"deleted": len(entries), "deleted_ids": ids, "operation_id": operation_id}


@exclusive_job("復原刪除圖片")
def restore_deleted_images(operation_id):
    if not re.fullmatch(r"delete_[a-f0-9]{32}", operation_id):
        raise ValueError("無效的刪除批次 ID")
    manifest = database.DATA_DIR / "file_journals" / f"{operation_id}.json"
    plan = json.loads(manifest.read_text(encoding="utf-8"))
    if plan.get("kind") != "delete" or plan.get("status") != "committed":
        raise ValueError("此刪除批次尚未完成，請先執行檔案復原檢查")
    restore_prefix = "restore_" + operation_id + "_"
    conn = database.get_db()
    try:
        if conn.execute("SELECT 1 FROM file_operations WHERE kind='restore_delete' AND id GLOB ?", (restore_prefix + "*",)).fetchone():
            raise ValueError("此批次已經復原")
        entries = []
        for record in plan["entries"]:
            # Validate both destinations before moving any files. execute also
            # checks identities, path boundaries and collisions during the move.
            destination = gallery_path(database.GALLERY_ROOT, record["old_rel_path"])
            source = gallery_path(database.GALLERY_ROOT, record["new_rel_path"])
            stat = source.stat()
            identity = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
            if identity not in (record["identity"], record.get("copy_identity")):
                raise ValueError(f"回收區檔案已變更，尚未復原：{source.name}")
            if destination.exists():
                raise FileExistsError(f"原位置已有檔案，尚未復原：{destination.name}")
            if conn.execute("SELECT 1 FROM images WHERE id=? OR rel_path=?",
                            (record["image"]["id"], record["old_rel_path"])).fetchone():
                raise ValueError("原圖片索引已存在，尚未復原任何檔案")
            # No id: execute must not update a row before it is reinserted.
            entries.append({"old_rel_path": record["new_rel_path"],
                            "new_rel_path": record["old_rel_path"], "snapshot": record})
    finally:
        conn.close()

    def restore_rows(conn, records):
        cache = {}
        def insert(table, values):
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})") if row[1] in values]
            conn.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                         [values[column] for column in columns])
        for entry in records:
            snapshot = entry["snapshot"]
            insert("images", snapshot["image"])
            for tag in snapshot["tags"]:
                tid = database.ensure_tag(conn, cache, tag["name"], tag["category"])
                if tag["zh_name"]:
                    conn.execute("UPDATE tags SET zh_name=? WHERE id=? AND zh_name=''", (tag["zh_name"], tid))
                conn.execute("INSERT INTO image_tags(image_id,tag_id,confidence,source) VALUES(?,?,?,?)",
                             (snapshot["image"]["id"], tid, tag["confidence"], tag["source"]))
            for stage in snapshot["processing"]:
                insert("image_processing", stage)
        database.refresh_tag_counts(conn)

    execute(database.GALLERY_ROOT, database.DATA_DIR, database.get_db, entries,
            kind="restore_delete", operation_id=restore_prefix + uuid.uuid4().hex[:8], after_write=restore_rows)
    database.log_operation(category="files", action="復原刪除圖片",
                           summary=f"已復原 {len(entries)} 個檔案與標籤", affected_count=len(entries),
                           details={"operation_id": operation_id})
    return {"restored": len(entries)}
