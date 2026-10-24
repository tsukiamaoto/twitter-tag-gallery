"""Single-image vision analysis with short writes and independent stage state."""
import time
import database
from background_work import exclusive_job, update_job
from file_safety import gallery_path
from processing_state import record, AI_MODEL_VERSION


@exclusive_job("單張 AI 分析")
def analyze(image_id, *, report_progress=True, screenshot_bytes=None):
    from ai_tagger import tagger_engine,preprocess_image_fast
    conn=database.get_db()
    try:
        row=conn.execute("SELECT id,rel_path,filename,media_type FROM images WHERE id=? AND missing_since IS NULL",(image_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise FileNotFoundError("找不到圖片")
    path=gallery_path(database.GALLERY_ROOT,row["rel_path"])
    try:
        target_path = path
        if row["media_type"] == "video" or str(path).lower().endswith(".mp4"):
            from thumbnail_cache import ensure_video_screenshot
            ss_path = ensure_video_screenshot(image_id, path, max_size=720, override_bytes=screenshot_bytes)
            if ss_path is not None and ss_path.is_file():
                target_path = ss_path
        tensor=preprocess_image_fast(str(target_path),512)
        if tensor is None:
            raise ValueError("圖片解碼失敗")
        if report_progress:
            update_job(stage="本機 AI 分析",processed_count=0,total_target=1,current_file=row["filename"])
        predictions,suggested=tagger_engine.predict_single(tensor)
        from tag_policy import allows_tag
        predictions = [tag for tag in predictions if allows_tag(tag[1], "ai")]
    except Exception as error:
        conn=database.get_db()
        try:
            database.run_db_write_with_retry(conn,lambda c:record(c,image_id,"ai","failed",error=str(error),retry_at=time.time()+300,model_version=AI_MODEL_VERSION))
        finally:
            conn.close()
        raise
    conn=database.get_db()
    cache={}
    try:
        def write(c):
            c.execute("DELETE FROM image_tags WHERE image_id=? AND source='ai'",(image_id,))
            added = 0
            for name,category,confidence in predictions:
                tid=database.ensure_tag(c,cache,name,category,source="ai")
                if tid > 0:
                    added += c.execute("INSERT OR IGNORE INTO image_tags(image_id,tag_id,confidence,source) VALUES(?,?,?,'ai')",(image_id,tid,round(confidence,4))).rowcount
            c.execute("UPDATE images SET suggested_folder=COALESCE(?,suggested_folder) WHERE id=?",(suggested,image_id))
            stat=path.stat()
            record(c,image_id,"ai","succeeded",model_version=AI_MODEL_VERSION,file_version=f"{stat.st_size}:{stat.st_mtime_ns}")
            database.refresh_tag_counts(c)
            return added
        added = database.run_db_write_with_retry(conn,write)
    finally:
        conn.close()
    from artist_identity import resolve_image_folders
    identity = resolve_image_folders([image_id])
    suggested = identity["folders"].get(image_id)
    return {"filename":row["filename"],"tags_added":added,"suggested_folder":suggested,
            "pending_identity": len(identity["pending_image_ids"])}
