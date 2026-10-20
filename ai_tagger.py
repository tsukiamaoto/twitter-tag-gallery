# -*- coding: utf-8 -*-
"""
AI 視覺標籤與畫師辨識引擎 (支援 AMD DirectML GPU 硬體加速)
結合：
1. Camie Tagger v2 (ViT 70,527 標籤模型，內建 7,007 位繪師風格辨識 + 角色 + 作品 + 服裝屬性 + 分級)
2. Pixiv 公開 API 反查引擎 (針對檔名含 Pixiv ID 的圖片直接取得 100% 準確繪師名稱與原作者標籤)
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import requests
from PIL import Image, ImageFile

from database import (
    GALLERY_ROOT,
    MODEL_DIR,
    get_db,
    ensure_tag,
    refresh_tag_counts,
)
from zh_dict import (
    DANBOORU_ARTIST_TO_FOLDER,
    FOLDER_TO_DANBOORU_ARTIST,
    resolve_artist_folder_name,
)

ImageFile.LOAD_TRUNCATED_IMAGES = True

# ImageNet normalization constants for Camie Tagger v2
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32).reshape(3, 1, 1)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32).reshape(3, 1, 1)
PAD_COLOR = (124, 116, 104)


def preprocess_image_fast(image_path: str, image_size: int = 512) -> Optional[np.ndarray]:
    """將圖片預處理為 (1, 3, 512, 512) float32 Tensor，保持長寬比並做 ImageNet 標準化"""
    try:
        with Image.open(image_path) as img:
            if img.mode != "RGB":
                img = img.convert("RGB")
            w, h = img.size
            if w <= 0 or h <= 0:
                return None
            aspect = w / h
            if aspect > 1.0:
                new_w = image_size
                new_h = max(1, int(new_w / aspect))
            else:
                new_h = image_size
                new_w = max(1, int(new_h * aspect))

            img_resized = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
            canvas = Image.new("RGB", (image_size, image_size), PAD_COLOR)
            paste_x = (image_size - new_w) // 2
            paste_y = (image_size - new_h) // 2
            canvas.paste(img_resized, (paste_x, paste_y))

            arr = np.asarray(canvas, dtype=np.float32) / 255.0
            # (H, W, C) -> (C, H, W)
            arr = np.transpose(arr, (2, 0, 1))
            arr = (arr - IMAGENET_MEAN) / IMAGENET_STD
            return np.expand_dims(arr, axis=0)
    except Exception:
        return None


class AITaggerEngine:
    def __init__(self):
        self.session = None
        self.input_name = None
        self.idx_to_tag: Dict[str, str] = {}
        self.tag_to_category: Dict[str, str] = {}
        self.artist_indices: np.ndarray = np.array([], dtype=np.int64)
        self.rating_indices: np.ndarray = np.array([], dtype=np.int64)
        self.active_provider: str = "尚未載入"
        self.model_loaded: bool = False

        # Background worker state
        self.running: bool = False
        self.stop_requested: bool = False
        self.status_message: str = "待機中"
        self.total_target: int = 0
        self.processed_count: int = 0
        self.speed_img_per_sec: float = 0.0
        self.current_file: str = ""
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def ensure_model_loaded(self):
        if self.model_loaded and self.session is not None:
            return

        import onnxruntime as ort
        from huggingface_hub import hf_hub_download

        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        self.status_message = "⏳ 正在準備 AI 視覺模型 (檢查本機模型快取)..."

        meta_path = hf_hub_download(
            repo_id="Camais03/camie-tagger-v2",
            filename="camie-tagger-v2-metadata.json",
            local_dir=str(MODEL_DIR),
        )
        onnx_path = hf_hub_download(
            repo_id="Camais03/camie-tagger-v2",
            filename="camie-tagger-v2.onnx",
            local_dir=str(MODEL_DIR),
        )

        self.status_message = "⏳ 正在載入 70,527 標籤資料庫與啟動 GPU 加速引擎 (DirectML)..."
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)

        tm = meta["dataset_info"]["tag_mapping"]
        self.idx_to_tag = tm["idx_to_tag"]
        self.tag_to_category = tm["tag_to_category"]

        artist_idxs = []
        rating_idxs = []
        for idx_str, tname in self.idx_to_tag.items():
            cat = self.tag_to_category.get(tname, "general")
            if cat == "artist":
                artist_idxs.append(int(idx_str))
            elif cat == "rating":
                rating_idxs.append(int(idx_str))
        self.artist_indices = np.array(artist_idxs, dtype=np.int64)
        self.rating_indices = np.array(rating_idxs, dtype=np.int64)

        avail = ort.get_available_providers()
        providers = []
        if "DmlExecutionProvider" in avail:
            providers.append("DmlExecutionProvider")
        if "CUDAExecutionProvider" in avail:
            providers.append("CUDAExecutionProvider")
        providers.append("CPUExecutionProvider")

        sess_opts = ort.SessionOptions()
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self.session = ort.InferenceSession(onnx_path, sess_options=sess_opts, providers=providers)
        self.active_provider = self.session.get_providers()[0]
        self.input_name = self.session.get_inputs()[0].name
        self.model_loaded = True
        self.status_message = f"🟢 AI 引擎已就緒 ({self.active_provider})"

    def load_model(self) -> Dict[str, Any]:
        """手動將 Camie Tagger v2 模型預載入至 GPU 顯卡記憶體 (VRAM)"""
        self.ensure_model_loaded()
        return {
            "status": "loaded",
            "model_loaded": self.model_loaded,
            "provider": self.active_provider,
            "message": f"已成功將 AI 視覺模型載入顯卡記憶體 ({self.active_provider})",
        }

    def predict_single(
        self,
        tensor: np.ndarray,
        general_thresh: float = 0.38,
        character_thresh: float = 0.45,
        copyright_thresh: float = 0.45,
        artist_thresh: float = 0.65,
    ) -> Tuple[List[Tuple[str, str, float]], Optional[str]]:
        """
        執行單張推論，回傳:
          ( [(tag_name, category, confidence), ...], suggested_folder )
        注意：本機視覺模型對畫師預測採高門檻 (>= 0.65) 以避免低信心度誤猜；
              精準畫師辨識優先交由第二階段「以圖搜圖 (IQDB/Danbooru/SauceNAO/Google) + Gemini Flash 3.8 MCP 校正」。
        """
        if not self.model_loaded or self.session is None:
            self.ensure_model_loaded()

        outputs = self.session.run(None, {self.input_name: tensor})
        logits = outputs[1][0] if len(outputs) >= 2 else outputs[0][0]
        probs = 1.0 / (1.0 + np.exp(-np.clip(logits, -30.0, 30.0)))

        results: List[Tuple[str, str, float]] = []
        suggested_folder: Optional[str] = None

        # 1. Pick top Rating
        if len(self.rating_indices) > 0:
            r_probs = probs[self.rating_indices]
            best_r_idx = self.rating_indices[int(np.argmax(r_probs))]
            r_tag = self.idx_to_tag.get(str(best_r_idx))
            if r_tag:
                results.append((r_tag, "rating", float(probs[best_r_idx])))

        # 2. Pick ONLY top-1 Artist prediction if confidence >= artist_thresh (floor 0.65)
        if len(self.artist_indices) > 0:
            a_probs = probs[self.artist_indices]
            best_pos = int(np.argmax(a_probs))
            prob_val = float(a_probs[best_pos])
            if prob_val >= max(0.65, artist_thresh):
                real_idx = self.artist_indices[best_pos]
                a_tag = self.idx_to_tag.get(str(real_idx))
                if a_tag:
                    results.append((a_tag, "artist", prob_val))
                    if prob_val >= 0.72:
                        if a_tag.lower() in DANBOORU_ARTIST_TO_FOLDER:
                            suggested_folder = DANBOORU_ARTIST_TO_FOLDER[a_tag.lower()]
                        else:
                            suggested_folder = resolve_artist_folder_name(a_tag)

        # 3. Pick Character, Copyright, General, Meta
        min_t = min(general_thresh, character_thresh, copyright_thresh)
        candidate_indices = np.where(probs >= min_t)[0]

        gen_list = []
        for idx in candidate_indices:
            idx_str = str(int(idx))
            tname = self.idx_to_tag.get(idx_str)
            if not tname:
                continue
            cat = self.tag_to_category.get(tname, "general")
            p = float(probs[idx])
            if cat in ("artist", "rating", "year"):
                continue
            elif cat == "character" and p >= character_thresh:
                results.append((tname, "character", p))
            elif cat == "copyright" and p >= copyright_thresh:
                results.append((tname, "copyright", p))
            elif cat in ("general", "meta") and p >= general_thresh:
                gen_list.append((tname, "general", p))

        # Keep top 35 general tags sorted by confidence
        gen_list.sort(key=lambda x: x[2], reverse=True)
        results.extend(gen_list[:35])

        return results, suggested_folder

    def unload_model(self, delete_disk_files: bool = False, stop_running_task: bool = True) -> Dict[str, Any]:
        """
        卸載 Camie Tagger v2 模型，釋放 GPU 顯卡記憶體 (VRAM) 與系統 RAM；
        若 delete_disk_files=True，同時移除硬碟上的模型檔案 (~750MB)。
        """
        if stop_running_task and self.running:
            self.stop_requested = True
            for _ in range(30):
                if not self.running:
                    break
                time.sleep(0.1)

        with self._lock:
            was_loaded = self.model_loaded
            self.session = None
            self.input_name = None
            self.idx_to_tag = {}
            self.tag_to_category = {}
            self.artist_indices = np.array([], dtype=np.int64)
            self.rating_indices = np.array([], dtype=np.int64)
            self.model_loaded = False
            self.active_provider = "已卸載 (已釋放 VRAM)"
            if stop_running_task:
                self.status_message = "AI 模型已卸載，顯卡與系統記憶體已釋放"

        try:
            from decensor_engine import decensor_engine
            decensor_engine.unload_models()
        except Exception:
            pass

        import gc
        gc.collect()

        deleted_files = []
        freed_bytes = 0
        if delete_disk_files and MODEL_DIR.exists():
            for fn in ("camie-tagger-v2.onnx", "camie-tagger-v2-metadata.json", "decensor_mosaic.onnx", "decensor_bar.onnx"):
                fp = MODEL_DIR / fn
                if fp.exists():
                    try:
                        sz = fp.stat().st_size
                        fp.unlink()
                        deleted_files.append(fn)
                        freed_bytes += sz
                    except Exception:
                        pass

        return {
            "status": "unloaded",
            "was_loaded": was_loaded,
            "model_loaded": False,
            "deleted_disk_files": deleted_files,
            "freed_disk_mb": round(freed_bytes / (1024 * 1024), 1),
            "message": "已成功卸載 AI 模型（含視覺打標與去馬賽克模型）並釋放 GPU 顯卡記憶體 (VRAM) 與系統記憶體"
            + (f"，同時刪除硬碟模型檔 ({round(freed_bytes / (1024 * 1024), 1)} MB)" if deleted_files else ""),
        }

    def start_batch_tagging(
        self,
        scope: str = "root_untagged",
        limit: int = 0,
        general_thresh: float = 0.38,
        artist_thresh: float = 0.65,
        auto_move_to_artist_folder: bool = False,
        auto_unload: bool = True,
        run_local_ai: bool = True,
        run_reverse_search_calibrate: bool = True,
        reverse_search_limit: int = 30,
        min_similarity: float = 72.0,
        saucenao_api_key: Optional[str] = None,
        run_semantic_themes: bool = True,
        run_decensor: bool = False,
        decensor_save_mode: str = "new_copy",
    ) -> Dict[str, Any]:
        with self._lock:
            if self.running:
                return {"status": "already_running", "message": "智慧標籤任務已在執行中"}
            self.running = True
            self.stop_requested = False
            self.processed_count = 0
            est_ai = int(limit) if (run_local_ai and limit and int(limit) > 0) else (200 if run_local_ai else 0)
            est_rev = int(reverse_search_limit) if (run_reverse_search_calibrate and reverse_search_limit) else 0
            self.total_target = max(1, est_ai + est_rev + (1 if run_semantic_themes else 0))
            self.speed_img_per_sec = 0.0
            self.current_file = ""
            self.status_message = (
                "⏳ 正在啟動智慧標籤並載入 AI 視覺模型至 GPU 顯卡記憶體..."
                if run_local_ai and not self.model_loaded
                else "⏳ 正在準備智慧標籤待處理圖片清單..."
            )

        self._thread = threading.Thread(
            target=self._batch_worker,
            args=(
                scope,
                limit,
                general_thresh,
                artist_thresh,
                auto_move_to_artist_folder,
                auto_unload,
                run_local_ai,
                run_reverse_search_calibrate,
                reverse_search_limit,
                min_similarity,
                saucenao_api_key,
                run_semantic_themes,
                run_decensor,
                decensor_save_mode,
            ),
            daemon=True,
        )
        self._thread.start()
        return {
            "status": "started",
            "scope": scope,
            "run_local_ai": run_local_ai,
            "run_reverse_search_calibrate": run_reverse_search_calibrate,
            "run_semantic_themes": run_semantic_themes,
            "run_decensor": run_decensor,
            "decensor_save_mode": decensor_save_mode,
            "auto_move_to_artist_folder": auto_move_to_artist_folder,
            "auto_unload": auto_unload,
        }

    def stop_batch_tagging(self) -> Dict[str, Any]:
        self.stop_requested = True
        self.status_message = "正在安全停止並儲存目前進度..."
        return {"status": "stopping"}

    def get_status(self) -> Dict[str, Any]:
        onnx_file = MODEL_DIR / "camie-tagger-v2.onnx"
        disk_cached = onnx_file.exists()
        disk_size_mb = round(onnx_file.stat().st_size / (1024 * 1024), 1) if disk_cached else 0.0
        dec_loaded = False
        try:
            from decensor_engine import decensor_engine
            dec_loaded = decensor_engine.model_loaded
        except Exception:
            pass
        return {
            "running": self.running,
            "model_loaded": self.model_loaded or dec_loaded,
            "disk_cached": disk_cached,
            "disk_size_mb": disk_size_mb,
            "provider": self.active_provider if self.model_loaded else ("DmlExecutionProvider (去碼模型)" if dec_loaded else self.active_provider),
            "status_message": self.status_message,
            "total_target": self.total_target,
            "processed_count": self.processed_count,
            "speed_img_per_sec": round(self.speed_img_per_sec, 2),
            "current_file": self.current_file,
        }

    def _batch_worker(
        self,
        scope: str,
        limit: int,
        general_thresh: float,
        artist_thresh: float,
        auto_move_to_artist_folder: bool = False,
        auto_unload: bool = True,
        run_local_ai: bool = True,
        run_reverse_search_calibrate: bool = True,
        reverse_search_limit: int = 30,
        min_similarity: float = 72.0,
        saucenao_api_key: Optional[str] = None,
        run_semantic_themes: bool = True,
        run_decensor: bool = False,
        decensor_save_mode: str = "new_copy",
    ):
        try:
            self.current_file = ""
            ai_rows = []
            rev_rows = []

            # 若勾選第一階段本機 AI 打標，第一時間先載入模型至 GPU，確保使用者立即看到模型載入就緒！
            if run_local_ai and not self.stop_requested:
                self.ensure_model_loaded()

            conn = get_db()
            # 1. 準備階段一 (本機 GPU AI 視覺打標) 目標清單
            if run_local_ai and not self.stop_requested:
                limit_sql = f" LIMIT {int(limit)}" if (limit and int(limit) > 0) else ""
                if scope == "root_untagged":
                    # 優先抓取根目錄 ai_tagged = 0 或尚未有畫師標籤的圖片
                    sql = f"""
                        SELECT i.id, i.rel_path, i.filename, i.folder
                        FROM images i
                        WHERE i.media_type IN ('image', 'gif')
                          AND i.folder = ''
                          AND (
                              i.ai_tagged = 0
                              OR NOT EXISTS (
                                  SELECT 1 FROM image_tags it
                                  JOIN tags t ON it.tag_id = t.id
                                  WHERE it.image_id = i.id AND t.category = 'artist'
                              )
                          )
                        ORDER BY i.ai_tagged ASC, i.mtime DESC
                        {limit_sql}
                    """
                    ai_rows = conn.execute(sql).fetchall()
                    if not ai_rows:
                        ai_rows = conn.execute(
                            f"SELECT id, rel_path, filename, folder FROM images WHERE media_type IN ('image', 'gif') AND folder = '' ORDER BY mtime DESC{limit_sql}"
                        ).fetchall()
                elif scope == "all_untagged":
                    sql = f"""
                        SELECT i.id, i.rel_path, i.filename, i.folder
                        FROM images i
                        WHERE i.media_type IN ('image', 'gif')
                          AND (
                              i.ai_tagged = 0
                              OR NOT EXISTS (
                                  SELECT 1 FROM image_tags it
                                  WHERE it.image_id = i.id AND it.source = 'ai'
                              )
                          )
                        ORDER BY i.ai_tagged ASC, i.mtime DESC
                        {limit_sql}
                    """
                    ai_rows = conn.execute(sql).fetchall()
                elif scope == "root_all":
                    ai_rows = conn.execute(
                        f"SELECT id, rel_path, filename, folder FROM images WHERE media_type IN ('image', 'gif') AND folder = '' ORDER BY mtime DESC{limit_sql}"
                    ).fetchall()
                elif scope == "ai_low_conf":
                    ai_rows = conn.execute(
                        f"SELECT id, rel_path, filename, folder FROM images WHERE media_type IN ('image', 'gif') AND folder = '' AND ai_tagged = 0 ORDER BY mtime DESC{limit_sql}"
                    ).fetchall()

            # 2. 準備階段二 (以圖搜圖 & Gemini 畫師校正) 目標清單
            if run_reverse_search_calibrate and not self.stop_requested:
                rev_limit_val = int(reverse_search_limit) if reverse_search_limit and int(reverse_search_limit) > 0 else 30
                if scope == "ai_low_conf":
                    rev_sql = """
                        SELECT DISTINCT i.id, i.rel_path, i.filename, i.folder
                        FROM images i
                        JOIN image_tags it ON i.id = it.image_id
                        JOIN tags t ON it.tag_id = t.id
                        WHERE i.media_type IN ('image', 'gif')
                          AND t.category = 'artist' AND it.source = 'ai'
                          AND i.id NOT IN (
                              SELECT it2.image_id FROM image_tags it2
                              JOIN tags t2 ON it2.tag_id = t2.id
                              WHERE t2.category = 'artist' AND it2.source IN ('pixiv', 'saucenao', 'iqdb', 'danbooru', 'twitter', 'gemini_mcp', 'folder', 'manual')
                          )
                        ORDER BY i.mtime DESC
                        LIMIT ?
                    """
                elif scope == "all_untagged":
                    rev_sql = """
                        SELECT i.id, i.rel_path, i.filename, i.folder
                        FROM images i
                        WHERE i.media_type IN ('image', 'gif')
                          AND i.id NOT IN (
                              SELECT it.image_id FROM image_tags it
                              JOIN tags t ON it.tag_id = t.id
                              WHERE t.category = 'artist' AND it.source IN ('pixiv', 'saucenao', 'iqdb', 'danbooru', 'twitter', 'gemini_mcp', 'folder', 'manual')
                          )
                        ORDER BY i.mtime DESC
                        LIMIT ?
                    """
                else:
                    rev_sql = """
                        SELECT i.id, i.rel_path, i.filename, i.folder
                        FROM images i
                        WHERE i.media_type IN ('image', 'gif') AND i.folder = ''
                          AND i.id NOT IN (
                              SELECT it.image_id FROM image_tags it
                              JOIN tags t ON it.tag_id = t.id
                              WHERE t.category = 'artist' AND it.source IN ('pixiv', 'saucenao', 'iqdb', 'danbooru', 'twitter', 'gemini_mcp', 'folder', 'manual')
                          )
                        ORDER BY
                          CASE WHEN i.pixiv_id IS NULL THEN 0 ELSE 1 END,
                          i.mtime DESC
                        LIMIT ?
                    """
                rev_rows = conn.execute(rev_sql, (rev_limit_val,)).fetchall()
            conn.close()

            total_steps = len(ai_rows) + len(rev_rows) + (1 if run_semantic_themes else 0)
            self.total_target = max(1, total_steps)
            self.processed_count = 0

            if len(ai_rows) == 0 and len(rev_rows) == 0 and not run_semantic_themes:
                self.status_message = "沒有符合條件的待分析或待校正圖片"
                self.running = False
                return

            t_start = time.time()
            ai_done_count = 0
            rev_found_count = 0
            rev_purged_count = 0
            moved_total = 0
            pending_moves: Dict[str, List[int]] = {}

            # =========================================================================
            # 階段 ①：本機 GPU AI 視覺打標 (Camie Tagger v2)
            # =========================================================================
            if run_local_ai and len(ai_rows) > 0 and not self.stop_requested:
                self.ensure_model_loaded()
                self.status_message = (
                    f"[1/3 本機 AI 視覺打標] 模型已載入 ({self.active_provider})，開始分析 {len(ai_rows):,} 張圖片..."
                )

                conn = get_db()
                tag_cache: Dict[str, int] = {}
                for r in conn.execute("SELECT id, name FROM tags"):
                    tag_cache[r["name"]] = r["id"]

                batch_db_links = []
                batch_img_updates = []

                def load_item(row_item):
                    full_p = str(GALLERY_ROOT / row_item["rel_path"])
                    tensor = preprocess_image_fast(full_p, 512)
                    return row_item, tensor

                eff_gen_t = max(0.25, float(general_thresh))
                eff_art_t = max(0.65, float(artist_thresh))

                with ThreadPoolExecutor(max_workers=4) as pool:
                    chunk_size = 24
                    for c_start in range(0, len(ai_rows), chunk_size):
                        if self.stop_requested:
                            break
                        chunk = ai_rows[c_start : c_start + chunk_size]
                        for row_item, tensor in pool.map(load_item, chunk):
                            if self.stop_requested:
                                break

                            img_id = row_item["id"]
                            self.current_file = row_item["filename"]
                            if tensor is not None:
                                preds, sug_folder = self.predict_single(
                                    tensor,
                                    general_thresh=eff_gen_t,
                                    character_thresh=max(0.40, eff_gen_t),
                                    copyright_thresh=max(0.40, eff_gen_t),
                                    artist_thresh=eff_art_t,
                                )
                                for tname, cat, conf in preds:
                                    tid = ensure_tag(conn, tag_cache, tname, cat)
                                    if tid > 0:
                                        batch_db_links.append((img_id, tid, round(conf, 4), "ai"))
                                        if cat == "artist" and tname.lower() in DANBOORU_ARTIST_TO_FOLDER:
                                            folder_artist = DANBOORU_ARTIST_TO_FOLDER[tname.lower()]
                                            tid_fa = ensure_tag(conn, tag_cache, folder_artist, "artist")
                                            if tid_fa > 0:
                                                batch_db_links.append((img_id, tid_fa, round(conf, 4), "ai"))

                                batch_img_updates.append((sug_folder, img_id))
                                if auto_move_to_artist_folder and sug_folder and row_item["folder"] == "":
                                    pending_moves.setdefault(sug_folder, []).append(img_id)
                            else:
                                batch_img_updates.append((None, img_id))

                            ai_done_count += 1
                            self.processed_count += 1
                            elapsed = max(0.001, time.time() - t_start)
                            self.speed_img_per_sec = self.processed_count / elapsed
                            self.status_message = (
                                f"[1/3 本機 AI 視覺打標] 整體進度：{self.processed_count:,} / {self.total_target:,} "
                                f"（已完成 AI 打標 {ai_done_count:,} / {len(ai_rows):,} 張，速度 {self.speed_img_per_sec:.1f} 張/秒）"
                            )

                            if len(batch_img_updates) >= 24:
                                with conn:
                                    conn.executemany(
                                        """
                                        INSERT INTO image_tags (image_id, tag_id, confidence, source)
                                        VALUES (?, ?, ?, ?)
                                        ON CONFLICT(image_id, tag_id) DO UPDATE SET
                                        confidence = MAX(image_tags.confidence, excluded.confidence)
                                        """,
                                        batch_db_links,
                                    )
                                    conn.executemany(
                                        "UPDATE images SET ai_tagged = 1, suggested_folder = COALESCE(?, suggested_folder) WHERE id = ?",
                                        batch_img_updates,
                                    )
                                batch_db_links.clear()
                                batch_img_updates.clear()

                with conn:
                    if batch_img_updates:
                        conn.executemany(
                            """
                            INSERT INTO image_tags (image_id, tag_id, confidence, source)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(image_id, tag_id) DO UPDATE SET
                            confidence = MAX(image_tags.confidence, excluded.confidence)
                            """,
                            batch_db_links,
                        )
                        conn.executemany(
                            "UPDATE images SET ai_tagged = 1, suggested_folder = COALESCE(?, suggested_folder) WHERE id = ?",
                            batch_img_updates,
                        )
                    refresh_tag_counts(conn)
                conn.close()

            # 階段 ① 結束後，若開啟 auto_unload 立即先釋放 GPU VRAM，讓後續以圖搜圖完全不佔顯卡記憶體！
            if auto_unload and self.model_loaded and not self.stop_requested:
                self.unload_model(delete_disk_files=False, stop_running_task=False)

            # =========================================================================
            # 階段 ②：深度以圖搜圖 & Gemini 畫師與標籤校正 (Danbooru / IQDB / SauceNAO)
            # =========================================================================
            if run_reverse_search_calibrate and len(rev_rows) > 0 and not self.stop_requested:
                from reverse_search import reverse_search_and_tag_image
                saucenao_rate_limited = False

                for idx, r_item in enumerate(rev_rows):
                    if self.stop_requested:
                        break

                    img_id = r_item["id"]
                    self.current_file = r_item["filename"]
                    self.status_message = (
                        f"[2/3 以圖搜圖 & 畫師校正] 整體進度：{self.processed_count:,} / {self.total_target:,} "
                        f"— 正在搜圖比對 ({idx + 1}/{len(rev_rows)}): {r_item['filename']}"
                    )

                    use_sn = "" if saucenao_rate_limited else (saucenao_api_key or "")
                    res = reverse_search_and_tag_image(
                        image_id=img_id,
                        saucenao_api_key=use_sn if not saucenao_rate_limited else None,
                        min_similarity=min_similarity,
                        fallback_to_ai=False,
                        include_google_lens_url=False,
                    )
                    if res.get("rate_limited"):
                        saucenao_rate_limited = True

                    if res.get("found"):
                        rev_found_count += 1
                        rev_purged_count += res.get("purged_wrong_ai_tags", 0)
                        sug_f = res.get("suggested_folder")
                        if auto_move_to_artist_folder and sug_f and r_item["folder"] == "":
                            pending_moves.setdefault(sug_f, []).append(img_id)

                    self.processed_count += 1
                    elapsed = max(0.001, time.time() - t_start)
                    self.speed_img_per_sec = self.processed_count / elapsed
                    self.status_message = (
                        f"[2/3 以圖搜圖 & 畫師校正] 整體進度：{self.processed_count:,} / {self.total_target:,} "
                        f"（搜圖認證 {rev_found_count} 位畫師 / 校正清除 {rev_purged_count} 個誤猜標籤）"
                    )

                    if idx < len(rev_rows) - 1 and not self.stop_requested:
                        time.sleep(0.35)

            # =========================================================================
            # 階段 ③：語意相關性主題聚合 & 自動建立繪師資料夾歸檔
            # =========================================================================
            if run_semantic_themes and not self.stop_requested:
                self.status_message = (
                    f"[3/3 語意主題聚合與歸檔] 整體進度：{self.processed_count:,} / {self.total_target:,} — 正在聚合相關性主題標籤..."
                )
                try:
                    from semantic_engine import apply_semantic_theme_tags_batch
                    apply_semantic_theme_tags_batch()
                except Exception:
                    pass
                self.processed_count = self.total_target

            decensored_count = 0
            if run_decensor and not self.stop_requested:
                try:
                    from decensor_engine import decensor_engine
                    conn = get_db()
                    cand_ids = [r["id"] for r in ai_rows] if ai_rows else [r["id"] for r in rev_rows]
                    if cand_ids:
                        placeholders = ",".join("?" for _ in cand_ids)
                        censor_rows = conn.execute(
                            f"""
                            SELECT DISTINCT i.id, i.filename
                            FROM images i
                            JOIN image_tags it ON i.id = it.image_id
                            JOIN tags t ON it.tag_id = t.id
                            WHERE i.id IN ({placeholders})
                              AND t.name IN ('mosaic_censoring', 'bar_censor', 'censored')
                            LIMIT 30
                            """,
                            cand_ids,
                        ).fetchall()
                    else:
                        censor_rows = conn.execute(
                            """
                            SELECT DISTINCT i.id, i.filename
                            FROM images i
                            JOIN image_tags it ON i.id = it.image_id
                            JOIN tags t ON it.tag_id = t.id
                            WHERE t.name IN ('mosaic_censoring', 'bar_censor')
                              AND i.id NOT IN (
                                  SELECT it2.image_id FROM image_tags it2
                                  JOIN tags t2 ON it2.tag_id = t2.id
                                  WHERE t2.name = 'AI去馬賽克'
                              )
                            ORDER BY i.mtime DESC
                            LIMIT 20
                            """
                        ).fetchall()
                    conn.close()

                    for d_idx, c_row in enumerate(censor_rows):
                        if self.stop_requested:
                            break
                        self.current_file = c_row["filename"]
                        self.status_message = (
                            f"[✨ AI 去馬賽克] 正在處理 ({d_idx + 1}/{len(censor_rows)}): {c_row['filename']}"
                        )
                        d_res = decensor_engine.apply_and_save_decensor(
                            image_id=c_row["id"],
                            save_mode=decensor_save_mode or "new_copy",
                            mode="auto",
                        )
                        if d_res.get("success"):
                            decensored_count += 1
                except Exception:
                    pass

            if auto_move_to_artist_folder and pending_moves and not self.stop_requested:
                from app import move_images_to_folder
                for target_f, iids in pending_moves.items():
                    unique_iids = list(dict.fromkeys(iids))
                    m_res = move_images_to_folder(unique_iids, target_f, auto_rename=True)
                    moved_total += m_res.get("moved", 0)

            if self.stop_requested:
                self.status_message = (
                    f"已暫停（完成 AI 打標 {ai_done_count} 張、以圖搜圖認證 {rev_found_count} 張、歸檔 {moved_total} 張）"
                )
            else:
                dec_txt = f" ➔ AI 去馬賽克 {decensored_count} 張" if run_decensor else ""
                self.status_message = (
                    f"✅ 智慧標籤處理完成！AI 打標 {ai_done_count} 張 ➔ "
                    f"搜圖認證 {rev_found_count} 位畫師 (校正 {rev_purged_count} 標籤){dec_txt} ➔ "
                    f"歸入繪師資料夾 {moved_total} 張"
                    + ("（已自動卸載模型釋放 VRAM）" if auto_unload else "")
                )
        except Exception as e:
            self.status_message = f"❌ 發生錯誤: {str(e)}"
        finally:
            self.running = False
            if auto_unload:
                final_msg = self.status_message
                self.unload_model(delete_disk_files=False, stop_running_task=False)
                self.status_message = final_msg


def lookup_pixiv_artists_batch(limit: int = 100, auto_move_to_artist_folder: bool = False) -> Dict[str, Any]:
    """
    針對檔名含有 Pixiv ID 的圖片，透過 Pixiv 公開 Ajax 介面反查真實繪師名稱與原作者標籤，
    並同步填入 suggested_folder（或直接建立繪師資料夾歸檔）。
    """
    conn = get_db()
    rows = conn.execute(
        """
        SELECT id, pixiv_id, filename, folder
        FROM images
        WHERE pixiv_id IS NOT NULL
          AND id NOT IN (SELECT image_id FROM image_tags WHERE source = 'pixiv')
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    if not rows:
        conn.close()
        return {"checked": 0, "found": 0, "message": "所有含 Pixiv ID 的圖片皆已查詢過"}

    tag_cache: Dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM tags"):
        tag_cache[r["name"]] = r["id"]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.pixiv.net/",
    }

    # Group by pixiv_id so multi-page works (_p0, _p1) only request once
    pid_to_img_ids: Dict[str, List[int]] = {}
    img_id_to_folder: Dict[int, str] = {}
    for r in rows:
        pid_to_img_ids.setdefault(r["pixiv_id"], []).append(r["id"])
        img_id_to_folder[r["id"]] = r["folder"]

    found_works = 0
    links = []
    sug_updates = []
    pending_moves: Dict[str, List[int]] = {}

    for pid, img_ids in pid_to_img_ids.items():
        try:
            resp = requests.get(f"https://www.pixiv.net/ajax/illust/{pid}", headers=headers, timeout=6)
            if resp.status_code == 200:
                body = resp.json().get("body", {})
                if body:
                    user_name = body.get("userName")
                    if user_name:
                        found_works += 1
                        tid_artist = ensure_tag(conn, tag_cache, user_name, "artist")
                        sug_folder = resolve_artist_folder_name(user_name)
                        for iid in img_ids:
                            links.append((iid, tid_artist, 1.0, "pixiv"))
                            if sug_folder:
                                sug_updates.append((sug_folder, iid))
                                if auto_move_to_artist_folder and img_id_to_folder.get(iid, "") == "":
                                    pending_moves.setdefault(sug_folder, []).append(iid)

                    tags_obj = body.get("tags", {}).get("tags", [])
                    for t_item in tags_obj:
                        t_str = t_item.get("tag")
                        if t_str and t_str not in ("R-18", "オリジナル"):
                            tid_t = ensure_tag(conn, tag_cache, t_str, "general")
                            for iid in img_ids:
                                links.append((iid, tid_t, 1.0, "pixiv"))
            time.sleep(0.25)
        except Exception:
            continue

    with conn:
        if links:
            conn.executemany(
                "INSERT OR IGNORE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, ?, ?)",
                links,
            )
        if sug_updates:
            conn.executemany(
                "UPDATE images SET suggested_folder = COALESCE(?, suggested_folder) WHERE id = ?",
                sug_updates,
            )
        refresh_tag_counts(conn)
    conn.close()

    moved_total = 0
    if auto_move_to_artist_folder and pending_moves:
        from app import move_images_to_folder
        for target_f, iids in pending_moves.items():
            m_res = move_images_to_folder(iids, target_f, auto_rename=True)
            moved_total += m_res.get("moved", 0)

    return {
        "checked_illusts": len(pid_to_img_ids),
        "found_illusts": found_works,
        "images_updated": len(rows),
        "moved_to_artist_folders": moved_total,
    }


tagger_engine = AITaggerEngine()

