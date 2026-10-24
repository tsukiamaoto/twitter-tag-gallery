"""Shared filing operations used by HTTP, scheduler and MCP."""
import re
from pathlib import Path

import database
from background_work import exclusive_job
from file_safety import gallery_path, inside, UnsafePath


def artist_directory(root, target_folder):
    raw = target_folder.strip()
    if raw in (".", "..") or ".." in Path(raw).parts:
        raise UnsafePath("不可使用上層路徑作為目標資料夾")
    if raw in ("", "__ROOT__"):
        return Path(root).resolve(), ""
    clean = re.sub(r'[<>:"/\\|?*]', "_", raw).rstrip(". ")
    if not clean or clean.casefold() == ".tag_gallery" or clean.startswith("."):
        raise UnsafePath("不允許系統資料夾或空白名稱")
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?",clean) or len(clean)>200:
        raise UnsafePath("目標資料夾名稱無效或過長")
    return inside(root, Path(root) / clean), clean


def plan_image_moves(image_ids, target_folder, auto_rename=True):
    from renamer import get_folder_sequence_state, win_natural_key
    root = database.GALLERY_ROOT
    from artist_identity import known_folder
    target_folder = known_folder(target_folder) or target_folder
    target, folder = artist_directory(root, target_folder)
    conn = database.get_db()
    try:
        rows = []
        ids = list(dict.fromkeys(image_ids))
        for start in range(0,len(ids),500):
            chunk = ids[start:start+500]
            rows.extend(dict(r) for r in conn.execute("SELECT id,rel_path,filename,folder,ext FROM images WHERE id IN ("+",".join("?" for _ in chunk)+")",chunk))
        if len(rows) != len(ids):
            raise ValueError("包含不存在的圖片 ID；尚未搬移任何檔案")
        from publication_dates import settings as publication_settings
        numbered = auto_rename and folder and folder not in database.NON_ARTIST_FOLDERS and not publication_settings()["enabled"]
        base, sequence, label = get_folder_sequence_state(folder,conn=conn) if numbered else ("",0,"")
    finally:
        conn.close()
    entries, reserved = [], set()
    for row in sorted(rows,key=lambda r:win_natural_key(r["rel_path"])):
        if row["folder"].casefold() == folder.casefold():
            continue
        source = gallery_path(root,row["rel_path"])
        if not source.is_file():
            raise FileNotFoundError(f"圖片不存在：{row['filename']}")
        if numbered:
            sequence += 1
            filename = f"{base}_{sequence:04d}_{label}{row['ext']}"
        else:
            filename = row["filename"]
        dest = target / filename
        count = 1
        while (dest.exists() and dest != source) or str(dest).casefold() in reserved:
            dest = target / f"{Path(filename).stem}_{count}{Path(filename).suffix}"
            count += 1
        if dest == source:
            continue
        reserved.add(str(dest).casefold())
        entries.append({"id":row["id"],"old_rel_path":row["rel_path"],"new_rel_path":str(dest.relative_to(root.resolve())).replace("\\","/")})
    return {"entries": entries, "target_folder": folder}


@exclusive_job("圖片歸檔")
def move_images_to_folder(image_ids, target_folder, auto_rename=True, refresh_counts=True):
    from file_operations import execute
    plan = plan_image_moves(image_ids, target_folder, auto_rename)
    entries, folder = plan["entries"], plan["target_folder"]
    if not entries:
        return {"moved":0,"target_folder":folder}
    cache = {}
    def tags(conn, records):
        for entry in records:
            conn.execute("DELETE FROM image_tags WHERE image_id=? AND source='folder' AND tag_id IN (SELECT id FROM tags WHERE category='artist')",(entry["id"],))
        if folder and folder not in database.NON_ARTIST_FOLDERS:
            tid = database.ensure_tag(conn,cache,folder,"artist")
            conn.executemany("INSERT OR IGNORE INTO image_tags(image_id,tag_id,confidence,source) VALUES(?,?,1,'folder')",[(entry["id"],tid) for entry in records])
        if refresh_counts:
            database.refresh_tag_counts(conn)
    result = execute(database.GALLERY_ROOT,database.DATA_DIR,database.get_db,entries,kind="move",after_write=tags)
    from publication_dates import settings as publication_settings, rename_folder
    publication = publication_settings()
    publication_errors = []
    if auto_rename and publication["enabled"]:
        # New unknown images keep their original names; verified images retain their dates.
        affected = {folder, *(Path(e["old_rel_path"]).parts[0] if len(Path(e["old_rel_path"]).parts) > 1 else "" for e in entries)}
        for affected_folder in affected:
            try:
                rename_folder(affected_folder, publication["pad_digits"])
            except (OSError, ValueError, RuntimeError) as exc:
                publication_errors.append({"folder": affected_folder, "error": str(exc)})
    old_folders = {Path(e["old_rel_path"]).parts[0] for e in entries if len(Path(e["old_rel_path"]).parts) > 1}
    for old_f in old_folders:
        if old_f and old_f.casefold() != (folder or "").casefold():
            old_dir = database.GALLERY_ROOT / old_f
            if old_dir.exists() and old_dir.is_dir():
                try:
                    if not any(old_dir.iterdir()):
                        old_dir.rmdir()
                except Exception:
                    pass
    return {"moved":len(entries),"target_folder":folder,"operation_id":result["operation_id"], "publication_rename_errors": publication_errors}


def move_images_to_artist_folder(image_ids, target_folder=None, auto_rename=True, refresh_counts=True):
    """Automatic filing requires confirmed identity, including for an existing folder."""
    from artist_identity import resolve_image_folders
    result = resolve_image_folders(image_ids)
    groups = {}
    for iid, folder in result["folders"].items():
        groups.setdefault(folder, []).append(iid)
    moved, operations = 0, []
    for folder, ids in groups.items():
        value = move_images_to_folder(ids, folder, auto_rename, refresh_counts)
        moved += value["moved"]
        if value.get("operation_id"):
            operations.append(value["operation_id"])
    return {"moved": moved, "pending_identity": len(result["pending_image_ids"]),
            "target_folder": next(iter(groups)) if len(groups) == 1 else None, "operation_ids": operations}


@exclusive_job("按年月歸檔")
def organize_by_date():
    from file_operations import execute
    conn=database.get_db()
    try:
        rows=conn.execute("SELECT id,rel_path,filename FROM images WHERE folder='' AND missing_since IS NULL").fetchall()
    finally:
        conn.close()
    entries,reserved=[],set()
    for row in rows:
        match=database.RE_DATE_SEQ.match(Path(row["filename"]).stem)
        if not match:
            continue
        dest=gallery_path(database.GALLERY_ROOT,f"_按日期歸檔/{match[1]}-{match[2]}/{row['filename']}")
        original=dest;count=1
        while dest.exists() or str(dest).casefold() in reserved:
            dest=original.with_name(f"{original.stem}_{count}{original.suffix}");count+=1
        reserved.add(str(dest).casefold())
        entries.append({"id":row["id"],"old_rel_path":row["rel_path"],"new_rel_path":str(dest.relative_to(database.GALLERY_ROOT.resolve())).replace("\\","/")})
    if entries:
        execute(database.GALLERY_ROOT,database.DATA_DIR,database.get_db,entries,kind="move")
    return {"moved":len(entries),"target_folder":"_按日期歸檔"}
