"""Independent processing stages; transient failures never count as completion."""
import time

AI_MODEL_VERSION = "Camais03/camie-tagger-v2"


def record(conn, image_id, stage, status, *, error="", retry_at=None, model_version="", file_version=""):
    conn.execute("""INSERT INTO image_processing(image_id,stage,status,attempts,last_error,retry_at,model_version,file_version,updated_at)
        VALUES(?,?,?,1,?,?,?,COALESCE(NULLIF(?,''),(SELECT CAST(size_bytes AS TEXT)||':'||CAST(mtime_ns AS TEXT) FROM images WHERE id=?)),?) ON CONFLICT(image_id,stage) DO UPDATE SET
        status=excluded.status, attempts=image_processing.attempts+1,last_error=excluded.last_error,
        retry_at=excluded.retry_at,model_version=excluded.model_version,file_version=excluded.file_version,updated_at=excluded.updated_at""",
        (image_id, stage, status, str(error)[:1000], retry_at, model_version, file_version, image_id, time.time()))
    if stage == "ai":
        conn.execute("UPDATE images SET ai_tagged=? WHERE id=?", (int(status == "succeeded"), image_id))


def reset(conn, image_id):
    conn.execute("DELETE FROM image_processing WHERE image_id=?", (image_id,))
    conn.execute("UPDATE images SET ai_tagged=0,suggested_folder=NULL WHERE id=?", (image_id,))
