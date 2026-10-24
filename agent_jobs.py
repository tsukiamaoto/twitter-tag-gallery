"""Durable desktop-agent queue. Model waits never hold the gallery write lock."""
import contextlib
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

import database
from background_work import atomic_json
import prompt_store

ACTIVE = ("queued", "waiting", "running", "launching", "launch_uncertain")
REQUIRED_MODEL = "gemini-3.8-flash"


@contextlib.contextmanager
def connection():
    database.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database.DATA_DIR / "agent_jobs.sqlite3", timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS agent_jobs(
        id TEXT PRIMARY KEY, workflow TEXT NOT NULL, status TEXT NOT NULL,
        context_json TEXT NOT NULL, prompt_json TEXT NOT NULL, created_at REAL NOT NULL,
        updated_at REAL NOT NULL, conversation_id TEXT NOT NULL DEFAULT '',
        model_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
        result_json TEXT NOT NULL DEFAULT '{}');
      CREATE TABLE IF NOT EXISTS agent_items(
        job_id TEXT NOT NULL, image_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        error TEXT NOT NULL DEFAULT '', PRIMARY KEY(job_id,image_id));
      CREATE TABLE IF NOT EXISTS bridge_state(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL);
    """)
    try:
        yield conn
    finally:
        conn.close()


def token_path():
    return database.DATA_DIR / "antigravity_bridge.json"


def bridge_token(create=False):
    if token_path().exists():
        return json.loads(token_path().read_text(encoding="utf-8"))["token"]
    if not create:
        return ""
    token = secrets.token_urlsafe(32)
    atomic_json(token_path(), {"token": token}, durable=True)
    return token


def require_bridge(token):
    actual = bridge_token()
    if not actual or not secrets.compare_digest(actual, token or ""):
        raise PermissionError("橋接器認證失敗")


def heartbeat(model_id="", error=""):
    # Only the authenticated sidecar can publish runtime-derived model metadata.
    payload = {"last_seen": time.time(), "model_id": model_id, "model_verified": model_id == REQUIRED_MODEL,
               "error": str(error)[:1000]}
    with connection() as conn, conn:
        conn.execute("INSERT OR REPLACE INTO bridge_state VALUES(1,?)", (json.dumps(payload),))
    return payload


def bridge_status():
    with connection() as conn:
        row = conn.execute("SELECT payload FROM bridge_state WHERE id=1").fetchone()
    value = json.loads(row[0]) if row else {}
    value["connected"] = time.time() - value.get("last_seen", 0) < 30
    value["required_model"] = REQUIRED_MODEL
    return value


def queue_pending_identities():
    if not bridge_token():
        return
    conn = database.get_db()
    try:
        pending = conn.execute("SELECT artist_name,profile_urls_json FROM artist_identity_requests WHERE status='pending_model' ORDER BY updated_at LIMIT 5").fetchall()
    finally:
        conn.close()
    for row in pending:
        create("artist_identity", {"artist_name": row["artist_name"], "profile_urls": json.loads(row["profile_urls_json"])})


def _public(row):
    value = dict(row)
    value["agent_job_id"] = value["id"]
    for name in ("context", "prompt", "result"):
        value[name] = json.loads(value.pop(name + "_json"))
    return value


def get(job_id):
    with connection() as conn:
        row = conn.execute("SELECT * FROM agent_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("找不到 Antigravity 任務")
        value = _public(row)
        value["counts"] = {r[0]: r[1] for r in conn.execute("SELECT status,COUNT(*) FROM agent_items WHERE job_id=? GROUP BY status", (job_id,))}
    return value


def history(limit=30):
    with connection() as conn:
        rows = conn.execute("SELECT * FROM agent_jobs ORDER BY created_at DESC LIMIT ?", (min(limit, 100),)).fetchall()
        values = []
        for row in rows:
            value = _public(row)
            value["counts"] = {r[0]: r[1] for r in conn.execute("SELECT status,COUNT(*) FROM agent_items WHERE job_id=? GROUP BY status", (row["id"],))}
            values.append(value)
    return values


def create(workflow, context):
    if workflow not in prompt_store.DEFAULT_TASKS:
        raise ValueError("未知的查證工作")
    from integration_settings import require_tool
    require_tool("gallery_trace_publication_dates" if workflow == "publication" else "gallery_resolve_artist_identity")
    context = dict(context)
    if workflow == "publication":
        folder = context.get("folder", "__ALL__")
        if not isinstance(context.get("pad_digits", 4), int) or not 3 <= context.get("pad_digits", 4) <= 6:
            raise ValueError("流水號位數需為 3–6")
        context["pad_digits"] = context.get("pad_digits", 4)
        context["folder"] = folder
        # Repeated clicks must return the active job even after its last image
        # was verified but before its final rename/commit completed.
        with connection() as queue:
            for previous in queue.execute("SELECT * FROM agent_jobs WHERE status IN ('queued','waiting','running','launching','launch_uncertain') AND workflow=?", (workflow,)):
                if json.loads(previous["context_json"]) == context:
                    return get(previous["id"])
        conn = database.get_db()
        try:
            def pending_only(rows):
                metadata = {r[0]: r for r in conn.execute("""SELECT f.image_id,f.file_version,f.content_hash,a.retry_at,
                  EXISTS(SELECT 1 FROM publication_sources s WHERE s.image_id=f.image_id AND s.content_hash=f.content_hash),
                  i.size_bytes,i.mtime_ns,i.content_hash,i.rel_path FROM publication_files f JOIN images i ON i.id=f.image_id
                  LEFT JOIN publication_attempts a ON a.image_id=f.image_id""")}
                selected = []
                for row in rows:
                    meta = metadata.get(row["id"])
                    if meta:
                        same = meta[1] == f"{meta[5]}:{meta[6]}" and (not meta[7] or meta[2] == meta[7])
                        if same and (meta[4] or (meta[3] or 0) > time.time()):
                            from file_safety import gallery_path
                            try:
                                stat = gallery_path(database.GALLERY_ROOT, meta[8]).stat()
                                if meta[1] == f"{stat.st_size}:{stat.st_mtime_ns}":
                                    continue
                            except OSError:
                                pass
                    selected.append(row)
                return selected
            if folder in ("__ALL__", "__ALL_ARTIST_FOLDERS__"):
                rows = conn.execute("SELECT id,folder FROM images WHERE missing_since IS NULL ORDER BY id").fetchall()
                if folder == "__ALL_ARTIST_FOLDERS__":
                    rows = [r for r in rows if r["folder"] and r["folder"] not in database.NON_ARTIST_FOLDERS]
            else:
                rows = conn.execute("SELECT id,folder FROM images WHERE missing_since IS NULL AND folder=? ORDER BY id", ("" if folder == "__ROOT__" else folder,)).fetchall()
            rows = pending_only(rows)
        finally:
            conn.close()
        if not rows:
            raise ValueError("目前沒有待查新圖或到期重試項目；已確認的圖片會保留來源紀錄")
        context["folder"] = folder
    else:
        rows = []
    now = time.time()
    if workflow == "publication":
        atomic_json(database.DATA_DIR / "publication_settings.json", {"enabled": True, "pad_digits": context["pad_digits"]}, durable=True)
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        for previous in conn.execute("SELECT * FROM agent_jobs WHERE status IN ('queued','waiting','running','launching','launch_uncertain') AND workflow=?", (workflow,)):
            if json.loads(previous["context_json"]) == context:
                return get(previous["id"])
        job_id = uuid.uuid4().hex
        prompt = prompt_store.snapshot(workflow, {"job_id": job_id, **context})
        conn.execute("INSERT INTO agent_jobs(id,workflow,status,context_json,prompt_json,created_at,updated_at) VALUES(?,?,'queued',?,?,?,?)",
                     (job_id, workflow, json.dumps(context, ensure_ascii=False), json.dumps(prompt, ensure_ascii=False), now, now))
        conn.executemany("INSERT INTO agent_items(job_id,image_id) VALUES(?,?)", [(job_id, row["id"]) for row in rows])
    return get(job_id)


def claim():
    state = bridge_status()
    if not state.get("connected") or not state.get("model_verified"):
        return None
    with connection() as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        # A claimed conversation is reconciled by the sidecar, never relaunched blindly.
        if conn.execute("SELECT 1 FROM agent_jobs WHERE status IN ('running','launching','launch_uncertain')").fetchone():
            return None
        row = conn.execute("SELECT id FROM agent_jobs WHERE status IN ('queued','waiting') ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return None
        conn.execute("UPDATE agent_jobs SET status='launching',model_id=?,updated_at=? WHERE id=?", (state["model_id"], time.time(), row[0]))
    return get(row[0])


def launched(job_id, conversation_id):
    with connection() as conn, conn:
        conn.execute("UPDATE agent_jobs SET status='running',conversation_id=?,updated_at=?,error='' WHERE id=? AND status IN ('launching','launch_uncertain')", (conversation_id, time.time(), job_id))
    return get(job_id)


def launch_failed(job_id, error):
    with connection() as conn, conn:
        conn.execute("UPDATE agent_jobs SET status='launch_uncertain',error=?,updated_at=? WHERE id=? AND status='launching'", (str(error)[:1000], time.time(), job_id))


def require_running(job_id, workflow=None):
    job = get(job_id)
    if job["status"] != "running" or job["model_id"] != REQUIRED_MODEL:
        raise ValueError("工作未在已確認的 Gemini 3.8 Flash 執行，或已停止")
    if workflow and job["workflow"] != workflow:
        raise ValueError("工作流程不符")
    return job


def update_item(job_id, image_id, status, error=""):
    with connection() as conn, conn:
        if conn.execute("UPDATE agent_items SET status=?,error=? WHERE job_id=? AND image_id=?", (status, str(error)[:1000], job_id, image_id)).rowcount != 1:
            raise ValueError("圖片不在此工作範圍")


def batch(job_id, limit=50):
    job = require_running(job_id)
    with connection() as conn:
        ids = [r[0] for r in conn.execute("SELECT image_id FROM agent_items WHERE job_id=? AND status IN ('pending','review','verified') ORDER BY image_id LIMIT ?", (job_id, min(limit, 50)))]
    job["image_ids"] = ids
    return job


def finish(job_id, result=None):
    require_running(job_id)
    with connection() as conn, conn:
        failed = conn.execute("SELECT COUNT(*) FROM agent_items WHERE job_id=? AND status='failed'", (job_id,)).fetchone()[0]
        conn.execute("UPDATE agent_jobs SET status=?,result_json=?,updated_at=? WHERE id=? AND status='running'", ("partial" if failed else "succeeded", json.dumps(result or {}, ensure_ascii=False), time.time(), job_id))
    return get(job_id)


def control(job_id, action):
    job = get(job_id)
    with connection() as conn, conn:
        if action == "stop":
            conn.execute("UPDATE agent_jobs SET status='cancelled',updated_at=? WHERE id=? AND status IN ('queued','waiting','running','launching','launch_uncertain')", (time.time(), job_id))
        elif action == "resume":
            if job["status"] not in ("cancelled", "failed", "interrupted", "partial"):
                raise ValueError("此工作不能直接重試；啟動結果不明時需先核對 Antigravity 對話")
            # New IDs fence off late submissions from a cancelled desktop conversation.
            new_id, now = uuid.uuid4().hex, time.time()
            prompt = dict(job["prompt"])
            frozen = prompt.get("rendered_prompt", prompt["prompt"]).rsplit("\n\n工作資料（JSON）：\n", 1)[0]
            prompt["rendered_prompt"] = frozen + "\n\n工作資料（JSON）：\n" + json.dumps({"job_id": new_id, **job["context"]}, ensure_ascii=False)
            conn.execute("INSERT INTO agent_jobs(id,workflow,status,context_json,prompt_json,created_at,updated_at) VALUES(?,?,'queued',?,?,?,?)", (new_id, job["workflow"], json.dumps(job["context"], ensure_ascii=False), json.dumps(prompt, ensure_ascii=False), now, now))
            conn.execute("INSERT INTO agent_items(job_id,image_id) SELECT ?,image_id FROM agent_items WHERE job_id=? AND status!='done'", (new_id, job_id))
            job_id = new_id
        else:
            raise ValueError("未知的工作操作")
    return get(job_id)
