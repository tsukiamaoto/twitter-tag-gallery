"""Single-image vision analysis with short writes and independent stage state."""
import time
import database
from background_work import exclusive_job, update_job
from file_safety import gallery_path
from processing_state import record, AI_MODEL_VERSION


@exclusive_job("單張 AI 分析")
def analyze(image_id, *, report_progress=True):
    from ai_tagger import tagger_engine,preprocess_image_fast
    conn=database.get_db()
    try:
        row=conn.execute("SELECT id,rel_path,filename FROM images WHERE id=? AND missing_since IS NULL",(image_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise FileNotFoundError("找不到圖片")
    path=gallery_path(database.GALLERY_ROOT,row["rel_path"])
    try:
        tensor=preprocess_image_fast(str(path),512)
        if tensor is None:
            raise ValueError("圖片解碼失敗")
        if report_progress:
            update_job(stage="本機 AI 分析",processed_count=0,total_target=1,current_file=row["filename"])
        predictions,suggested=tagger_engine.predict_single(tensor)
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
            for name,category,confidence in predictions:
                tid=database.ensure_tag(c,cache,name,category)
                c.execute("INSERT OR IGNORE INTO image_tags(image_id,tag_id,confidence,source) VALUES(?,?,?,'ai')",(image_id,tid,round(confidence,4)))
            c.execute("UPDATE images SET suggested_folder=COALESCE(?,suggested_folder) WHERE id=?",(suggested,image_id))
            stat=path.stat()
            record(c,image_id,"ai","succeeded",model_version=AI_MODEL_VERSION,file_version=f"{stat.st_size}:{stat.st_mtime_ns}")
            database.refresh_tag_counts(c)
        database.run_db_write_with_retry(conn,write)
    finally:
        conn.close()
    return {"filename":row["filename"],"tags_added":len(predictions),"suggested_folder":suggested}
