# -*- coding: utf-8 -*-
"""
智慧順序鎖定改名引擎 (Windows Natural Sort + 兩階段無衝突改名 + 歷史還原)
格式：{YYYYMMDD}_{流水號}_{畫師名}.{ext} (例：20230515_0001_千種みのり.jpg)
保證：
1. 嚴格使用 Windows 檔案總管原生 API (StrCmpLogicalW) 進行自然排序，確保 _2 永遠在 _10 前面。
2. 若資料夾內已有部分檔案為 YYYYMMDD_NNN_畫師 格式（如 mignon、Gentsuki、ほうき星），
   優先固定這批已編號檔案的順序在最前面，未改名的新檔案依自然排序接續在後，絕不插隊！
3. 同一資料夾內的日期前綴保持單調遞增或統一基準日期，防止因個別檔案日期較早而排到前面打亂流水號。
4. 每次改名皆記錄於 SQLite 與 JSON 備份日誌，支援隨時「一鍵還原舊檔名」。
"""

import ctypes
import datetime
import functools
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

from database import GALLERY_ROOT, DATA_DIR, VALID_EXTENSIONS, NON_ARTIST_FOLDERS, get_db, get_preview_db, log_operation
from background_work import exclusive_job, atomic_json

RENAME_LOG_DIR = DATA_DIR / "rename_logs"
RENAME_LOG_DIR.mkdir(parents=True, exist_ok=True)

# Windows native StrCmpLogicalW for 100% identical Windows Explorer natural sort
_shlwapi = ctypes.windll.shlwapi
_shlwapi.StrCmpLogicalW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
_shlwapi.StrCmpLogicalW.restype = ctypes.c_int


def win_natural_cmp(a: str, b: str) -> int:
    return _shlwapi.StrCmpLogicalW(a, b)


win_natural_key = functools.cmp_to_key(win_natural_cmp)

# Regex patterns to detect existing naming structures
RE_ALREADY_TARGET = re.compile(r"^(20\d{6})_(\d{2,5})_(.+)$")
RE_SEQ_THEN_DATE = re.compile(r"^(\d{2,5})_(20\d{6})(?:[_\s].*)?$")
RE_ANY_DATE = re.compile(r"(20[12]\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))")


def clean_artist_display_name(folder_name: str) -> str:
    """清理資料夾名稱中不適合放進檔名的多餘前綴（如 CloBA - pixiv -> CloBA, [アーティスト] 青い桜 -> 青い桜）"""
    name = folder_name.strip()
    name = re.sub(r"^\[アーティスト\]\s*", "", name)
    name = re.sub(r"\s*-\s*pixiv$", "", name, flags=re.IGNORECASE)
    # Replace characters invalid in Windows filenames
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    return name.strip()


def detect_folder_base_date(files_info: List[Dict[str, Any]]) -> str:
    """
    自動推算該資料夾最適合的基準日期 (YYYYMMDD)：
    1. 若已有 YYYYMMDD_NNN_畫師 檔案，取其中最常出現或第一張的 YYYYMMDD。
    2. 若檔名中有 NNN_YYYYMMDD_畫師（如 001_20230610_machi.jpg），取第 1 張或眾數的 YYYYMMDD。
    3. 否則取資料夾中檔案修改時間 (mtime) 的中位數/最新有效日期。
    """
    for f in files_info:
        stem = os.path.splitext(f["filename"])[0]
        m1 = RE_ALREADY_TARGET.match(stem)
        if m1:
            return m1.group(1)
        m2 = RE_SEQ_THEN_DATE.match(stem)
        if m2:
            return m2.group(2)

    # Search any YYYYMMDD in first few filenames
    for f in files_info[:10]:
        m_any = RE_ANY_DATE.search(f["filename"])
        if m_any:
            return m_any.group(1)

    # Fallback to latest file modification date in that folder
    if files_info:
        max_mtime = max((f.get("mtime") or 0) for f in files_info)
        if max_mtime > 0:
            return datetime.datetime.fromtimestamp(max_mtime).strftime("%Y%m%d")

    return datetime.date.today().strftime("%Y%m%d")


def sort_folder_files_preserving_order(files_info: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    對單一資料夾內的檔案進行嚴格排序：
    - 若該資料夾已有部分檔案是 `YYYYMMDD_NNN_畫師`（例如 mignon 有 175 張已編號，但多了 1 張 107999142_p0.jpg），
      則已編號的檔案依 (日期, 流水號) 排在最前面，未編號的檔案依 Windows 自然排序 (StrCmpLogicalW) 接在後面！
    - 否則全部依 Windows 檔案總管自然排序 (StrCmpLogicalW) 排列（若含子資料夾則先依子資料夾自然排序）。
    """
    already_numbered = []
    unnumbered = []

    for item in files_info:
        stem = os.path.splitext(item["filename"])[0]
        m = RE_ALREADY_TARGET.match(stem)
        if m:
            date_str = m.group(1)
            seq_num = int(m.group(2))
            already_numbered.append((date_str, seq_num, item))
        else:
            unnumbered.append(item)

    if already_numbered:
        # Sort numbered by (seq_num, date_str, natural filename) so 001..175 stays 1..175
        already_numbered.sort(key=lambda x: (x[1], x[0], win_natural_key(x[2]["filename"])))
        sorted_numbered = [x[2] for x in already_numbered]
        unnumbered.sort(key=lambda x: win_natural_key(x["rel_path"]))
        return sorted_numbered + unnumbered
    else:
        return sorted(files_info, key=lambda x: win_natural_key(x["rel_path"]))


def generate_rename_plan_for_folder(
    folder_name: str,
    pad_digits: int = 4,
    custom_date: Optional[str] = None,
    custom_artist: Optional[str] = None,
    keep_existing_batch_dates: bool = True,
) -> Dict[str, Any]:
    """
    產生指定資料夾（或根目錄 '__ROOT__'）的改名預覽計畫，不會直接修改檔案。
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, rel_path, filename, folder, ext, mtime FROM images WHERE folder = ?",
            ("" if folder_name == "__ROOT__" else folder_name,),
        ).fetchall()
    finally:
        conn.close()
    return generate_rename_plan_from_rows(
        folder_name, rows, pad_digits, custom_date, custom_artist, keep_existing_batch_dates
    )


def generate_rename_plan_from_rows(
    folder_name: str,
    rows,
    pad_digits: int = 4,
    custom_date: Optional[str] = None,
    custom_artist: Optional[str] = None,
    keep_existing_batch_dates: bool = True,
) -> Dict[str, Any]:
    """Build the same rename plan from an already-fetched database snapshot."""

    files_info = [dict(r) for r in rows]
    if not files_info:
        return {"folder": folder_name, "total": 0, "changed_count": 0, "items": []}

    sorted_files = sort_folder_files_preserving_order(files_info)

    if folder_name == "__ROOT__":
        # For root files: standardize to YYYYMMDD_00001.ext while preserving exact chronological / natural order
        plan_items = []
        changed_cnt = 0
        for idx, item in enumerate(sorted_files, start=1):
            stem = os.path.splitext(item["filename"])[0]
            m_date = RE_ANY_DATE.search(stem)
            if m_date:
                d_str = m_date.group(1)
            elif item["mtime"] > 0:
                d_str = datetime.datetime.fromtimestamp(item["mtime"]).strftime("%Y%m%d")
            else:
                d_str = "20230730"

            seq_str = str(idx).zfill(max(5, pad_digits))
            if custom_artist and custom_artist.strip():
                art_clean = clean_artist_display_name(custom_artist)
                new_fname = f"{d_str}_{seq_str}_{art_clean}{item['ext']}"
            else:
                new_fname = f"{d_str}_{seq_str}{item['ext']}"

            is_changed = new_fname != item["filename"]
            if is_changed:
                changed_cnt += 1
            plan_items.append({
                "id": item["id"],
                "old_rel_path": item["rel_path"],
                "old_filename": item["filename"],
                "new_filename": new_fname,
                "changed": is_changed,
                "seq": idx,
            })
        return {
            "folder": "__ROOT__",
            "artist_name": custom_artist or "(根目錄未歸檔)",
            "base_date": "依檔案日期",
            "total": len(plan_items),
            "changed_count": changed_cnt,
            "items": plan_items,
        }

    # For a specific Artist / Theme subfolder:
    artist_label = clean_artist_display_name(custom_artist if custom_artist else folder_name)
    base_date = (
        custom_date.strip()
        if (custom_date and re.match(r"^\d{8}$", custom_date.strip()))
        else detect_folder_base_date(sorted_files)
    )

    total_files = len(sorted_files)
    actual_pad = max(pad_digits, len(str(total_files)))

    plan_items = []
    changed_cnt = 0
    last_date_used = base_date

    for idx, item in enumerate(sorted_files, start=1):
        stem = os.path.splitext(item["filename"])[0]
        m_existing = RE_ALREADY_TARGET.match(stem)

        if keep_existing_batch_dates and m_existing and not custom_date:
            d_candidate = m_existing.group(1)
            # Only use d_candidate if it is >= last_date_used so alphabetical sort never breaks sequence order!
            if d_candidate >= last_date_used:
                last_date_used = d_candidate

        d_str = custom_date.strip() if (custom_date and re.match(r"^\d{8}$", custom_date.strip())) else last_date_used
        seq_str = str(idx).zfill(actual_pad)
        new_fname = f"{d_str}_{seq_str}_{artist_label}{item['ext']}"

        # Preserve relative subdirectory inside the artist folder if any (e.g. HAONI/Part 1/...)
        old_rel = item["rel_path"]
        parent_rel = os.path.dirname(old_rel)
        new_rel = f"{parent_rel}/{new_fname}" if parent_rel else new_fname

        is_changed = new_fname != item["filename"]
        if is_changed:
            changed_cnt += 1

        plan_items.append({
            "id": item["id"],
            "old_rel_path": old_rel,
            "new_rel_path": new_rel,
            "old_filename": item["filename"],
            "new_filename": new_fname,
            "changed": is_changed,
            "seq": idx,
        })

    return {
        "folder": folder_name,
        "artist_name": artist_label,
        "base_date": base_date,
        "pad_digits": actual_pad,
        "total": len(plan_items),
        "changed_count": changed_cnt,
        "items": plan_items,
    }


def generate_rename_preview(
    folder: str,
    pad_digits: int = 4,
    custom_date: Optional[str] = None,
    custom_artist: Optional[str] = None,
) -> Dict[str, Any]:
    """Fetch once, return the connection, then calculate the preview in memory."""
    all_folders = folder == "__ALL_ARTIST_FOLDERS__"
    with get_preview_db() as conn:
        if all_folders:
            rows = conn.execute(
                "SELECT id, rel_path, filename, folder, ext, mtime FROM images "
                "WHERE folder != '' ORDER BY folder ASC, id ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, rel_path, filename, folder, ext, mtime FROM images WHERE folder = ?",
                ("" if folder == "__ROOT__" else folder,),
            ).fetchall()

    if not all_folders:
        plan = generate_rename_plan_from_rows(folder, rows, pad_digits, custom_date, custom_artist)
        return {
            "mode": "single_folder",
            "folder": plan["folder"],
            "artist_name": plan.get("artist_name", ""),
            "base_date": plan.get("base_date", ""),
            "total": plan["total"],
            "changed_count": plan["changed_count"],
            "items": plan["items"][:300],
        }

    grouped = {}
    for row in rows:
        if row["folder"] not in NON_ARTIST_FOLDERS:
            grouped.setdefault(row["folder"], []).append(row)
    summary_items = []
    total_files = total_changed = 0
    for folder_name, folder_rows in grouped.items():
        # All-folder mode preserves the existing per-folder date/artist settings.
        plan = generate_rename_plan_from_rows(folder_name, folder_rows, pad_digits)
        first_it, last_it = plan["items"][0], plan["items"][-1]
        total_files += plan["total"]
        total_changed += plan["changed_count"]
        summary_items.append({
            "folder": folder_name,
            "base_date": plan["base_date"],
            "total": plan["total"],
            "changed_count": plan["changed_count"],
            "sample_first_old": first_it["old_filename"],
            "sample_first_new": first_it["new_filename"],
            "sample_last_old": last_it["old_filename"],
            "sample_last_new": last_it["new_filename"],
        })
    return {
        "mode": "all_folders",
        "folder_count": len(summary_items),
        "total": total_files,
        "changed_count": total_changed,
        "folders": summary_items,
    }


@exclusive_job("批次改名")
def execute_rename_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    from file_operations import execute
    from file_safety import gallery_path
    items = [item for item in plan.get("items", []) if item.get("changed")]
    entries = []
    source_paths = {str(gallery_path(GALLERY_ROOT, item["old_rel_path"])).casefold() for item in items}
    reserved = set()
    for item in items:
        old = gallery_path(GALLERY_ROOT, item["old_rel_path"])
        target = gallery_path(GALLERY_ROOT, str(Path(item["old_rel_path"]).parent / item["new_filename"]))
        count = 1
        original = target
        while (target.exists() and str(target).casefold() not in source_paths) or str(target).casefold() in reserved:
            target = original.with_name(f"{original.stem}_dup{count}{original.suffix}")
            count += 1
        reserved.add(str(target).casefold())
        entries.append({"id":item["id"],"old_rel_path":item["old_rel_path"],"old_filename":old.name,
                        "new_rel_path":str(target.relative_to(GALLERY_ROOT.resolve())).replace("\\","/"),"new_filename":target.name})
    if not entries:
        return {"renamed":0,"message":"所有檔案皆已符合目標命名格式，無需更動"}
    result = execute(GALLERY_ROOT, DATA_DIR, get_db, entries, kind="rename")
    batch_id = result["operation_id"]
    log = {"batch_id":batch_id,"timestamp":datetime.datetime.now().isoformat(timespec="seconds"),
           "folder":plan.get("folder"),"count":len(entries),"entries":entries}
    atomic_json(RENAME_LOG_DIR / f"rename_{batch_id}.json", log)
    log_operation(category="rename",action="批次改名",summary=f"完成 {len(entries)} 張圖片改名",affected_count=len(entries),details={"batch_id":batch_id})
    return {"batch_id":batch_id,"folder":plan.get("folder"),"renamed":len(entries),"log_file":result["journal"]}


def list_rename_logs() -> List[Dict[str, Any]]:
    logs = []
    for p in sorted(RENAME_LOG_DIR.glob("rename_*.json"), reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("undone"):
                continue
            logs.append({
                "batch_id": data["batch_id"],
                "timestamp": data["timestamp"],
                "folder": data.get("folder", ""),
                "count": data.get("count", 0),
            })
        except Exception:
            continue
    known = {item["batch_id"] for item in logs}
    for path in (DATA_DIR / "file_journals").glob("*.json"):
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
            if journal["kind"] == "rename" and journal["status"] == "committed" and journal["id"] not in known:
                logs.append({"batch_id":journal["id"],"timestamp":datetime.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),"folder":"(復原日誌)","count":len(journal["entries"])})
        except (OSError,ValueError,KeyError):
            continue
    return sorted(logs,key=lambda item:item["batch_id"],reverse=True)[:30]


@exclusive_job("還原批次改名")
def undo_rename_batch(batch_id: str) -> Dict[str, Any]:
    from file_operations import execute
    if not re.fullmatch(r"[A-Za-z0-9_-]+",batch_id):
        raise ValueError("無效的批次 ID")
    log_file = RENAME_LOG_DIR / f"rename_{batch_id}.json"
    if log_file.exists():
        data = json.loads(log_file.read_text(encoding="utf-8"))
    else:
        data = json.loads((DATA_DIR / "file_journals" / f"{batch_id}.json").read_text(encoding="utf-8"))
        if data.get("kind") != "rename" or data.get("status") != "committed":
            raise ValueError("此批次尚未完成，請先執行檔案復原")
    if data.get("undone"):
        return {"restored":0,"folder":data.get("folder")}
    entries = [{"id":entry["id"],"old_rel_path":entry["new_rel_path"],"new_rel_path":entry["old_rel_path"]} for entry in data.get("entries",[])]
    execute(GALLERY_ROOT, DATA_DIR, get_db, entries, kind="undo")
    data["undone"] = True
    atomic_json(log_file,data)
    log_operation(category="rename",action="還原批次改名",summary=f"還原 {len(entries)} 張圖片",affected_count=len(entries),details={"batch_id":batch_id})
    return {"restored":len(entries),"folder":data.get("folder")}


def get_folder_sequence_state(folder_name: str, conn=None) -> Tuple[str, int, str]:
    """
    取得指定繪師資料夾目前的 (base_date, max_seq, artist_label)，
    供批次歸檔在記憶體中連續遞增流水號，避免每移一張圖就重新查詢一次資料庫。
    """
    own_conn = False
    if conn is None:
        conn = get_db()
        own_conn = True
    try:
        rows = conn.execute(
            "SELECT id, rel_path, filename, mtime FROM images WHERE folder = ?",
            (folder_name,),
        ).fetchall()
    finally:
        if own_conn:
            conn.close()

    files_info = [dict(r) for r in rows]
    artist_label = clean_artist_display_name(folder_name)
    base_date = detect_folder_base_date(files_info)

    max_seq = len(files_info)
    for f in files_info:
        stem = os.path.splitext(f["filename"])[0]
        m = RE_ALREADY_TARGET.match(stem)
        if m:
            seq_val = int(m.group(2))
            if seq_val > max_seq:
                max_seq = seq_val
            if m.group(1) > base_date:
                base_date = m.group(1)

    return base_date, max_seq, artist_label


def get_next_sequence_filename_for_folder(folder_name: str, ext: str, pad_digits: int = 4) -> str:
    """
    當把新圖片「歸檔移入繪師資料夾」時，自動算出該資料夾最後一號 + 1 的檔名：
    例如資料夾內最後一張是 20230515_0336_千種みのり.jpg，
    新移入的圖片就會自動命名為 20230515_0337_千種みのり.jpg（接在最後，絕不打亂順序！）
    """
    base_date, max_seq, artist_label = get_folder_sequence_state(folder_name)
    next_seq = max_seq + 1
    actual_pad = max(pad_digits, len(str(next_seq)))
    return f"{base_date}_{str(next_seq).zfill(actual_pad)}_{artist_label}{ext}"
