# -*- coding: utf-8 -*-
"""
定時自動匯入、清空空子資料夾、AI 自動打標與繪師資料夾歸檔排程引擎 (Scheduler Engine)

功能流程：
1. 支援設定每日指定時間 (HH:MM，可設定一組或多組時間) 自動觸發，或隨時手動點擊「立即執行」。
2. 掃描使用者指定的來源資料夾路徑（含所有深層子資料夾）內的圖片/影片。
3. 依 Windows 自然排序將檔案安全搬移至 D:\\圖檔\\twitter。
4. 搬移完成後，由下而上遞迴刪除來源資料夾內所有已變空的子資料夾（保留最上層來源根資料夾本身）。
5. 對新搬入的圖片自動執行：
   - 資料庫索引與檔名解析
   - Pixiv ID 真實畫師反查
   - (可選) SauceNAO / IQDB 以圖搜圖抓最新 Twitter/Pixiv 畫師
   - 本地 AMD GPU AI (Camie Tagger v2) 視覺打標與畫師辨識
   - 高階語意主題標籤自動聚合
   - 自動建立 D:\\圖檔\\twitter\\<繪師名稱> 資料夾並將圖片移入、按順序標準化編號改名！
"""

import datetime
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from database import (
    DATA_DIR,
    GALLERY_ROOT,
    VALID_EXTENSIONS as ALL_EXTS,
    get_db,
    scan_library,
    ensure_tag,
    refresh_tag_counts,
)
from renamer import win_natural_key
from zh_dict import DANBOORU_ARTIST_TO_FOLDER, resolve_artist_folder_name

SCHEDULER_CONFIG_PATH = DATA_DIR / "scheduler_config.json"


DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "schedule_times": ["03:00"],  # 支援一組或多組 HH:MM (24小時制)
    "source_dirs": [],            # 例如 ["C:\\Users\\Gary\\Downloads\\twitter_dl"]
    "remove_empty_subdirs": True, # 搬完後清空來源路徑下的空子資料夾
    "run_pixiv_lookup": True,     # 自動執行 Pixiv ID 反查
    "run_reverse_search": False,  # 是否對新進圖片先跑 SauceNAO/IQDB 以圖搜圖
    "reverse_search_limit": 30,   # 單次排程以圖搜圖上限張數
    "run_ai_tagger": True,        # 執行本地 GPU AI 自動打標與畫師辨識
    "general_thresh": 0.70,
    "artist_thresh": 0.70,
    "auto_create_artist_folders": True,  # 自動建立繪師資料夾並歸檔
    "min_artist_confidence": 0.70,       # 歸檔至繪師資料夾的最低畫師信心度
    "auto_rename_in_artist_folder": True,# 移入繪師資料夾時自動統一編號改名
    "fallback_date_archive": False,      # 若未認出繪師且為日期檔名，是否歸入 _按日期歸檔
    "last_triggered_date_time": "",      # 避免同一分鐘重複觸發 (YYYY-MM-DD HH:MM)
    "history_logs": [],                  # 最近 30 次排程執行紀錄
}


def load_scheduler_config() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SCHEDULER_CONFIG_PATH.exists():
        save_scheduler_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(SCHEDULER_CONFIG_PATH.read_text(encoding="utf-8"))
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        return merged
    except Exception:
        return dict(DEFAULT_CONFIG)


def save_scheduler_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    merged = load_scheduler_config() if SCHEDULER_CONFIG_PATH.exists() else dict(DEFAULT_CONFIG)
    merged.update(cfg)
    # 限制歷史紀錄最多保留 30 筆
    if "history_logs" in merged and isinstance(merged["history_logs"], list):
        merged["history_logs"] = merged["history_logs"][:30]
    SCHEDULER_CONFIG_PATH.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return merged


def remove_empty_subdirectories(root_dir: Path) -> int:
    """
    由下而上 (bottom-up) 遞迴檢查 root_dir 下的所有子資料夾：
    - 若子資料夾內已無任何有效檔案（若僅剩 Thumbs.db 或 .DS_Store 會先清除），則刪除該空子資料夾。
    - 保留最頂層的 root_dir 本身不刪除，方便使用者下次繼續放新圖片。
    回傳刪除的空子資料夾數量。
    """
    if not root_dir.exists() or not root_dir.is_dir():
        return 0

    removed_dirs = 0
    root_resolved = root_dir.resolve()

    for dirpath, dirnames, filenames in os.walk(str(root_dir), topdown=False):
        curr_path = Path(dirpath).resolve()
        if curr_path == root_resolved:
            continue

        # 刪除 Windows 系統自動產生的快取垃圾檔 (Thumbs.db, desktop.ini, .DS_Store)
        for junk in ("Thumbs.db", "thumbs.db", ".DS_Store"):
            junk_file = curr_path / junk
            if junk_file.exists():
                try:
                    junk_file.unlink()
                except Exception:
                    pass

        try:
            # 若資料夾已經全空，將其刪除
            if not any(curr_path.iterdir()):
                curr_path.rmdir()
                removed_dirs += 1
        except Exception:
            pass

    return removed_dirs


def scan_source_directory_preview(source_dirs: List[str]) -> Dict[str, Any]:
    """
    預覽指定的來源資料夾內目前有多少張圖片/影片與多少個子資料夾。
    """
    details = []
    total_files = 0
    total_subdirs = 0

    for raw_p in source_dirs:
        p_str = raw_p.strip()
        if not p_str:
            continue
        p = Path(p_str)
        if not p.exists() or not p.is_dir():
            details.append({
                "path": p_str,
                "exists": False,
                "file_count": 0,
                "subdir_count": 0,
                "sample_files": [],
            })
            continue

        f_count = 0
        s_count = 0
        samples: List[str] = []
        gallery_resolved = GALLERY_ROOT.resolve()

        for dirpath, dirnames, filenames in os.walk(str(p)):
            curr_p = Path(dirpath).resolve()
            # 防止誤把 D:\圖檔\twitter 本身當成來源資料夾造成無限搬移
            if curr_p == gallery_resolved or gallery_resolved in curr_p.parents:
                continue
            if curr_p != p.resolve():
                s_count += 1
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in ALL_EXTS:
                    f_count += 1
                    if len(samples) < 5:
                        rel = str((curr_p / fn).relative_to(p.resolve())).replace("\\", "/")
                        samples.append(rel)

        total_files += f_count
        total_subdirs += s_count
        details.append({
            "path": p_str,
            "exists": True,
            "file_count": f_count,
            "subdir_count": s_count,
            "sample_files": samples,
        })

    return {
        "total_files": total_files,
        "total_subdirs": total_subdirs,
        "directories": details,
    }


class AutoIngestScheduler:
    def __init__(self):
        self.running_job = False
        self.status_message = "排程器待機中"
        self.current_step = ""
        self.total_target = 100
        self.processed_count = 0
        self._lock = threading.Lock()
        self._daemon_started = False
        self._daemon_thread: Optional[threading.Thread] = None

    def start_daemon(self):
        if self._daemon_started:
            return
        self._daemon_started = True
        self._daemon_thread = threading.Thread(target=self._clock_loop, daemon=True)
        self._daemon_thread.start()

    def _clock_loop(self):
        """背景時鐘執行緒：每 15 秒檢查一次現在時間是否符合使用者設定的 HH:MM"""
        while True:
            try:
                cfg = load_scheduler_config()
                if cfg.get("enabled") and not self.running_job:
                    now = datetime.datetime.now()
                    now_hm = now.strftime("%H:%M")
                    now_ymd_hm = now.strftime("%Y-%m-%d %H:%M")
                    times = [t.strip() for t in cfg.get("schedule_times", []) if t.strip()]
                    if now_hm in times and cfg.get("last_triggered_date_time") != now_ymd_hm:
                        save_scheduler_config({"last_triggered_date_time": now_ymd_hm})
                        self.trigger_pipeline_async(trigger_reason=f"定時排程 ({now_hm})")
            except Exception:
                pass
            time.sleep(15)

    def get_status(self) -> Dict[str, Any]:
        cfg = load_scheduler_config()
        return {
            "running_job": self.running_job,
            "status_message": self.status_message,
            "current_step": "",
            "total_target": self.total_target,
            "processed_count": self.processed_count,
            "config": cfg,
        }

    def trigger_pipeline_async(self, trigger_reason: str = "手動立即執行") -> Dict[str, Any]:
        with self._lock:
            if self.running_job:
                return {"status": "already_running", "message": "排程匯入與分類任務正在執行中"}
            self.running_job = True
            self.total_target = 100
            self.processed_count = 2
            self.status_message = f"正在執行排程匯入與自動歸檔 ({trigger_reason})"
            self.current_step = ""

        t = threading.Thread(target=self._execute_pipeline, args=(trigger_reason,), daemon=True)
        t.start()
        return {"status": "started", "trigger_reason": trigger_reason}

    def _execute_pipeline(self, trigger_reason: str):
        start_ts = time.time()
        started_at_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cfg = load_scheduler_config()

        moved_from_source = 0
        empty_dirs_removed = 0
        new_indexed = 0
        pixiv_found = 0
        reverse_searched = 0
        ai_tagged_count = 0
        organized_to_artist_folders = 0
        new_artist_folders_created = 0
        errors: List[str] = []

        try:
            source_dirs: List[str] = cfg.get("source_dirs", [])
            gallery_resolved = GALLERY_ROOT.resolve()
            moved_dest_filenames: List[str] = []

            # === STEP 1: 從來源資料夾（含子資料夾）依自然排序搬移圖片至 D:\圖檔\twitter ===
            self.processed_count = 5
            self.status_message = f"正在執行排程自動處理 ({trigger_reason})"

            for raw_src in source_dirs:
                src_str = raw_src.strip()
                if not src_str:
                    continue
                src_root = Path(src_str)
                if not src_root.exists() or not src_root.is_dir():
                    errors.append(f"來源路徑不存在: {src_str}")
                    continue

                src_root_resolved = src_root.resolve()
                if src_root_resolved == gallery_resolved or gallery_resolved in src_root_resolved.parents:
                    errors.append(f"略過屬於圖庫內部的路徑: {src_str}")
                    continue

                found_files: List[Tuple[str, Path]] = []
                for dirpath, dirnames, filenames in os.walk(str(src_root)):
                    curr_dir = Path(dirpath)
                    for fn in filenames:
                        ext = os.path.splitext(fn)[1].lower()
                        if ext in ALL_EXTS:
                            full_f = curr_dir / fn
                            rel_str = str(full_f.relative_to(src_root)).replace("\\", "/")
                            found_files.append((rel_str, full_f))

                found_files.sort(key=lambda item: win_natural_key(item[0]))

                for rel_str, src_file in found_files:
                    try:
                        dest_file = GALLERY_ROOT / src_file.name
                        if dest_file.exists():
                            stem = dest_file.stem
                            ext = dest_file.suffix
                            counter = 1
                            while dest_file.exists():
                                dest_file = GALLERY_ROOT / f"{stem}_{counter}{ext}"
                                counter += 1
                        shutil.move(str(src_file), str(dest_file))
                        moved_from_source += 1
                        moved_dest_filenames.append(dest_file.name)
                    except Exception as e:
                        errors.append(f"搬移失敗 ({src_file.name}): {e}")

                # === STEP 2: 搬移完成後清空來源路徑下的所有空子資料夾 ===
                if cfg.get("remove_empty_subdirs", True):
                    empty_dirs_removed += remove_empty_subdirectories(src_root)

            # === STEP 3: 更新圖庫索引 ===
            self.processed_count = 15
            scan_res = scan_library()
            new_indexed = scan_res.get("new_added", 0)

            conn = get_db()
            if moved_dest_filenames:
                placeholders = ",".join(["?"] * len(moved_dest_filenames))
                target_rows = conn.execute(
                    f"SELECT id, rel_path, filename, folder FROM images WHERE folder = '' AND (filename IN ({placeholders}) OR ai_tagged = 0) ORDER BY mtime DESC",
                    moved_dest_filenames,
                ).fetchall()
            else:
                target_rows = conn.execute(
                    "SELECT id, rel_path, filename, folder FROM images WHERE folder = '' AND ai_tagged = 0 ORDER BY mtime DESC LIMIT 300"
                ).fetchall()
            conn.close()

            # === STEP 4: 執行 Pixiv ID 反查 / 以圖搜圖 / 本地 GPU AI 視覺打標 ===
            self.processed_count = 25
            if cfg.get("run_pixiv_lookup", True):
                from ai_tagger import lookup_pixiv_artists_batch
                px_res = lookup_pixiv_artists_batch(limit=250, auto_move_to_artist_folder=False)
                pixiv_found = px_res.get("found_illusts", 0)

            if cfg.get("run_reverse_search", False) and target_rows:
                rev_limit = int(cfg.get("reverse_search_limit", 25))
                from reverse_search import reverse_search_and_tag_image
                rev_targets = target_rows[:rev_limit]
                for idx, r in enumerate(rev_targets, 1):
                    self.processed_count = 25 + int((idx / max(1, len(rev_targets))) * 25)
                    rs = reverse_search_and_tag_image(
                        image_id=r["id"],
                        min_similarity=72.0,
                        fallback_to_ai=False,
                        include_google_lens_url=False,
                    )
                    if rs.get("found"):
                        reverse_searched += 1
                    time.sleep(1.8)

            if cfg.get("run_ai_tagger", True):
                from ai_tagger import tagger_engine, preprocess_image_fast
                conn = get_db()
                if moved_dest_filenames:
                    placeholders = ",".join(["?"] * len(moved_dest_filenames))
                    ai_rows = conn.execute(
                        f"SELECT id, rel_path, filename FROM images WHERE folder = '' AND media_type = 'image' AND filename IN ({placeholders}) AND ai_tagged = 0",
                        moved_dest_filenames,
                    ).fetchall()
                else:
                    ai_rows = conn.execute(
                        "SELECT id, rel_path, filename FROM images WHERE folder = '' AND media_type = 'image' AND ai_tagged = 0 ORDER BY mtime DESC LIMIT 200"
                    ).fetchall()

                if ai_rows:
                    tagger_engine.ensure_model_loaded()
                    tag_cache: Dict[str, int] = {}
                    for r in conn.execute("SELECT id, name FROM tags"):
                        tag_cache[r["name"]] = r["id"]

                    gen_t = max(0.70, float(cfg.get("general_thresh", 0.70)))
                    art_t = max(0.70, float(cfg.get("artist_thresh", 0.70)))

                    db_links = []
                    img_updates = []
                    for idx, r in enumerate(ai_rows, 1):
                        self.processed_count = 50 + int((idx / max(1, len(ai_rows))) * 35)
                        full_p = str(GALLERY_ROOT / r["rel_path"])
                        tensor = preprocess_image_fast(full_p, 512)
                        if tensor is not None:
                            preds, sug_folder = tagger_engine.predict_single(
                                tensor,
                                general_thresh=gen_t,
                                character_thresh=gen_t,
                                copyright_thresh=gen_t,
                                artist_thresh=art_t,
                            )
                            for tname, cat, conf in preds:
                                tid = ensure_tag(conn, tag_cache, tname, cat)
                                if tid > 0:
                                    db_links.append((r["id"], tid, round(conf, 4), "ai"))
                                    if cat == "artist" and tname.lower() in DANBOORU_ARTIST_TO_FOLDER:
                                        fa = DANBOORU_ARTIST_TO_FOLDER[tname.lower()]
                                        tid_fa = ensure_tag(conn, tag_cache, fa, "artist")
                                        if tid_fa > 0:
                                            db_links.append((r["id"], tid_fa, round(conf, 4), "ai"))
                            img_updates.append((sug_folder, r["id"]))
                            ai_tagged_count += 1

                    if img_updates:
                        with conn:
                            conn.executemany(
                                """
                                INSERT INTO image_tags (image_id, tag_id, confidence, source)
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(image_id, tag_id) DO UPDATE SET
                                confidence = MAX(image_tags.confidence, excluded.confidence)
                                """,
                                db_links,
                            )
                            conn.executemany(
                                "UPDATE images SET ai_tagged = 1, suggested_folder = COALESCE(?, suggested_folder) WHERE id = ?",
                                img_updates,
                            )
                            refresh_tag_counts(conn)
                conn.close()

            from semantic_engine import apply_semantic_theme_tags_batch
            apply_semantic_theme_tags_batch()

            # === STEP 5: 自動建立繪師資料夾並將圖片歸檔 ===
            self.processed_count = 90
            if cfg.get("auto_create_artist_folders", True):
                from app import move_images_to_folder, sync_root_suggested_folders
                min_conf = max(0.70, float(cfg.get("min_artist_confidence", 0.70)))
                auto_rn = bool(cfg.get("auto_rename_in_artist_folder", True))
                also_all_root = bool(cfg.get("also_organize_all_root", False))

                sync_root_suggested_folders(min_confidence=min_conf)

                conn = get_db()
                existing_folders_set = {
                    r["folder"]
                    for r in conn.execute("SELECT DISTINCT folder FROM images WHERE folder != ''").fetchall()
                }
                for d in GALLERY_ROOT.iterdir():
                    if d.is_dir() and not d.name.startswith("."):
                        existing_folders_set.add(d.name)
                existing_lower_map = {f.lower(): f for f in existing_folders_set}

                if moved_dest_filenames and not also_all_root:
                    placeholders = ",".join(["?"] * len(moved_dest_filenames))
                    rows_to_move = conn.execute(
                        f"""
                        SELECT id, suggested_folder
                        FROM images
                        WHERE folder = ''
                          AND filename IN ({placeholders})
                          AND suggested_folder IS NOT NULL
                          AND suggested_folder != ''
                        """,
                        moved_dest_filenames,
                    ).fetchall()
                else:
                    rows_to_move = conn.execute(
                        """
                        SELECT i.id, i.suggested_folder
                        FROM images i
                        WHERE i.folder = ''
                          AND i.suggested_folder IS NOT NULL
                          AND i.suggested_folder != ''
                        """
                    ).fetchall()
                conn.close()

                by_artist_folder: Dict[str, List[int]] = {}
                for rm in rows_to_move:
                    raw_f = rm["suggested_folder"].strip()
                    resolved_f = existing_lower_map.get(raw_f.lower(), raw_f)
                    by_artist_folder.setdefault(resolved_f, []).append(rm["id"])

                for folder_name, iids in by_artist_folder.items():
                    is_new = folder_name not in existing_folders_set
                    mres = move_images_to_folder(iids, folder_name, auto_rename=auto_rn)
                    moved_cnt = mres.get("moved", 0)
                    if moved_cnt > 0:
                        organized_to_artist_folders += moved_cnt
                        if is_new:
                            new_artist_folders_created += 1
                            existing_folders_set.add(folder_name)

            elapsed_sec = round(time.time() - start_ts, 2)
            summary_msg = (
                f"✅ 排程執行完成 ({trigger_reason})！搬入 {moved_from_source} 張、清空 {empty_dirs_removed} 個空子資料夾、"
                f"AI 打標 {ai_tagged_count} 張、歸檔至繪師資料夾 {organized_to_artist_folders} 張 (新建 {new_artist_folders_created} 個繪師夾)"
            )
            self.status_message = summary_msg
            self.current_step = "done"

            log_entry = {
                "timestamp": started_at_str,
                "trigger_reason": trigger_reason,
                "elapsed_sec": elapsed_sec,
                "moved_from_source": moved_from_source,
                "empty_dirs_removed": empty_dirs_removed,
                "new_indexed": new_indexed,
                "pixiv_found": pixiv_found,
                "reverse_searched": reverse_searched,
                "ai_tagged_count": ai_tagged_count,
                "organized_to_artist_folders": organized_to_artist_folders,
                "new_artist_folders_created": new_artist_folders_created,
                "errors": errors[:10],
            }
            latest_cfg = load_scheduler_config()
            logs = latest_cfg.get("history_logs", [])
            logs.insert(0, log_entry)
            save_scheduler_config({"history_logs": logs[:30]})

        except Exception as e:
            self.status_message = f"❌ 排程執行發生錯誤: {e}"
            self.current_step = "error"
        finally:
            self.running_job = False


def browse_filesystem_directory(path_str: str = "") -> Dict[str, Any]:
    """
    提供網頁內建「視覺化磁碟與資料夾瀏覽器」所需的目錄清單、磁碟機列表、常用捷徑與麵包屑導覽。
    """
    import string

    # 1. 取得所有可用磁碟機
    drives = []
    for letter in string.ascii_uppercase:
        drive_root = f"{letter}:\\"
        if os.path.exists(drive_root):
            drives.append({
                "name": f"本機磁碟 ({letter}:)",
                "path": drive_root,
            })

    # 2. 常用捷徑資料夾
    user_home = Path.home()
    quick_candidates = [
        ("📥 下載 (Downloads)", user_home / "Downloads"),
        ("🖼️ 下載圖片 (Downloads\\pic)", user_home / "Downloads" / "pic"),
        ("🖥️ 桌面 (Desktop)", user_home / "Desktop"),
        ("🏞️ 圖片 (Pictures)", user_home / "Pictures"),
        ("📂 D:\\ 磁碟根目錄", Path("D:\\")),
        ("📂 D:\\圖檔", Path("D:\\圖檔")),
    ]
    quick_access = []
    seen_quick = set()
    for label, qp in quick_candidates:
        try:
            if qp.exists() and qp.is_dir():
                resolved_str = str(qp.resolve())
                if resolved_str not in seen_quick:
                    seen_quick.add(resolved_str)
                    quick_access.append({"name": label, "path": resolved_str})
        except Exception:
            pass

    target_str = (path_str or "").strip()
    if not target_str:
        # 預設優先打開 Downloads 或第一個存在的常用資料夾
        default_dir = user_home / "Downloads"
        if default_dir.exists():
            target_str = str(default_dir.resolve())
        elif drives:
            target_str = drives[0]["path"]

    target_path = Path(target_str)
    if not target_path.exists() or not target_path.is_dir():
        return {
            "current_path": target_str,
            "exists": False,
            "parent_path": "",
            "breadcrumbs": [],
            "drives": drives,
            "quick_access": quick_access,
            "subdirectories": [],
            "direct_image_count": 0,
        }

    try:
        resolved = target_path.resolve()
    except Exception:
        resolved = target_path

    resolved_str = str(resolved)
    parent_path = str(resolved.parent) if resolved.parent != resolved else ""

    # 建立麵包屑 (Breadcrumbs)
    breadcrumbs = []
    parts = resolved.parts
    acc = ""
    for idx, part in enumerate(parts):
        if idx == 0:
            acc = part if part.endswith("\\") else part + "\\"
            breadcrumbs.append({"name": part.rstrip("\\"), "path": acc})
        else:
            acc = str(Path(acc) / part)
            breadcrumbs.append({"name": part, "path": acc})

    subdirs = []
    direct_image_count = 0
    gallery_resolved = GALLERY_ROOT.resolve()

    try:
        with os.scandir(resolved_str) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.startswith(".") or entry.name in ("$RECYCLE.BIN", "System Volume Information"):
                            continue
                        sub_p = Path(entry.path).resolve()
                        is_gallery_self = (sub_p == gallery_resolved)
                        # 快速統計該子資料夾第一層內的媒體檔數量作為提示
                        media_hint = 0
                        sub_subdir_hint = 0
                        try:
                            with os.scandir(entry.path) as sub_entries:
                                for se in sub_entries:
                                    if se.is_dir(follow_symlinks=False):
                                        if not se.name.startswith("."):
                                            sub_subdir_hint += 1
                                    elif se.is_file(follow_symlinks=False):
                                        if os.path.splitext(se.name)[1].lower() in ALL_EXTS:
                                            media_hint += 1
                        except Exception:
                            pass
                        subdirs.append({
                            "name": entry.name,
                            "path": entry.path,
                            "direct_media_count": media_hint,
                            "subdir_count": sub_subdir_hint,
                            "is_gallery_root": is_gallery_self,
                        })
                    elif entry.is_file(follow_symlinks=False):
                        if os.path.splitext(entry.name)[1].lower() in ALL_EXTS:
                            direct_image_count += 1
                except Exception:
                    continue
    except Exception:
        pass

    subdirs.sort(key=lambda x: win_natural_key(x["name"]))

    return {
        "current_path": resolved_str,
        "exists": True,
        "parent_path": parent_path,
        "breadcrumbs": breadcrumbs,
        "drives": drives,
        "quick_access": quick_access,
        "subdirectories": subdirs,
        "direct_image_count": direct_image_count,
    }


def pick_folder_dialog(initial_dir: str = "") -> str:
    """呼叫 Windows 原生最上層 (TopMost) 資料夾選擇視窗，讓使用者直接點選來源資料夾"""
    import subprocess
    init_escaped = (initial_dir or "").replace("'", "''")
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "[System.Windows.Forms.Application]::EnableVisualStyles(); "
        "$topForm = New-Object System.Windows.Forms.Form; "
        "$topForm.TopMost = $true; "
        "$topForm.StartPosition = 'CenterScreen'; "
        "$topForm.Width = 1; $topForm.Height = 1; "
        "$topForm.ShowInTaskbar = $false; "
        "$topForm.Opacity = 0; "
        "$topForm.Show(); "
        "$topForm.Activate(); "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$f.Description = '請選擇要定時自動搬移圖片的來源資料夾 (支援含子資料夾)'; "
        "$f.ShowNewFolderButton = $true; "
        f"if ('{init_escaped}' -ne '' -and (Test-Path '{init_escaped}')) {{ $f.SelectedPath = '{init_escaped}' }}; "
        "if ($f.ShowDialog($topForm) -eq 'OK') { Write-Output $f.SelectedPath }; "
        "$topForm.Close()"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Sta", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=120,
        )
        return (res.stdout or "").strip()
    except Exception:
        return ""


def sync_windows_task_scheduler(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    同步註冊至 Windows 系統「工作排程器 (Task Scheduler)」，
    確保就算瀏覽器或伺服器沒開，到了設定的幾點幾分也會自動執行搬移、清空空資料夾與 AI 歸檔！
    """
    import subprocess
    import sys

    task_base_name = "TwitterTagGallery_AutoIngest"
    enabled = bool(cfg.get("enabled", False))
    times = [t.strip() for t in cfg.get("schedule_times", []) if t.strip()]

    # 先清理舊的同名排程
    for i in range(1, 6):
        tname = task_base_name if i == 1 else f"{task_base_name}_{i}"
        subprocess.run(
            ["schtasks", "/Delete", "/TN", tname, "/F"],
            capture_output=True,
            text=True,
        )

    if not enabled or not times:
        return {"registered": False, "message": "已停用或移除 Windows 系統定時排程"}

    python_exe = sys.executable
    script_path = str(Path(__file__).resolve())
    tr_cmd = f'"{python_exe}" "{script_path}" --run-once'

    registered_tasks = []
    for idx, hm in enumerate(times[:5], 1):
        tname = task_base_name if idx == 1 else f"{task_base_name}_{idx}"
        res = subprocess.run(
            [
                "schtasks",
                "/Create",
                "/SC",
                "DAILY",
                "/TN",
                tname,
                "/TR",
                tr_cmd,
                "/ST",
                hm,
                "/F",
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            registered_tasks.append(f"{tname} ({hm})")

    return {
        "registered": len(registered_tasks) > 0,
        "tasks": registered_tasks,
    }


scheduler_engine = AutoIngestScheduler()


if __name__ == "__main__":
    import sys
    if "--run-once" in sys.argv:
        print("正在執行定時排程任務 (CLI 模式)...")
        scheduler_engine._execute_pipeline(trigger_reason="Windows 系統排程器自動觸發")
        print(scheduler_engine.status_message)

