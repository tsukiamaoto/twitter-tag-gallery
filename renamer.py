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

from database import GALLERY_ROOT, DATA_DIR, VALID_EXTENSIONS, NON_ARTIST_FOLDERS, get_db

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
    if folder_name == "__ROOT__":
        rows = conn.execute(
            "SELECT id, rel_path, filename, folder, ext, mtime FROM images WHERE folder = ''"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, rel_path, filename, folder, ext, mtime FROM images WHERE folder = ?",
            (folder_name,),
        ).fetchall()
    conn.close()

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


def execute_rename_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    """
    安全執行改名計畫（採用兩階段暫存檔名，防止如 001->002 與既有 002 撞名），
    並同步更新 SQLite 資料庫與寫入還原日誌。
    """
    items_to_change = [it for it in plan.get("items", []) if it.get("changed")]
    if not items_to_change:
        return {"renamed": 0, "message": "所有檔案皆已符合目標命名格式，無需更動"}

    batch_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    stage1_records = []

    # Phase 1: Rename old -> temporary unique name on disk
    for it in items_to_change:
        old_rel = it["old_rel_path"]
        old_full = GALLERY_ROOT / old_rel
        if not old_full.exists():
            continue

        parent_dir = old_full.parent
        tmp_name = f"__renaming_{batch_id}_{it['id']}{old_full.suffix}"
        tmp_full = parent_dir / tmp_name

        try:
            os.rename(str(old_full), str(tmp_full))
            stage1_records.append((it, tmp_full))
        except Exception as e:
            # Rollback any already staged files if error occurs
            for prev_it, prev_tmp in stage1_records:
                try:
                    os.rename(str(prev_tmp), str(GALLERY_ROOT / prev_it["old_rel_path"]))
                except Exception:
                    pass
            raise RuntimeError(f"檔案重新命名失敗 ({old_rel}): {e}")

    # Phase 2: Rename temporary name -> final target filename and update DB
    conn = get_db()
    history_entries = []
    db_updates = []

    for it, tmp_full in stage1_records:
        parent_dir = tmp_full.parent
        target_name = it["new_filename"]
        target_full = parent_dir / target_name

        if target_full.exists():
            stem = target_full.stem
            ext = target_full.suffix
            c = 1
            while target_full.exists():
                target_full = parent_dir / f"{stem}_dup{c}{ext}"
                c += 1
            target_name = target_full.name

        os.rename(str(tmp_full), str(target_full))
        new_rel = str(target_full.relative_to(GALLERY_ROOT)).replace("\\", "/")

        history_entries.append({
            "id": it["id"],
            "old_rel_path": it["old_rel_path"],
            "old_filename": it["old_filename"],
            "new_rel_path": new_rel,
            "new_filename": target_name,
        })
        db_updates.append((new_rel, target_name, it["id"]))

    with conn:
        conn.executemany(
            "UPDATE images SET rel_path = ?, filename = ? WHERE id = ?",
            db_updates,
        )
    conn.close()

    # Save undo log
    log_file = RENAME_LOG_DIR / f"rename_{batch_id}.json"
    log_data = {
        "batch_id": batch_id,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "folder": plan.get("folder"),
        "count": len(history_entries),
        "entries": history_entries,
    }
    log_file.write_text(json.dumps(log_data, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "batch_id": batch_id,
        "folder": plan.get("folder"),
        "renamed": len(history_entries),
        "log_file": str(log_file),
    }


def list_rename_logs() -> List[Dict[str, Any]]:
    logs = []
    for p in sorted(RENAME_LOG_DIR.glob("rename_*.json"), reverse=True):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            logs.append({
                "batch_id": data["batch_id"],
                "timestamp": data["timestamp"],
                "folder": data.get("folder", ""),
                "count": data.get("count", 0),
            })
        except Exception:
            continue
    return logs[:30]


def undo_rename_batch(batch_id: str) -> Dict[str, Any]:
    log_file = RENAME_LOG_DIR / f"rename_{batch_id}.json"
    if not log_file.exists():
        raise FileNotFoundError(f"找不到改名紀錄: {batch_id}")

    data = json.loads(log_file.read_text(encoding="utf-8"))
    entries = data.get("entries", [])
    if not entries:
        return {"restored": 0}

    # Two-phase restore
    stage1 = []
    for ent in entries:
        cur_full = GALLERY_ROOT / ent["new_rel_path"]
        if not cur_full.exists():
            continue
        tmp_full = cur_full.parent / f"__undo_{batch_id}_{ent['id']}{cur_full.suffix}"
        os.rename(str(cur_full), str(tmp_full))
        stage1.append((ent, tmp_full))

    conn = get_db()
    db_updates = []
    for ent, tmp_full in stage1:
        old_full = GALLERY_ROOT / ent["old_rel_path"]
        old_full.parent.mkdir(parents=True, exist_ok=True)
        os.rename(str(tmp_full), str(old_full))
        db_updates.append((ent["old_rel_path"], ent["old_filename"], ent["id"]))

    with conn:
        conn.executemany(
            "UPDATE images SET rel_path = ?, filename = ? WHERE id = ?",
            db_updates,
        )
    conn.close()
    log_file.unlink(missing_ok=True)
    return {"restored": len(db_updates), "folder": data.get("folder")}


def get_next_sequence_filename_for_folder(folder_name: str, ext: str, pad_digits: int = 4) -> str:
    """
    當把新圖片「歸檔移入繪師資料夾」時，自動算出該資料夾最後一號 + 1 的檔名：
    例如資料夾內最後一張是 20230515_0336_千種みのり.jpg，
    新移入的圖片就會自動命名為 20230515_0337_千種みのり.jpg（接在最後，絕不打亂順序！）
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT id, rel_path, filename, mtime FROM images WHERE folder = ?",
        (folder_name,),
    ).fetchall()
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

    next_seq = max_seq + 1
    actual_pad = max(pad_digits, len(str(next_seq)))
    return f"{base_date}_{str(next_seq).zfill(actual_pad)}_{artist_label}{ext}"
