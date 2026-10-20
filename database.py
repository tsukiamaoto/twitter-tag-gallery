# -*- coding: utf-8 -*-
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from zh_dict import get_tag_zh, FOLDER_TO_DANBOORU_ARTIST, DANBOORU_ARTIST_TO_FOLDER

GALLERY_ROOT = Path(r"D:\圖檔\twitter")
DATA_DIR = GALLERY_ROOT / ".tag_gallery"
THUMB_DIR = DATA_DIR / "thumbs"
MODEL_DIR = DATA_DIR / "models"
DB_PATH = DATA_DIR / "gallery.db"

VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4"}
NON_ARTIST_FOLDERS = {"1", "V", "W", "Pussy Spreading", ".tag_gallery"}

# Regex patterns for filename metadata extraction
RE_DATE_SEQ = re.compile(r"^((?:19|20)\d{2})(\d{2})(\d{2})_\d+")
RE_PIXIV = re.compile(r"^(?:\d+[_：:])?(\d{7,10})_p\d+", re.IGNORECASE)
RE_AI_SEED = re.compile(r"^\d{5}-\d{6,}")
RE_TWITTER_RAW = re.compile(r"^[A-Za-z0-9_-]{15}$")
RE_KONACHAN = re.compile(r"^Konachan\.com\s*-\s*(\d+)\s+(.*)$", re.IGNORECASE)


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
    conn = get_db()
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
        CREATE INDEX IF NOT EXISTS idx_images_ai_tagged ON images(ai_tagged);
        CREATE INDEX IF NOT EXISTS idx_images_media_type ON images(media_type);
        CREATE INDEX IF NOT EXISTS idx_images_mtime ON images(mtime DESC);

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

        CREATE INDEX IF NOT EXISTS idx_image_tags_tag ON image_tags(tag_id);
        CREATE INDEX IF NOT EXISTS idx_image_tags_img ON image_tags(image_id);
        """)
    conn.close()


def ensure_tag(conn: sqlite3.Connection, tag_cache: Dict[str, int], name: str, category: str) -> int:
    name_clean = name.strip()
    if not name_clean:
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
    tag_cache[name_clean] = tid
    return tid


def refresh_tag_counts(conn: sqlite3.Connection):
    conn.execute("""
        UPDATE tags
        SET post_count = (
            SELECT COUNT(*) FROM image_tags WHERE image_tags.tag_id = tags.id
        )
    """)
    conn.execute("DELETE FROM tags WHERE post_count <= 0")


def scan_library() -> Dict[str, Any]:
    """
    掃描 D:\\圖檔\\twitter 下所有檔案並同步至 SQLite，
    同時自動從「子資料夾名稱」與「檔名規則」提取畫師與來源標籤。
    """
    t0 = time.time()
    conn = get_db()

    # Load existing rel_paths
    existing = {}
    for row in conn.execute("SELECT id, rel_path, folder FROM images"):
        existing[row["rel_path"]] = (row["id"], row["folder"])

    tag_cache: Dict[str, int] = {}
    for row in conn.execute("SELECT id, name FROM tags"):
        tag_cache[row["name"]] = row["id"]

    found_paths = set()
    new_images = []
    # Pre-scan filesystem
    for root, dirs, files in os.walk(str(GALLERY_ROOT)):
        # Skip hidden metadata dir
        dirs[:] = [d for d in dirs if d != ".tag_gallery" and not d.startswith(".")]
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

            if rel_path_norm not in existing:
                full_path = os.path.join(root, fname)
                try:
                    st = os.stat(full_path)
                    size_bytes = st.st_size
                    mtime = st.st_mtime
                except OSError:
                    size_bytes = 0
                    mtime = 0.0

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

    with conn:
        if removed_paths:
            conn.executemany(
                "DELETE FROM images WHERE rel_path = ?",
                [(p,) for p in removed_paths]
            )

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
                elif folder == "Pussy Spreading":
                    tid = ensure_tag(conn, tag_cache, "spreading", "general")
                    tag_links.append((img_id, tid, 1.0, "folder"))

                # 2. Filename-based Date / Source / Konachan tags
                m_date = RE_DATE_SEQ.match(base)
                if m_date:
                    yr = m_date.group(1)
                    ym = f"{m_date.group(1)}-{m_date.group(2)}"
                    tid_y = ensure_tag(conn, tag_cache, f"{yr}年", "year")
                    tag_links.append((img_id, tid_y, 1.0, "filename"))

                if row["pixiv_id"]:
                    tid_p = ensure_tag(conn, tag_cache, "Pixiv來源", "source")
                    tag_links.append((img_id, tid_p, 1.0, "filename"))
                elif RE_AI_SEED.match(base):
                    tid_ai = ensure_tag(conn, tag_cache, "AI生成(檔名特徵)", "source")
                    tag_links.append((img_id, tid_ai, 1.0, "filename"))
                elif RE_TWITTER_RAW.match(base):
                    tid_tw = ensure_tag(conn, tag_cache, "Twitter原始檔名", "source")
                    tag_links.append((img_id, tid_tw, 1.0, "filename"))
                else:
                    m_kona = RE_KONACHAN.match(base)
                    if m_kona:
                        tid_k = ensure_tag(conn, tag_cache, "Konachan圖站", "source")
                        tag_links.append((img_id, tid_k, 1.0, "filename"))
                        raw_tags = m_kona.group(2).strip().split()
                        for rt in raw_tags:
                            if rt and rt != "sample":
                                tid_rt = ensure_tag(conn, tag_cache, rt, "general")
                                tag_links.append((img_id, tid_rt, 1.0, "filename"))

            if tag_links:
                conn.executemany(
                    "INSERT OR IGNORE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, ?, ?)",
                    tag_links
                )

        refresh_tag_counts(conn)

    total_count = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
    root_count = conn.execute("SELECT COUNT(*) FROM images WHERE folder = ''").fetchone()[0]
    ai_tagged_count = conn.execute("SELECT COUNT(*) FROM images WHERE ai_tagged = 1").fetchone()[0]
    conn.close()

    return {
        "total_images": total_count,
        "root_unorganized": root_count,
        "folder_organized": total_count - root_count,
        "ai_tagged": ai_tagged_count,
        "new_added": len(new_images),
        "removed": len(removed_paths),
        "elapsed_sec": round(time.time() - t0, 2)
    }
