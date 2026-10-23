"""Gallery workflows for MCP, using the same services as the web application."""
import database
from background_work import background_status, job_history, request_cancel, update_job
from file_safety import gallery_path


def _images(image_ids, *, present=False):
    ids = list(dict.fromkeys(image_ids))
    conn = database.get_db()
    try:
        rows = conn.execute("SELECT * FROM images WHERE id IN (" + ",".join("?" for _ in ids) + ")", ids).fetchall()
    finally:
        conn.close()
    by_id = {row["id"]: row for row in rows}
    missing = [iid for iid in ids if iid not in by_id or present and by_id[iid]["missing_since"] is not None]
    if missing:
        raise ValueError("圖片 ID 不存在或已標記遺失：" + ", ".join(map(str, missing[:20])))
    return [by_id[iid] for iid in ids]


def tool_gallery_scan_library():
    update_job(stage="掃描圖庫索引")
    result = database.scan_library()
    if not result["scan_complete"]:
        result.update(status="failed", message="掃描不完整，已保留未出現的索引；請處理 errors 後重試")
    update_job(stage="掃描完成" if result["scan_complete"] else "掃描不完整")
    return result


def tool_gallery_search_images(**options):
    from gallery_query import search_images
    return search_images(**options)


def tool_gallery_get_image_details(image_id, tag_limit=100):
    row = _images([image_id])[0]
    conn = database.get_db()
    try:
        tags = [dict(tag) for tag in conn.execute("""SELECT t.id,t.name,t.category,t.zh_name,it.confidence,it.source
            FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE it.image_id=?
            ORDER BY it.confidence DESC,t.id LIMIT ?""", (image_id, tag_limit))]
        total_tags = conn.execute("SELECT COUNT(*) FROM image_tags WHERE image_id=?", (image_id,)).fetchone()[0]
        stages = [dict(stage) for stage in conn.execute("SELECT * FROM image_processing WHERE image_id=? ORDER BY stage", (image_id,))]
    finally:
        conn.close()
    path = gallery_path(database.GALLERY_ROOT, row["rel_path"])
    return {"image": dict(row), "full_path": str(path), "tags": tags, "total_tags": total_tags,
            "tags_truncated": total_tags > len(tags), "processing": stages}


def tool_gallery_update_image_tags(image_ids, add_tags=None, remove_tag_names=None):
    from gallery_tag_service import edit_tags
    return edit_tags(image_ids, add_tags or [], remove_tag_names or [], source="gemini_mcp")


def tool_gallery_analyze_images(image_ids, skip_completed=True):
    from processing_state import AI_MODEL_VERSION
    from vision_service import analyze
    rows = _images(image_ids, present=True)
    if any(row["media_type"] not in ("image", "gif", "video") for row in rows):
        raise ValueError("本機 AI 工具僅支援靜態圖片、GIF 及 MP4 影片；尚未開始打標")
    conn = database.get_db()
    try:
        ids = [row["id"] for row in rows]
        completed = {row["image_id"]: row["file_version"] for row in conn.execute(
            "SELECT image_id,file_version FROM image_processing WHERE stage='ai' AND status='succeeded' AND model_version=? AND image_id IN (" + ",".join("?" for _ in ids) + ")", [AI_MODEL_VERSION, *ids])}
    finally:
        conn.close()
    results, errors, skipped = [], [], []
    for index, row in enumerate(rows):
        update_job(stage="本機 AI 打標", processed_count=index, total_target=len(rows), current_file=row["filename"])
        try:
            stat = gallery_path(database.GALLERY_ROOT, row["rel_path"]).stat()
            version = f"{stat.st_size}:{stat.st_mtime_ns}"
            if skip_completed and completed.get(row["id"]) == version:
                skipped.append(row["id"])
                continue
            result = analyze(row["id"], report_progress=False)
            results.append({"image_id": row["id"], **result})
        except Exception as error:
            errors.append({"image_id": row["id"], "error": str(error)[:1000]})
    update_job(stage="AI 打標完成" if not errors else "部分圖片打標失敗", processed_count=len(rows), total_target=len(rows))
    return {"status": "succeeded" if not errors else "failed", "message": f"完成 {len(results)} 張、略過 {len(skipped)} 張、失敗 {len(errors)} 張",
            "processed": len(results), "skipped": skipped, "errors": errors, "results": results}


def tool_gallery_move_images(image_ids, target_folder, auto_rename=True, execute_move=False):
    from gallery_service import plan_image_moves, move_images_to_folder
    _images(image_ids, present=True)
    if not execute_move:
        plan = plan_image_moves(image_ids, target_folder, auto_rename)
        return {"preview": True, "planned_count": len(plan["entries"]), **plan}
    return {"preview": False, **move_images_to_folder(image_ids, target_folder, auto_rename)}


def tool_gallery_recover_file_operations():
    from file_operations import recover
    update_job(stage="檢查中斷檔案操作")
    result = recover(database.GALLERY_ROOT, database.DATA_DIR, database.get_db)
    if result["errors"]:
        result.update(status="failed", message="部分檔案操作無法復原，已保留日誌；請查看 errors")
    return result


def tool_gallery_list_rename_batches():
    from renamer import list_rename_logs
    return {"batches": list_rename_logs()}


def tool_gallery_undo_rename(batch_id):
    from renamer import undo_rename_batch
    return undo_rename_batch(batch_id)


def tool_gallery_get_jobs(limit=10):
    return {"active": background_status(), "jobs": job_history(limit)}


def tool_gallery_get_tool_run(run_id):
    from tool_runtime import get_run
    return {"run": get_run(run_id)}


def tool_gallery_cancel_job(job_id):
    return request_cancel(job_id)
