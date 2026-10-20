"""Idle-only fingerprint backfill; reads files without holding SQLite transactions."""
from pathlib import Path
import database
from background_work import FileLock,background_status,interactive_recently
from file_safety import content_hash,gallery_path


def backfill_hashes(cursor=0,limit=16):
    lock=FileLock(database.DATA_DIR/"locks"/"hash-backfill.lock")
    if background_status() or interactive_recently() or not lock.acquire():
        return cursor,0
    try:
        conn=database.get_db()
        try:
            rows=[dict(row) for row in conn.execute("SELECT id,rel_path,size_bytes,mtime FROM images WHERE content_hash IS NULL AND missing_since IS NULL AND id>? ORDER BY id LIMIT ?",(cursor,limit))]
        finally:conn.close()
        if not rows:
            return 0,0
        updates=[]
        for row in rows:
            if background_status() or interactive_recently():
                break
            cursor=row["id"]
            try:
                path=gallery_path(database.GALLERY_ROOT,row["rel_path"]);stat=path.stat()
                if (stat.st_size,stat.st_mtime)!=(row["size_bytes"],row["mtime"]):
                    continue
                digest=content_hash(path)
                updates.append((digest,row["id"],row["rel_path"],row["size_bytes"],row["mtime"]))
            except (OSError,ValueError):
                continue
        if updates:
            conn=database.get_db()
            try:
                database.run_db_write_with_retry(conn,lambda c:c.executemany("UPDATE images SET content_hash=? WHERE id=? AND rel_path=? AND size_bytes=? AND mtime=? AND content_hash IS NULL",updates))
            finally:conn.close()
        return cursor,len(updates)
    finally:lock.release()
