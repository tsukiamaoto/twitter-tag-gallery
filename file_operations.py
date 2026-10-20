"""Recoverable filesystem batches: one durable plan, buffered events, one DB commit.

Recovery identifies files from the durable plan and their filesystem identity,
so a process can die between rename and event append without losing its place.
No per-file fsync and no SQL transaction is held during filesystem operations.
"""
import json
import os
import time
import uuid
import re
from pathlib import Path

from background_work import atomic_json, exclusive_job, update_job
from file_safety import gallery_path, inside, validate_source


def _signature(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def _paths(root, entry, source_root=None):
    old_root = Path(source_root) if source_root else root
    return (gallery_path(old_root, entry["old_rel_path"]),
            gallery_path(root, entry["new_rel_path"]),
            gallery_path(old_root, entry["temp_rel_path"]))


def _rollback(root, entries, source_root=None):
    staged = []
    # Stage everything again first: original and destination names may overlap.
    for entry in entries:
        old, new, tmp = _paths(root, entry, source_root)
        identities = [entry["identity"],entry.get("copy_identity")]
        matches = [path for path in (tmp, new, old) if path.is_file() and _signature(path) in identities]
        if not matches:
            raise RuntimeError(f"無法辨識原檔案，保留復原計畫：{entry['old_rel_path']}")
        if old in matches and tmp not in matches and not entry.get("copy_identity"):
            continue  # Unstaged originals must not be moved just to roll back a batch.
        current = matches[0]
        if len(matches) > 1 and entry.get("copy_identity"):
            for duplicate in matches[1:]:
                if duplicate != current:
                    duplicate.unlink()
        copy = entry.get("copy_rel_path")
        if copy:
            copy_path = gallery_path(root,copy)
            if copy_path.is_file():
                copy_path.unlink()
        if current != tmp:
            if tmp.exists():
                raise FileExistsError(f"復原暫存檔已存在：{tmp.name}")
            os.rename(current, tmp)
        staged.append((old, tmp))
    for old, tmp in staged:
        old.parent.mkdir(parents=True, exist_ok=True)
        if old.exists():
            raise FileExistsError(f"原位置已有其他檔案，停止復原：{old.name}")
        os.rename(tmp, old)


def _committed(get_db, operation_id):
    conn = get_db()
    try:
        return conn.execute("SELECT 1 FROM file_operations WHERE id=?", (operation_id,)).fetchone() is not None
    finally:
        conn.close()


@exclusive_job("檔案操作復原")
def recover(root, data_dir, get_db):
    recovered, errors = [], []
    directory = Path(data_dir) / "file_journals"
    for path in sorted(directory.glob("*.json")):
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
            events=path.with_suffix(".events")
            if events.exists() and plan["status"] not in ("committed","rolled_back"):
                for line in events.read_text(encoding="utf-8").splitlines():
                    try:
                        event=json.loads(line)
                        if "copy_identity" in event:
                            plan["entries"][event["index"]]["copy_identity"]=event["copy_identity"]
                    except (ValueError,KeyError,IndexError):
                        continue  # An interrupted final append is recoverable from the plan.
            if plan["status"] in ("committed", "rolled_back"):
                if plan["status"]=="committed":
                    _cleanup_sources(root,plan["entries"],plan.get("source_root"))
                continue
            source_root = plan.get("source_root")
            if source_root:
                validate_source(root, source_root)
            if _committed(get_db, plan["id"]):
                plan["status"] = "committed"
                _cleanup_sources(root,plan["entries"],source_root)
            else:
                _rollback(Path(root), plan["entries"], source_root)
                plan["status"] = "rolled_back"
            atomic_json(path, plan, durable=True)
            recovered.append(plan["id"])
        except Exception as error:
            errors.append({"journal": path.name, "error": str(error)})
    marker=directory/"RECOVERY_REQUIRED"
    if errors:
        atomic_json(marker,{"errors":errors},durable=True)
    else:
        marker.unlink(missing_ok=True)
        (directory/"PENDING").unlink(missing_ok=True)
    return {"recovered": recovered, "errors": errors}


def _cleanup_sources(root, entries, source_root):
    if not source_root:
        return
    for entry in entries:
        _, _, tmp = _paths(root,entry,source_root)
        if entry.get("copy_identity") and tmp.is_file() and _signature(tmp) == entry["identity"]:
            tmp.unlink()


@exclusive_job("檔案批次操作")
def execute(root, data_dir, get_db, entries, *, kind="rename", operation_id=None, source_root=None, after_write=None):
    from database import run_db_write_with_retry
    root = Path(root).resolve()
    if source_root is not None:
        source_root = str(validate_source(root, source_root))
    operation_id = operation_id or (time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    if not re.fullmatch(r"[A-Za-z0-9_-]+",operation_id):
        raise ValueError("無效的批次 ID")
    directory = Path(data_dir) / "file_journals"
    directory.mkdir(parents=True, exist_ok=True)
    if (directory/"PENDING").exists():
        previous=recover(root,data_dir,get_db)
        if previous["errors"]:
            raise RuntimeError("前次檔案操作尚未復原，已停止新的操作")
    if (directory/"RECOVERY_REQUIRED").exists():
        raise RuntimeError("尚有未完成的檔案復原，請先到設定執行復原檢查")
    manifest = directory / f"{operation_id}.json"
    if manifest.exists():
        raise FileExistsError("批次操作 ID 已存在")
    entries = [dict(entry) for entry in entries]
    reserved = set()
    resolved_paths = []
    parents={}
    def checked_path(base,relative):
        relative=Path(relative)
        if relative.is_absolute() or relative.drive or ".." in relative.parts:
            raise ValueError("不允許圖庫外路徑")
        key=(str(base),str(relative.parent))
        if key not in parents:
            parents[key]=inside(base,base/relative.parent,allow_root=True)
        path=parents[key]/relative.name
        if path.is_symlink():
            raise ValueError("檔案操作不支援符號連結")
        return path
    for index, entry in enumerate(entries):
        old_root = Path(source_root) if source_root else root
        old = checked_path(old_root, entry["old_rel_path"])
        new = checked_path(root, entry["new_rel_path"])
        key = str(new).casefold()
        if key in reserved:
            raise ValueError("計畫包含重複目的路徑")
        reserved.add(key)
        entry["identity"] = _signature(old)
        entry["temp_rel_path"] = str(old.with_name(f"__gallery_{operation_id}_{index}{old.suffix}").relative_to(old_root)).replace("\\", "/")
        entry["copy_rel_path"] = str(new.with_name(f".__gallery_copy_{operation_id}_{index}").relative_to(root)).replace("\\", "/")
        tmp = old.parent/Path(entry["temp_rel_path"]).name
        resolved_paths.append((old,new,tmp))
        if tmp.exists():
            raise FileExistsError("暫存路徑已存在")
    source_paths = {str(paths[0]).casefold() for paths in resolved_paths}
    for _,new,_ in resolved_paths:
        if new.exists() and str(new).casefold() not in source_paths:
            raise FileExistsError(f"目的檔案已存在：{new.name}")
    plan = {"id": operation_id, "kind": kind, "status": "prepared", "source_root": source_root, "entries": entries}
    atomic_json(directory/"PENDING",{"id":operation_id},durable=True)
    atomic_json(manifest, plan, durable=True)
    committed = False
    parent_devices={}
    try:
        with manifest.with_suffix(".events").open("a", encoding="utf-8", buffering=65536) as events:
            for phase in ("stage", "final"):
                for index, entry in enumerate(entries):
                    old,new,tmp = resolved_paths[index]
                    src, dest = (old, tmp) if phase == "stage" else (tmp, new)
                    if phase=="stage" and _signature(src)!=entry["identity"]:
                        raise OSError("原檔案已被其他程式修改，停止操作")
                    if dest.parent not in parent_devices:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        parent_devices[dest.parent]=dest.parent.stat().st_dev
                    if os.name!="nt" and dest.exists():
                        raise FileExistsError(f"檔案操作遇到新衝突：{dest.name}")
                    # Cross-volume sources stay staged until the destination and DB commit are durable.
                    if entry["identity"][0] != parent_devices[dest.parent]:
                        import shutil
                        from file_safety import content_hash
                        copy = gallery_path(root,entry["copy_rel_path"])
                        if copy.exists():
                            raise FileExistsError("複製暫存路徑已有檔案")
                        shutil.copy2(src, copy)
                        if content_hash(src) != content_hash(copy):
                            raise OSError("跨磁碟複製驗證失敗")
                        with copy.open("rb+") as saved:
                            os.fsync(saved.fileno())
                        entry["copy_identity"] = _signature(copy)
                        events.write(json.dumps({"index":index,"copy_identity":entry["copy_identity"]})+"\n")
                        events.flush()
                        os.fsync(events.fileno())
                        os.rename(copy,dest)
                    else:
                        os.rename(src, dest)
                    events.write(json.dumps({"index": index, "phase": phase}) + "\n")
                    if index % 64 == 0:
                        events.flush()
                        update_job(stage=phase, processed_count=index, total_target=len(entries))
                events.flush()
        conn = get_db()
        try:
            def commit(c):
                # Temporary DB names make swaps safe under the UNIQUE rel_path constraint.
                for index, entry in enumerate(entries):
                    if entry.get("id") is not None:
                        c.execute("UPDATE images SET rel_path=? WHERE id=?", (f".tag_gallery/pending/{operation_id}/{index}",entry["id"]))
                for entry in entries:
                    if entry.get("id") is not None:
                        new = Path(entry["new_rel_path"])
                        folder = new.parts[0] if len(new.parts) > 1 else ""
                        c.execute("UPDATE images SET rel_path=?,filename=?,folder=?,suggested_folder=NULL WHERE id=?",
                                  (entry["new_rel_path"], new.name, folder, entry["id"]))
                if after_write is not None:
                    after_write(c, entries)
                c.execute("INSERT INTO file_operations VALUES(?,?,?)", (operation_id, kind, time.time()))
            run_db_write_with_retry(conn, commit)
            committed = True
        finally:
            conn.close()
        plan["status"] = "committed"
        atomic_json(manifest, plan, durable=True)
        _cleanup_sources(root,entries,source_root)
        (directory/"PENDING").unlink(missing_ok=True)
    except BaseException:
        if not committed:
            try:
                _rollback(root, entries, source_root)
                plan["status"] = "rolled_back"
                atomic_json(manifest, plan, durable=True)
                (directory/"PENDING").unlink(missing_ok=True)
            except Exception as recovery_error:
                plan["status"] = "recovery_required"
                plan["error"] = str(recovery_error)
                atomic_json(manifest, plan, durable=True)
                atomic_json(directory/"RECOVERY_REQUIRED",{"error":str(recovery_error)},durable=True)
        raise
    return {"operation_id": operation_id, "entries": entries, "journal": str(manifest)}
