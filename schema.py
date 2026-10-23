"""Idempotent, additive migrations. No filesystem walks or model imports."""
import time


def migrate(conn):
    if conn.execute("SELECT 1 FROM images LIMIT 1").fetchone() and not conn.execute("SELECT 1 FROM sqlite_master WHERE name='gallery_schema'").fetchone():
        import database
        from backup_service import snapshot
        import uuid
        snapshot(conn,database.DATA_DIR/"backups"/f"before_reliability_migration_{uuid.uuid4().hex[:8]}.db")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(images)")}
    for name, definition in {
        "missing_since": "REAL", "missing_scans": "INTEGER NOT NULL DEFAULT 0",
        "content_hash": "TEXT", "file_identity": "TEXT", "mtime_ns": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE images ADD COLUMN {name} {definition}")
    tag_idx = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_image_tags_tag'").fetchone()
    if tag_idx and "image_id" not in (tag_idx[0] or ""):
        conn.execute("DROP INDEX IF EXISTS idx_image_tags_tag")
    conn.executescript("""
        DROP INDEX IF EXISTS idx_image_tags_img;
        CREATE INDEX IF NOT EXISTS idx_image_tags_tag ON image_tags(tag_id, image_id);
        CREATE INDEX IF NOT EXISTS idx_images_folder_mtime ON images(folder, mtime);
        CREATE INDEX IF NOT EXISTS idx_images_hash ON images(content_hash);
        CREATE INDEX IF NOT EXISTS idx_images_identity ON images(file_identity);
        CREATE INDEX IF NOT EXISTS idx_images_present_mtime ON images(missing_since,mtime DESC,id DESC);
        CREATE TABLE IF NOT EXISTS gallery_schema(version INTEGER PRIMARY KEY, applied_at REAL);
        CREATE TABLE IF NOT EXISTS dirty_tag_counts(tag_id INTEGER PRIMARY KEY);
        CREATE TRIGGER IF NOT EXISTS dirty_tags_insert AFTER INSERT ON image_tags BEGIN
          INSERT OR IGNORE INTO dirty_tag_counts VALUES(NEW.tag_id);
        END;
        CREATE TRIGGER IF NOT EXISTS dirty_tags_delete AFTER DELETE ON image_tags BEGIN
          INSERT OR IGNORE INTO dirty_tag_counts VALUES(OLD.tag_id);
        END;
        CREATE TABLE IF NOT EXISTS image_processing (
          image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
          stage TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
          retry_at REAL, model_version TEXT NOT NULL DEFAULT '', file_version TEXT NOT NULL DEFAULT '',
          updated_at REAL NOT NULL, PRIMARY KEY(image_id,stage)
        );
        CREATE INDEX IF NOT EXISTS idx_processing_retry ON image_processing(stage,status,retry_at);
        CREATE TABLE IF NOT EXISTS file_operations (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, committed_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS semantic_rules(theme TEXT PRIMARY KEY,rule_hash TEXT NOT NULL);
    """)
    if len(conn.execute("PRAGMA index_info(idx_image_tags_tag)").fetchall()) < 2:
        conn.execute("DROP INDEX IF EXISTS idx_image_tags_tag")
        conn.execute("CREATE INDEX idx_image_tags_tag ON image_tags(tag_id, image_id)")
    if not conn.execute("SELECT 1 FROM gallery_schema WHERE version=1").fetchone():
        now = time.time()
        # A historical generic flag is not evidence of successful vision inference.
        conn.execute("""INSERT OR IGNORE INTO image_processing(image_id,stage,status,model_version,updated_at)
            SELECT DISTINCT it.image_id,'ai','succeeded','legacy',? FROM image_tags it JOIN tags t ON t.id=it.tag_id
            WHERE it.source='ai' AND t.category IN ('general','character','copyright','rating')""", (now,))
        conn.execute("UPDATE images SET ai_tagged=EXISTS(SELECT 1 FROM image_processing p WHERE p.image_id=images.id AND p.stage='ai' AND p.status='succeeded')")
        conn.execute("INSERT OR IGNORE INTO dirty_tag_counts SELECT id FROM tags")
        conn.execute("INSERT INTO gallery_schema VALUES(1,?)", (now,))
