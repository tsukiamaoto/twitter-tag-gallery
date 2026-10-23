# -*- coding: utf-8 -*-
import atexit
import os
import re
import shutil
import sqlite3
import time
import threading
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple, ContextManager

from sqlite_pool import SQLiteConnectionPool
from zh_dict import get_tag_zh, FOLDER_TO_DANBOORU_ARTIST, DANBOORU_ARTIST_TO_FOLDER, resolve_artist_folder_name
from background_work import exclusive_job, background_checkpoint, update_job

GALLERY_ROOT = Path(os.environ.get("TWITTER_GALLERY_ROOT",r"D:\圖檔\twitter"))
DATA_DIR = GALLERY_ROOT / ".tag_gallery"
THUMB_DIR = DATA_DIR / "thumbs"
MODEL_DIR = DATA_DIR / "models"
DB_PATH = DATA_DIR / "gallery.db"

VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4"}
NON_ARTIST_FOLDERS = {"1", "V", "W", "Pussy Spreading", ".tag_gallery", "_按日期歸檔"}

# Regex patterns for filename metadata extraction
RE_DATE_SEQ = re.compile(r"^((?:19|20)\d{2})(\d{2})(\d{2})_\d+")
RE_PIXIV = re.compile(r"^(?:\d+[_：:])?(\d{7,10})_p\d+", re.IGNORECASE)
RE_AI_SEED = re.compile(r"^\d{5}-\d{6,}")
RE_TWITTER_RAW = re.compile(r"^[A-Za-z0-9_-]{15}$")
RE_TWITTER_HANDLE_STATUS = re.compile(r"^([A-Za-z0-9_]{3,20})[-_](1[3-9]\d{17}|2[0-9]\d{17})(?:[-_].*)?$")
RE_TWITTERVID = re.compile(r"^twittervid\.com_[A-Za-z0-9_]+$", re.IGNORECASE)
RE_KONACHAN = re.compile(r"^Konachan\.com\s*-\s*(\d+)\s+(.*)$", re.IGNORECASE)
_retry_context = threading.local()
_DEFAULT_DB_PATH = DB_PATH
_db_pool_lock = threading.Lock()
_db_pool_idle: List["_PooledConnection"] = []
_DB_POOL_MAX_IDLE = 8


class _PooledConnection(sqlite3.Connection):
    _pool_path: Optional[Path] = None
    _in_pool: bool = False

    def real_close(self) -> None:
        super().close()

    def close(self) -> None:
        if self._pool_path is None or self._pool_path != DB_PATH:
            super().close()
            return
        try:
            if self.in_transaction:
                self.rollback()
            self.set_trace_callback(None)
        except sqlite3.Error:
            super().close()
            return
        with _db_pool_lock:
            if self._in_pool or len(_db_pool_idle) >= _DB_POOL_MAX_IDLE or self._pool_path != DB_PATH:
                should_close = not self._in_pool
            else:
                self._in_pool = True
                _db_pool_idle.append(self)
                should_close = False
        if should_close:
            super().close()


def close_db_pool() -> None:
    with _db_pool_lock:
        idle = list(_db_pool_idle)
        _db_pool_idle.clear()
    for conn in idle:
        try:
            conn.real_close()
        except sqlite3.Error:
            pass


atexit.register(close_db_pool)


def _open_raw_db(pooled: bool = False) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if pooled:
        conn = sqlite3.connect(str(DB_PATH), timeout=60.0, check_same_thread=False, factory=_PooledConnection)
        conn._pool_path = DB_PATH
    else:
        conn = sqlite3.connect(str(DB_PATH), timeout=60.0, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=60000;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA cache_size=-64000;")
        conn.execute("PRAGMA foreign_keys=ON;")
    except BaseException:
        if isinstance(conn, _PooledConnection):
            conn.real_close()
        else:
            conn.close()
        raise
    return conn


def get_db() -> sqlite3.Connection:
    if DB_PATH == _DEFAULT_DB_PATH:
        with _db_pool_lock:
            while _db_pool_idle:
                conn = _db_pool_idle.pop()
                conn._in_pool = False
                if conn._pool_path == DB_PATH:
                    return conn
                try:
                    conn.real_close()
                except sqlite3.Error:
                    pass
        return _open_raw_db(pooled=True)
    return _open_raw_db(pooled=False)


def _open_preview_db() -> sqlite3.Connection:
    conn = _open_raw_db(pooled=False)
    try:
        conn.execute("PRAGMA query_only=ON;")
    except BaseException:
        conn.close()
        raise
    return conn


_preview_db_pool = SQLiteConnectionPool(_open_preview_db, idle_timeout=60.0)
atexit.register(_preview_db_pool.close)


def get_preview_db() -> ContextManager[sqlite3.Connection]:
    """Borrow exclusively for a short preview read, returning even on exceptions."""
    return _preview_db_pool.connection()


def run_db_write_with_retry(conn: sqlite3.Connection, write_fn, max_retries: int = 15):
    """
    在短事務 `with conn:` 內執行寫入，若遇到瞬間多執行緒/多程序鎖庫競爭，自動進行指數退避重試。
    """
    import random
    for attempt in range(max_retries):
        cache_changes = []
        previous = getattr(_retry_context, "changes", None)
        _retry_context.changes = cache_changes
        try:
            with conn:
                return write_fn(conn)
        except BaseException as e:
            for cache, name in reversed(cache_changes):
                cache.pop(name, None)
            msg = str(e).lower()
            if isinstance(e, sqlite3.OperationalError) and ("locked" in msg or "busy" in msg) and attempt < max_retries - 1:
                try:
                    conn.rollback()
                except Exception:
                    pass
                time.sleep(random.uniform(0.05, 0.20) * (attempt + 1))
            else:
                raise
        finally:
            _retry_context.changes = previous


def init_db():
    from background_work import FileLock
    lock=FileLock(DATA_DIR/"locks"/"schema.lock")
    deadline=time.monotonic()+60
    while not lock.acquire():
        if time.monotonic()>deadline:
            raise RuntimeError("資料庫結構正在更新，請稍後重試")
        time.sleep(0.05)
    try:
        _init_db()
    finally:
        lock.release()


def _init_db():
    conn = get_db()
    try:
        with conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rel_path TEXT UNIQUE NOT NULL,
                filename TEXT NOT NULL,
                folder TEXT NOT NULL DEFAULT '',
                ext TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                mtime REAL NOT NULL DEFAULT 0,
                media_type TEXT NOT NULL DEFAULT 'image',
                pixiv_id TEXT DEFAULT NULL,
                ai_tagged INTEGER NOT NULL DEFAULT 0,
                suggested_folder TEXT DEFAULT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_images_folder ON images(folder);
            CREATE INDEX IF NOT EXISTS idx_images_folder_mtime ON images(folder, mtime);
            CREATE INDEX IF NOT EXISTS idx_images_ai_tagged ON images(ai_tagged);
            CREATE INDEX IF NOT EXISTS idx_images_media_type ON images(media_type);
            CREATE INDEX IF NOT EXISTS idx_images_mtime ON images(mtime DESC);
            CREATE INDEX IF NOT EXISTS idx_images_pixiv_id ON images(pixiv_id);

            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                category TEXT NOT NULL DEFAULT 'general',
                zh_name TEXT NOT NULL DEFAULT '',
                post_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_tags_category ON tags(category);
            CREATE INDEX IF NOT EXISTS idx_tags_count ON tags(post_count DESC);

            CREATE TABLE IF NOT EXISTS image_tags (
                image_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                source TEXT NOT NULL DEFAULT 'ai',
                PRIMARY KEY (image_id, tag_id),
                FOREIGN KEY (image_id) REFERENCES images(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_image_tags_tag ON image_tags(tag_id, image_id);

            CREATE INDEX IF NOT EXISTS idx_image_tags_source_tag ON image_tags(source, tag_id);
            CREATE INDEX IF NOT EXISTS idx_image_tags_img_source ON image_tags(image_id, source);

            CREATE TABLE IF NOT EXISTS operation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                created_at REAL NOT NULL,
                category TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'success',
                summary TEXT NOT NULL,
                affected_count INTEGER NOT NULL DEFAULT 0,
                duration_sec REAL NOT NULL DEFAULT 0.0,
                details_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_op_logs_created ON operation_logs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_op_logs_category ON operation_logs(category, created_at DESC);
            """)
        from schema import migrate
        with conn:
            migrate(conn)
    finally:
        conn.close()


def ensure_tag(conn: sqlite3.Connection, tag_cache: Dict[str, int], name: str, category: str, *, source=None) -> int:
    # Check before the cache: an existing legacy tag must not bypass the policy.
    from tag_policy import allows_tag
    if source is not None and not allows_tag(category, source):
        return -1
    name_clean = name.strip()
    if not name_clean:
        return -1
    if source is not None and source != "manual":
        stored = conn.execute("SELECT category FROM tags WHERE name=?", (name_clean,)).fetchone()
        if stored is not None and not allows_tag(stored[0], source):
            # Do not relabel a retained manual/legacy description as a core
            # identity merely because a model submitted a different category.
            return -1
    if name_clean in tag_cache:
        return tag_cache[name_clean]

    zh = get_tag_zh(name_clean, category)
    cur = conn.execute(
        "INSERT INTO tags (name, category, zh_name) VALUES (?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET "
        "category = CASE WHEN tags.category = 'general' AND excluded.category != 'general' THEN excluded.category ELSE tags.category END, "
        "zh_name = CASE WHEN tags.zh_name = '' AND excluded.zh_name != '' THEN excluded.zh_name ELSE tags.zh_name END "
        "RETURNING id",
        (name_clean, category, zh)
    )
    row = cur.fetchone()
    if row:
        tid = row[0]
    else:
        tid = conn.execute("SELECT id FROM tags WHERE name = ?", (name_clean,)).fetchone()[0]
    changes = getattr(_retry_context, "changes", None)
    if changes is not None:
        changes.append((tag_cache, name_clean))
    tag_cache[name_clean] = tid
    return tid


def refresh_tag_counts(conn: sqlite3.Connection):
    # Triggers record affected IDs; a one-image edit does not recount the library.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='dirty_tag_counts'").fetchone():
        conn.execute("""UPDATE tags SET post_count=(SELECT COUNT(*) FROM image_tags WHERE tag_id=tags.id)
                        WHERE id IN (SELECT tag_id FROM dirty_tag_counts)""")
        conn.execute("DELETE FROM dirty_tag_counts")
        return
    conn.execute("""
        UPDATE tags
        SET post_count = (
            SELECT COUNT(*) FROM image_tags WHERE image_tags.tag_id = tags.id
        )
    """)
    conn.execute("DELETE FROM tags WHERE post_count <= 0 AND category != 'source'")



@exclusive_job("圖庫索引掃描")
def scan_library() -> Dict[str, Any]:
    """
    掃描 D:\\圖檔\\twitter 下所有檔案並同步至 SQLite，
    同時自動從「子資料夾名稱」與「檔名規則」提取畫師與來源標籤。
    """
    if not GALLERY_ROOT.is_dir():
        raise OSError("圖庫根目錄無法存取，保留全部索引")
    if (DATA_DIR/"file_journals"/"PENDING").exists():
        from file_operations import recover
        if recover(GALLERY_ROOT,DATA_DIR,get_db)["errors"]:
            raise RuntimeError("請先完成檔案復原，再掃描圖庫")
    conn = get_db()
    try:
        return _scan_library(conn, time.time())
    finally:
        conn.close()


def _scan_library(conn, t0):
    from file_safety import content_hash, file_identity, prune_directories
    from processing_state import reset

    # Load existing rel_paths
    existing = {}
    for row in conn.execute("SELECT * FROM images"):
        existing[row["rel_path"]] = dict(row)

    tag_cache: Dict[str, int] = {}
    for row in conn.execute("SELECT id, name FROM tags"):
        tag_cache[row["name"]] = row["id"]

    found_paths = set()
    new_images = []
    details = {}
    errors = []
    changed_ids = []
    hash_budget = 32  # Bounded migration backfill; unchanged libraries are not rehashed.
    hash_bytes_budget = 64*1024*1024
    def walk_error(error):
        errors.append({"path": str(getattr(error, "filename", "")), "error": str(error)})
    # Pre-scan filesystem
    for root, dirs, files in os.walk(str(GALLERY_ROOT), onerror=walk_error):
        background_checkpoint()
        # Skip hidden metadata dir
        dirs[:] = prune_directories(root, dirs)
        rel_dir = os.path.relpath(root, str(GALLERY_ROOT))
        if rel_dir == ".":
            rel_dir = ""
        top_folder = rel_dir.split(os.sep)[0] if rel_dir else ""

        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in VALID_EXTENSIONS:
                continue
            rel_path = os.path.join(rel_dir, fname) if rel_dir else fname
            rel_path_norm = rel_path.replace("\\", "/")
            found_paths.add(rel_path_norm)
            if len(found_paths) % 100 == 0:
                background_checkpoint()
                update_job(stage="掃描檔案", processed_count=len(found_paths), total_target=0, current_file=rel_path_norm)
            full_path = Path(root) / fname
            try:
                st = full_path.stat()
                old = existing.get(rel_path_norm)
                changed = bool(old and (old["size_bytes"] != st.st_size or
                    (old["mtime_ns"] != st.st_mtime_ns if old["mtime_ns"] else old["mtime"] != st.st_mtime)))
                digest = old["content_hash"] if old and not changed else None
                if not digest and (not old or changed or hash_budget > 0 and st.st_size <= hash_bytes_budget):
                    digest = content_hash(full_path)
                    if old and not changed:
                        hash_budget -= 1
                        hash_bytes_budget -= st.st_size
                details[rel_path_norm] = (st.st_size, st.st_mtime, st.st_mtime_ns, file_identity(st), digest)
                if changed:
                    changed_ids.append(old["id"])
            except OSError as error:
                walk_error(error)
                continue

            if rel_path_norm not in existing:
                size_bytes, mtime = st.st_size, st.st_mtime

                if ext == ".mp4":
                    media_type = "video"
                elif ext == ".gif":
                    media_type = "gif"
                else:
                    media_type = "image"

                base = os.path.splitext(fname)[0]
                m_pixiv = RE_PIXIV.match(base)
                pixiv_id = m_pixiv.group(1) if m_pixiv else None

                new_images.append((
                    rel_path_norm, fname, top_folder, ext, size_bytes, mtime, media_type, pixiv_id
                ))

    removed_paths = set(existing.keys()) - found_paths
    reconciled = []
    # Only unique known identities may preserve an image ID across an external move.
    identities = {}
    for path in removed_paths:
        old = existing[path]
        for key in (("file", old["file_identity"]), ("hash", old["content_hash"])):
            if key[1]:
                identities.setdefault(key, []).append(old)
    if not errors:
        remaining = []
        for item in new_images:
            path = item[0]
            info = details[path]
            matches = identities.get(("file", info[3]), []) or identities.get(("hash", info[4]), [])
            matches = [row for row in matches if row["rel_path"] in removed_paths]
            if len(matches) == 1:
                old = matches[0]
                reconciled.append((path, item[1], item[2], old["id"]))
                if old["content_hash"] and info[4] != old["content_hash"]:
                    changed_ids.append(old["id"])
                removed_paths.remove(old["rel_path"])
            else:
                remaining.append(item)
        new_images = remaining

    with conn:
        if removed_paths and not errors:
            conn.executemany(
                "UPDATE images SET missing_since=COALESCE(missing_since,?),missing_scans=missing_scans+1 WHERE rel_path=?",
                [(time.time(), p) for p in removed_paths]
            )
        if reconciled:
            conn.executemany("UPDATE images SET rel_path=?,filename=?,folder=?,missing_since=NULL,missing_scans=0 WHERE id=?", reconciled)
            for _,_,folder,iid in reconciled:
                conn.execute("DELETE FROM image_tags WHERE image_id=? AND source='folder' AND tag_id IN (SELECT id FROM tags WHERE category='artist')",(iid,))
                if folder and folder not in NON_ARTIST_FOLDERS:
                    tid=ensure_tag(conn,tag_cache,folder,"artist")
                    conn.execute("INSERT OR IGNORE INTO image_tags(image_id,tag_id,confidence,source) VALUES(?,?,1,'folder')",(iid,tid))

        if new_images:
            conn.executemany(
                """
                INSERT OR IGNORE INTO images
                (rel_path, filename, folder, ext, size_bytes, mtime, media_type, pixiv_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                new_images
            )

            # Fetch newly inserted IDs to assign folder & filename tags
            path_to_id = {}
            for row in conn.execute("SELECT id, rel_path, filename, folder, pixiv_id FROM images"):
                path_to_id[row["rel_path"]] = row

            tag_links = []
            sug_folder_updates = []
            for item in new_images:
                rel_path_norm = item[0]
                row = path_to_id.get(rel_path_norm)
                if not row:
                    continue
                img_id = row["id"]
                fname = row["filename"]
                folder = row["folder"]
                base = os.path.splitext(fname)[0]

                # 1. Folder-based Artist or Theme Tag
                if folder and folder not in NON_ARTIST_FOLDERS:
                    tid = ensure_tag(conn, tag_cache, folder, "artist")
                    tag_links.append((img_id, tid, 1.0, "folder"))
                    # Also link Danbooru artist alias if known
                    if folder in FOLDER_TO_DANBOORU_ARTIST:
                        db_artist = FOLDER_TO_DANBOORU_ARTIST[folder]
                        if db_artist.lower() != folder.lower():
                            tid2 = ensure_tag(conn, tag_cache, db_artist, "artist")
                            tag_links.append((img_id, tid2, 1.0, "folder"))

                # 2. Filename-based Date / Source / Twitter / Konachan tags
                if row["pixiv_id"]:
                    tid_p = ensure_tag(conn, tag_cache, "Pixiv來源", "source")
                    tag_links.append((img_id, tid_p, 1.0, "filename"))
                elif RE_AI_SEED.match(base):
                    tid_ai = ensure_tag(conn, tag_cache, "AI生成(檔名特徵)", "source")
                    tag_links.append((img_id, tid_ai, 1.0, "filename"))
                elif RE_TWITTER_RAW.match(base) or RE_TWITTERVID.match(base):
                    tid_tw = ensure_tag(conn, tag_cache, "Twitter原始檔名", "source")
                    tag_links.append((img_id, tid_tw, 1.0, "filename"))
                else:
                    m_tw_hs = RE_TWITTER_HANDLE_STATUS.match(base)
                    if m_tw_hs:
                        tid_tw = ensure_tag(conn, tag_cache, "Twitter原始檔名", "source")
                        tag_links.append((img_id, tid_tw, 1.0, "filename"))
                        tw_handle = m_tw_hs.group(1)
                        if not tw_handle.isdigit() and tw_handle.lower() not in ("twitter", "x", "video", "img", "image", "photo", "clip"):
                            tid_h = ensure_tag(conn, tag_cache, f"@{tw_handle}", "artist", source="twitter")
                            if tid_h > 0:
                                tag_links.append((img_id, tid_h, 1.0, "twitter"))
                            res_folder = resolve_artist_folder_name(tw_handle)
                            if res_folder in FOLDER_TO_DANBOORU_ARTIST:
                                db_artist = FOLDER_TO_DANBOORU_ARTIST[res_folder]
                                tid_db = ensure_tag(conn, tag_cache, db_artist, "artist", source="twitter")
                                if tid_db > 0:
                                    tag_links.append((img_id, tid_db, 1.0, "twitter"))
                            if not folder and res_folder:
                                sug_folder_updates.append((res_folder, img_id))
                    else:
                        m_kona = RE_KONACHAN.match(base)
                        if m_kona:
                            tid_k = ensure_tag(conn, tag_cache, "Konachan圖站", "source")
                            tag_links.append((img_id, tid_k, 1.0, "filename"))

            if tag_links:
                conn.executemany(
                    "INSERT OR IGNORE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, ?, ?)",
                    tag_links
                )
            if sug_folder_updates:
                conn.executemany(
                    "UPDATE images SET suggested_folder = COALESCE(?, suggested_folder) WHERE id = ?",
                    sug_folder_updates
                )

        for path, info in details.items():
            old = existing.get(path)
            if old is None or tuple(info) != (old["size_bytes"],old["mtime"],old["mtime_ns"],old["file_identity"],old["content_hash"]) or old["missing_since"]:
                conn.execute("UPDATE images SET size_bytes=?,mtime=?,mtime_ns=?,file_identity=?,content_hash=?,missing_since=NULL,missing_scans=0 WHERE rel_path=?", (*info,path))
        for iid in changed_ids:
            reset(conn, iid)
            conn.execute("DELETE FROM image_tags WHERE image_id=? AND source IN ('ai','semantic','gemini_flash')", (iid,))

        if new_images or changed_ids or reconciled:
            refresh_tag_counts(conn)

    total_count = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    root_count = conn.execute("SELECT COUNT(*) FROM images WHERE folder = ''").fetchone()[0]
    ai_tagged_count = conn.execute("SELECT COUNT(*) FROM images WHERE ai_tagged = 1").fetchone()[0]

    elapsed_s = round(time.time() - t0, 2)
    res_data = {
        "total_images": total_count,
        "root_unorganized": root_count,
        "folder_organized": total_count - root_count,
        "ai_tagged": ai_tagged_count,
        "new_added": len(new_images),
        "removed": 0,
        "missing_marked": len(removed_paths) if not errors else 0,
        "missing_total": conn.execute("SELECT COUNT(*) FROM images WHERE missing_since IS NOT NULL").fetchone()[0],
        "scan_complete": not errors,
        "errors": errors[:50],
        "modified": len(changed_ids),
        "relocated": len(reconciled),
        "elapsed_sec": elapsed_s,
    }
    log_operation(
        category="scan",
        action="重新掃描圖庫檔案變動",
        summary=f"掃描{'完成' if not errors else '不完整，保留索引'}：新增 {len(new_images):,} 張、標記遺失 {res_data['missing_marked']:,} 張、更新 {len(changed_ids):,} 張",
        status="success" if not errors else "warning",
        affected_count=len(new_images) + len(removed_paths),
        duration_sec=elapsed_s,
        details=res_data,
    )
    return res_data


ACTIVITY_LOG_FILE = DATA_DIR / "activity.log"


def log_operation(
    category: str,
    action: str,
    summary: str,
    status: str = "success",
    affected_count: int = 0,
    duration_sec: float = 0.0,
    details: Optional[Dict[str, Any]] = None,
) -> int:
    """
    統一紀錄應用程式內除搜尋與篩選以外的所有功能處理日誌：
    1. 寫入 SQLite `operation_logs` 資料表（供前端 UI 分類篩選與追蹤）
    2. 同步附加寫入 `D:\\圖檔\\twitter\\.tag_gallery\\activity.log` 實體檔案
    """
    import datetime
    import json

    now_dt = datetime.datetime.now()
    ts_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    created_at = time.time()
    details_str = json.dumps(details or {}, ensure_ascii=False)

    log_id = 0
    try:
        conn = get_db()
        try:
            def _insert_log(c):
                cur = c.execute(
                    """
                    INSERT INTO operation_logs
                    (timestamp, created_at, category, action, status, summary, affected_count, duration_sec, details_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    RETURNING id
                    """,
                    (
                        ts_str,
                        created_at,
                        category,
                        action,
                        status,
                        summary,
                        int(affected_count or 0),
                        round(float(duration_sec or 0.0), 2),
                        details_str,
                    ),
                )
                row = cur.fetchone()
                return row[0] if row else 0

            log_id = run_db_write_with_retry(conn, _insert_log)
        finally:
            conn.close()
    except Exception:
        pass

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        line = (
            f"[{ts_str}] [{status.upper()}] [{category}] {action} | "
            f"{summary} (影響: {affected_count} 筆, 耗時: {round(float(duration_sec or 0.0), 2)}s)"
        )
        if details:
            line += f" | details={details_str}"
        with open(ACTIVITY_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

    return log_id


def get_operation_logs(
    category: str = "all",
    q: str = "",
    limit: int = 100,
) -> Dict[str, Any]:
    import json

    conn = get_db()
    try:
        # Ensure table exists
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS operation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                created_at REAL NOT NULL,
                category TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'success',
                summary TEXT NOT NULL,
                affected_count INTEGER NOT NULL DEFAULT 0,
                duration_sec REAL NOT NULL DEFAULT 0.0,
                details_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )

        where = ["1=1"]
        params: List[Any] = []
        if category and category != "all":
            where.append("category = ?")
            params.append(category)
        if q and q.strip():
            q_like = f"%{q.strip()}%"
            where.append("(action LIKE ? OR summary LIKE ? OR details_json LIKE ?)")
            params.extend([q_like, q_like, q_like])

        where_sql = " AND ".join(where)
        rows = conn.execute(
            f"""
            SELECT id, timestamp, created_at, category, action, status, summary, affected_count, duration_sec, details_json
            FROM operation_logs
            WHERE {where_sql}
            ORDER BY id DESC
            LIMIT ?
            """,
            params + [int(limit)],
        ).fetchall()

        cat_counts = {
            r["category"]: r["c"]
            for r in conn.execute(
                "SELECT category, COUNT(*) as c FROM operation_logs GROUP BY category"
            ).fetchall()
        }
        total_logs = conn.execute("SELECT COUNT(*) FROM operation_logs").fetchone()[0]
    finally:
        conn.close()

    items = []
    for r in rows:
        try:
            det = json.loads(r["details_json"] or "{}")
        except Exception:
            det = {}
        items.append({
            "id": r["id"],
            "timestamp": r["timestamp"],
            "created_at": r["timestamp"],
            "category": r["category"],
            "action": r["action"],
            "status": r["status"],
            "summary": r["summary"],
            "affected_count": r["affected_count"],
            "duration_sec": r["duration_sec"],
            "details": det,
        })

    return {
        "total": total_logs,
        "category_counts": cat_counts,
        "log_file_path": str(ACTIVITY_LOG_FILE),
        "log_file": str(ACTIVITY_LOG_FILE),
        "items": items,
        "logs": items,
    }


def clear_operation_logs() -> int:
    conn = get_db()
    try:
        with conn:
            cur = conn.execute("DELETE FROM operation_logs")
            return cur.rowcount
    finally:
        conn.close()
