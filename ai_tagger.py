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
from model_control import operation as model_operation, configure_directml, snapshot as model_snapshot
from processing_state import record as record_processing, AI_MODEL_VERSION
from background_work import update_job
import requests
from PIL import Image, ImageFile
from background_work import (
    BackgroundBusy, acquire_job, exclusive_job, start_job_thread,
    background_checkpoint, media_io,
)

from database import (
    GALLERY_ROOT,
    MODEL_DIR,
    get_db,
    ensure_tag,
    refresh_tag_counts,
    run_db_write_with_retry,
    log_operation,
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
    with media_io(background=True):
        return _preprocess_image(image_path, image_size)


def _preprocess_image(image_path: str, image_size: int = 512) -> Optional[np.ndarray]:
    """將圖片或影片影格預處理為 (1, 3, 512, 512) float32 Tensor，保持長寬比並做 ImageNet 標準化"""
    try:
        if str(image_path).lower().endswith(".mp4"):
            from thumbnail_cache import _extract_video_thumbnail
            img_ctx = _extract_video_thumbnail(Path(image_path), max_size=max(512, image_size))
            if img_ctx is None:
                return None
        else:
            img_ctx = Image.open(image_path)
        with img_ctx as img:
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

    @model_operation("loading")
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
        sess_opts.intra_op_num_threads = 2
        sess_opts.inter_op_num_threads = 1
        sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        configure_directml(sess_opts,ort,providers)

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

    @model_operation("inference")
    def predict_single(
        self,
        tensor: np.ndarray,
        general_thresh: float = 0.38,
        character_thresh: float = 0.45,
        copyright_thresh: float = 0.85,
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

        # Pick only core identities; general/rating predictions are not persisted.
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
        min_t = min(character_thresh, copyright_thresh)
        candidate_indices = np.where(probs >= min_t)[0]

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

        if stop_running_task and self.running:
            return {"status":"busy","message":"正在停止推論；請待任務結束後再卸載","model_loaded":self.model_loaded}
        return self._unload_model_now(delete_disk_files,stop_running_task)

    @model_operation("unloading")
    def _unload_model_now(self,delete_disk_files=False,stop_running_task=True):
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
            for fn in ("camie-tagger-v2.onnx", "camie-tagger-v2-metadata.json"):
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
        run_semantic_themes: bool = False,
        run_decensor: bool = False,
        decensor_save_mode: str = "new_copy",
    ) -> Dict[str, Any]:
        with self._lock:
            if self.running:
                return {"status": "already_running", "message": "智慧標籤任務已在執行中"}
            try:
                lease = acquire_job("AI 批次打標")
            except BackgroundBusy as exc:
                return exc.response()
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
            kwargs={"_job_lease": lease},
            daemon=True,
        )
        lease.publish(force=True,recipe={"kind":"ai","options":{"scope":scope,"limit":limit,"general_thresh":general_thresh,"artist_thresh":artist_thresh,
                      "auto_move_to_artist_folder":auto_move_to_artist_folder,"auto_unload":auto_unload,"run_local_ai":run_local_ai,
                      "run_reverse_search_calibrate":run_reverse_search_calibrate,"reverse_search_limit":reverse_search_limit,"min_similarity":min_similarity,
                      "run_semantic_themes":run_semantic_themes,"run_decensor":run_decensor,"decensor_save_mode":decensor_save_mode}})
        try:
            start_job_thread(self._thread, lease)
        except BaseException:
            self.running = False
            raise
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
            "lifecycle": model_snapshot(),
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

    @exclusive_job("AI 批次打標")
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
        run_semantic_themes: bool = False,
        run_decensor: bool = False,
        decensor_save_mode: str = "new_copy",
    ):
        ai_failures_count=0
        try:
            self.current_file = ""
            ai_rows = []
            rev_rows = []
            low_conf_rev_candidates: Dict[int, Dict[str, Any]] = {}

            # 若勾選第一階段本機 AI 打標，第一時間先載入模型至 GPU，確保使用者立即看到模型載入就緒！
            if run_local_ai and not self.stop_requested:
                self.ensure_model_loaded()

            conn = get_db()
            # 查詢目前已具備可靠畫師標籤的圖片集合（避免本機 AI 誤覆蓋或重複排入慢速搜圖）
            verified_artist_img_ids = set(
                r[0]
                for r in conn.execute(
                    """
                    SELECT DISTINCT it.image_id
                    FROM image_tags it
                    JOIN tags t ON it.tag_id = t.id
                    WHERE t.category = 'artist'
                      AND it.source IN ('pixiv', 'saucenao', 'iqdb', 'danbooru', 'twitter', 'gemini_mcp', 'folder', 'manual', 'filename')
                    """
                ).fetchall()
            )

            # 1. 準備階段一 (本機 GPU AI 視覺打標) 目標清單
            # 分開判定「缺視覺內容標籤 (general/character/copyright)」與「缺畫師標籤 (artist)」
            if run_local_ai and not self.stop_requested:
                limit_sql = f" LIMIT {int(limit)}" if (limit and int(limit) > 0) else ""
                if scope == "root_untagged":
                    sql = f"""
                        SELECT i.id, i.rel_path, i.filename, i.folder
                        FROM images i
                        WHERE i.missing_since IS NULL AND NOT EXISTS (SELECT 1 FROM image_processing p WHERE p.image_id=i.id AND p.stage='ai' AND p.status='failed' AND p.retry_at>strftime('%s','now')) AND i.media_type IN ('image', 'gif', 'video')
                          AND i.folder = ''
                          AND (
                              i.ai_tagged = 0
                              OR NOT EXISTS (
                                  SELECT 1 FROM image_tags it
                                  JOIN tags t ON it.tag_id = t.id
                                  WHERE it.image_id = i.id AND t.category IN ('general', 'character', 'copyright')
                              )
                              OR NOT EXISTS (
                                  SELECT 1 FROM image_tags it
                                  JOIN tags t ON it.tag_id = t.id
                                  WHERE it.image_id = i.id AND t.category = 'artist'
                              )
                          )
                        ORDER BY i.ai_tagged ASC, i.mtime DESC
                        {limit_sql}
                    """
                    ai_rows = [dict(r) for r in conn.execute(sql).fetchall()]
                    if not ai_rows:
                        ai_rows = [
                            dict(r)
                            for r in conn.execute(
                                f"SELECT id, rel_path, filename, folder FROM images WHERE missing_since IS NULL AND NOT EXISTS (SELECT 1 FROM image_processing p WHERE p.image_id=images.id AND p.stage='ai' AND p.status='failed' AND p.retry_at>strftime('%s','now')) AND media_type IN ('image', 'gif', 'video') AND folder = '' ORDER BY mtime DESC{limit_sql}"
                            ).fetchall()
                        ]
                elif scope == "all_untagged":
                    sql = f"""
                        SELECT i.id, i.rel_path, i.filename, i.folder
                        FROM images i
                        WHERE i.missing_since IS NULL AND NOT EXISTS (SELECT 1 FROM image_processing p WHERE p.image_id=i.id AND p.stage='ai' AND p.status='failed' AND p.retry_at>strftime('%s','now')) AND i.media_type IN ('image', 'gif', 'video')
                          AND (
                              i.ai_tagged = 0
                              OR NOT EXISTS (
                                  SELECT 1 FROM image_tags it
                                  JOIN tags t ON it.tag_id = t.id
                                  WHERE it.image_id = i.id AND t.category IN ('general', 'character', 'copyright')
                              )
                              OR (
                                  i.folder = ''
                                  AND NOT EXISTS (
                                      SELECT 1 FROM image_tags it
                                      JOIN tags t ON it.tag_id = t.id
                                      WHERE it.image_id = i.id AND t.category = 'artist'
                                  )
                              )
                          )
                        ORDER BY i.ai_tagged ASC, i.mtime DESC
                        {limit_sql}
                    """
                    ai_rows = [dict(r) for r in conn.execute(sql).fetchall()]
                elif scope == "root_all":
                    ai_rows = [
                        dict(r)
                        for r in conn.execute(
                            f"SELECT id, rel_path, filename, folder FROM images WHERE missing_since IS NULL AND NOT EXISTS (SELECT 1 FROM image_processing p WHERE p.image_id=images.id AND p.stage='ai' AND p.status='failed' AND p.retry_at>strftime('%s','now')) AND media_type IN ('image', 'gif', 'video') AND folder = '' ORDER BY mtime DESC{limit_sql}"
                        ).fetchall()
                    ]
                elif scope == "ai_low_conf":
                    ai_rows = [
                        dict(r)
                        for r in conn.execute(
                            f"""
                            SELECT i.id, i.rel_path, i.filename, i.folder
                            FROM images i
                            WHERE i.missing_since IS NULL AND NOT EXISTS (SELECT 1 FROM image_processing p WHERE p.image_id=i.id AND p.stage='ai' AND p.status='failed' AND p.retry_at>strftime('%s','now')) AND i.media_type IN ('image', 'gif', 'video')
                              AND (
                                  i.ai_tagged = 0
                                  OR NOT EXISTS (
                                      SELECT 1 FROM image_tags it
                                      JOIN tags t ON it.tag_id = t.id
                                      WHERE it.image_id = i.id AND t.category IN ('general', 'character', 'copyright')
                                  )
                              )
                            ORDER BY i.ai_tagged ASC, i.mtime DESC
                            {limit_sql}
                            """
                        ).fetchall()
                    ]

            if scope in ("root_untagged","all_untagged") and ai_rows:
                pending = {row[0] for row in conn.execute("SELECT id FROM images WHERE ai_tagged=0 AND missing_since IS NULL")}
                ai_rows = [row for row in ai_rows if row["id"] in pending]

            # 2. 準備階段二 (以圖搜圖 & Gemini 畫師校正) 基礎候選清單
            if run_reverse_search_calibrate and not self.stop_requested:
                rev_limit_val = int(reverse_search_limit) if reverse_search_limit and int(reverse_search_limit) > 0 else 30
                if scope == "ai_low_conf":
                    rev_sql = """
                        SELECT DISTINCT i.id, i.rel_path, i.filename, i.folder
                        FROM images i
                        LEFT JOIN image_tags it ON i.id = it.image_id
                        LEFT JOIN tags t ON it.tag_id = t.id AND t.category = 'artist'
                        WHERE i.missing_since IS NULL AND NOT EXISTS (SELECT 1 FROM image_processing p WHERE p.image_id=i.id AND p.stage='ai' AND p.status='failed' AND p.retry_at>strftime('%s','now')) AND i.media_type IN ('image', 'gif', 'video')
                          AND i.folder = ''
                          AND i.id NOT IN (
                              SELECT it2.image_id FROM image_tags it2
                              JOIN tags t2 ON it2.tag_id = t2.id
                              WHERE t2.category = 'artist' AND it2.source IN ('pixiv', 'saucenao', 'iqdb', 'danbooru', 'twitter', 'gemini_mcp', 'folder', 'manual')
                          )
                          AND (it.confidence IS NULL OR it.confidence < 0.70 OR it.source = 'ai')
                        ORDER BY i.mtime DESC
                        LIMIT ?
                    """
                elif scope == "all_untagged":
                    rev_sql = """
                        SELECT i.id, i.rel_path, i.filename, i.folder
                        FROM images i
                        WHERE i.missing_since IS NULL AND NOT EXISTS (SELECT 1 FROM image_processing p WHERE p.image_id=i.id AND p.stage='ai' AND p.status='failed' AND p.retry_at>strftime('%s','now')) AND i.media_type IN ('image', 'gif', 'video')
                          AND i.folder = ''
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
                else:
                    rev_sql = """
                        SELECT i.id, i.rel_path, i.filename, i.folder
                        FROM images i
                        WHERE i.missing_since IS NULL AND NOT EXISTS (SELECT 1 FROM image_processing p WHERE p.image_id=i.id AND p.stage='ai' AND p.status='failed' AND p.retry_at>strftime('%s','now')) AND i.media_type IN ('image', 'gif', 'video') AND i.folder = ''
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
                rev_rows = [dict(r) for r in conn.execute(rev_sql, (rev_limit_val,)).fetchall()]
            conn.close()

            total_steps = len(ai_rows) + len(rev_rows) + (1 if run_semantic_themes else 0) + (1 if run_decensor else 0)
            self.total_target = max(1, total_steps)
            self.processed_count = 0

            if len(ai_rows) == 0 and len(rev_rows) == 0 and not run_semantic_themes and not run_decensor:
                self.status_message = "沒有符合條件的待分析或待校正圖片"
                self.running = False
                return

            t_start = time.time()
            ai_done_count = 0
            danbooru_md5_found = 0
            rev_found_count = 0
            rev_purged_count = 0
            moved_total = 0
            identity_pending_count = 0
            pending_moves: Dict[str, List[int]] = {}

            # =========================================================================
            # 階段 ①：三段式智慧分流 — 第 1 段：本機 GPU AI 視覺打標 (Camie Tagger v2)
            # 修正重點：
            # 1) CPU 預處理與 GPU 推論期間不開啟資料庫連線、不執行 ensure_tag 隱式交易
            # 2) 每 96 張開啟一次毫秒級短交易寫入，徹底消除 database is locked
            # 3) 已在繪師子資料夾或已有可靠畫師的圖片，只補齊內容標籤，不覆蓋畫師，也不排入慢速搜圖
            # 4) 僅將「無可靠畫師 或 AI 畫師/角色最高信心度 < 0.70」的根目錄圖片排入後續網路搜圖
            # =========================================================================
            if run_local_ai and len(ai_rows) > 0 and not self.stop_requested:
                self.ensure_model_loaded()
                self.status_message = (
                    f"[1/3 本機 AI 視覺打標] 模型已載入 ({self.active_provider})，開始分析 {len(ai_rows):,} 張圖片..."
                )

                # 記憶體暫存區：(img_id, tname, cat, conf) 與 (sug_folder, img_id)
                pending_tag_entries: List[Tuple[int, str, str, float]] = []
                batch_img_updates: List[Tuple[Optional[str], int]] = []
                failed_ai_ids = []

                def flush_ai_batch():
                    if not batch_img_updates and not pending_tag_entries and not failed_ai_ids:
                        return
                    entries_snapshot = list(pending_tag_entries)
                    updates_snapshot = list(batch_img_updates)
                    failures_snapshot = list(failed_ai_ids)
                    pending_tag_entries.clear()
                    batch_img_updates.clear()
                    failed_ai_ids.clear()

                    db_conn = get_db()
                    try:
                        tag_cache: Dict[str, int] = {
                            r["name"]: r["id"]
                            for r in db_conn.execute("SELECT id, name FROM tags").fetchall()
                        }

                        def _do_flush(c):
                            for _,iid in updates_snapshot:
                                c.execute("DELETE FROM image_tags WHERE image_id=? AND source='ai'",(iid,))
                            db_links = []
                            for iid, tname, cat, conf in entries_snapshot:
                                tid = ensure_tag(c, tag_cache, tname, cat, source="ai")
                                if tid > 0:
                                    db_links.append((iid, tid, round(conf, 4), "ai"))
                            if db_links:
                                c.executemany(
                                    """
                                    INSERT INTO image_tags (image_id, tag_id, confidence, source)
                                    VALUES (?, ?, ?, ?)
                                    ON CONFLICT(image_id, tag_id) DO UPDATE SET
                                    confidence = MAX(image_tags.confidence, excluded.confidence)
                                    """,
                                    db_links,
                                )
                            if updates_snapshot:
                                c.executemany(
                                    "UPDATE images SET ai_tagged = 1, suggested_folder = COALESCE(?, suggested_folder) WHERE id = ?",
                                    updates_snapshot,
                                )
                            for _,iid in updates_snapshot:
                                record_processing(c,iid,"ai","succeeded",model_version=AI_MODEL_VERSION)
                            for iid in failures_snapshot:
                                record_processing(c,iid,"ai","failed",error="圖片解碼失敗",retry_at=time.time()+300,model_version=AI_MODEL_VERSION)
                            refresh_tag_counts(c)

                        run_db_write_with_retry(db_conn, _do_flush)
                    finally:
                        db_conn.close()

                def load_item(row_item):
                    full_p = str(GALLERY_ROOT / row_item["rel_path"])
                    tensor = preprocess_image_fast(full_p, 512)
                    return row_item, tensor

                eff_gen_t = max(0.25, float(general_thresh))
                eff_art_t = max(0.65, float(artist_thresh))

                with ThreadPoolExecutor(max_workers=2) as pool:
                    chunk_size = 8
                    for c_start in range(0, len(ai_rows), chunk_size):
                        if self.stop_requested:
                            break
                        chunk = ai_rows[c_start : c_start + chunk_size]
                        for row_item, tensor in pool.map(load_item, chunk):
                            background_checkpoint()
                            if self.stop_requested:
                                break

                            img_id = row_item["id"]
                            in_artist_subfolder = bool(row_item["folder"] and row_item["folder"].strip())
                            has_verified_artist = in_artist_subfolder or (img_id in verified_artist_img_ids)
                            self.current_file = row_item["filename"]

                            if tensor is not None:
                                preds, sug_folder = self.predict_single(
                                    tensor,
                                    general_thresh=eff_gen_t,
                                    character_thresh=max(0.40, eff_gen_t),
                                    copyright_thresh=max(0.85, eff_gen_t),
                                    artist_thresh=eff_art_t,
                                )
                                best_artist_conf = 0.0
                                best_char_conf = 0.0
                                for tname, cat, conf in preds:
                                    if cat == "artist":
                                        best_artist_conf = max(best_artist_conf, conf)
                                        # 若圖片已在繪師資料夾或已有可靠畫師，不讓 AI 猜測的畫師覆蓋或混入
                                        if has_verified_artist:
                                            continue
                                    elif cat == "character":
                                        best_char_conf = max(best_char_conf, conf)

                                    pending_tag_entries.append((img_id, tname, cat, conf))
                                    if cat == "artist" and not has_verified_artist and tname.lower() in DANBOORU_ARTIST_TO_FOLDER:
                                        folder_artist = DANBOORU_ARTIST_TO_FOLDER[tname.lower()]
                                        pending_tag_entries.append((img_id, folder_artist, "artist", conf))

                                # 若已有繪師資料夾，確保其資料夾名稱也作為可靠的 folder 來源畫師標籤
                                effective_sug_folder = None if has_verified_artist else sug_folder
                                batch_img_updates.append((effective_sug_folder, img_id))

                                if auto_move_to_artist_folder and effective_sug_folder and not in_artist_subfolder:
                                    pending_moves.setdefault(effective_sug_folder, []).append(img_id)

                                # 第 2 & 3 段智慧分流判定：
                                # 只有「尚未歸檔且無可靠畫師」或「在根目錄且 AI 畫師/角色信心度 < 0.70」才需要進入網路搜圖
                                if not has_verified_artist and not in_artist_subfolder:
                                    if best_artist_conf < 0.70 or best_char_conf < 0.70:
                                        low_conf_rev_candidates[img_id] = row_item
                            else:
                                failed_ai_ids.append(img_id)
                                ai_failures_count+=1

                            ai_done_count += 1
                            self.processed_count += 1
                            update_job(stage="本機 AI 分析",processed_count=self.processed_count,total_target=self.total_target,current_file=self.current_file,checkpoint={"last_image_id":img_id})
                            elapsed = max(0.001, time.time() - t_start)
                            self.speed_img_per_sec = ai_done_count / elapsed
                            self.status_message = (
                                f"[1/3 本機 AI 視覺打標] 整體進度：{self.processed_count:,} / {self.total_target:,} "
                                f"（已完成 AI 打標 {ai_done_count:,} / {len(ai_rows):,} 張，速度 {self.speed_img_per_sec:.1f} 張/秒）"
                            )

                            if len(batch_img_updates)+len(failed_ai_ids) >= 96:
                                flush_ai_batch()

                flush_ai_batch()
                db_conn = get_db()
                try:
                    run_db_write_with_retry(db_conn, lambda c: refresh_tag_counts(c))
                finally:
                    db_conn.close()

            # 階段 ① 結束後，若開啟 auto_unload 立即先釋放 GPU VRAM，讓後續以圖搜圖完全不佔顯卡記憶體！
            if auto_unload and self.model_loaded and not self.stop_requested:
                self.unload_model(delete_disk_files=False, stop_running_task=False)

            if run_local_ai and ai_rows and not self.stop_requested:
                from artist_identity import resolve_image_folders
                self.status_message = "正在查證畫師跨平台身分（Danbooru；未確認者交由 MCP 補查）..."
                update_job(stage=self.status_message)
                identity_report = resolve_image_folders([r["id"] for r in ai_rows])
                identity_pending_count = len(identity_report["pending_image_ids"])

            # =========================================================================
            # 階段 ②：三段式智慧分流 — 第 2 & 3 段：
            # 先執行 Danbooru 批次 MD5 高速精準秒查 (75 hashes/req)，
            # 再針對仍無可靠畫師或 AI 信心度 < 0.70 的圖片執行深度以圖搜圖 (IQDB / SauceNAO)
            # =========================================================================
            if run_reverse_search_calibrate and not self.stop_requested:
                from reverse_search import (
                    reverse_search_and_tag_image,
                    search_danbooru_by_md5_batch,
                    apply_danbooru_posts_batch_to_db,
                )

                # 合併「原本選入的 rev_rows」與「階段 ① 判定信心度 < 0.70 且在根目錄的候選圖片」
                rev_limit_val = int(reverse_search_limit) if reverse_search_limit and int(reverse_search_limit) > 0 else 30
                merged_rev_map: Dict[int, Dict[str, Any]] = {}
                for r_item in rev_rows:
                    if not (r_item.get("folder") or "").strip():
                        merged_rev_map[r_item["id"]] = dict(r_item)
                for iid, r_item in low_conf_rev_candidates.items():
                    if len(merged_rev_map) >= max(rev_limit_val, len(rev_rows)):
                        break
                    merged_rev_map.setdefault(iid, dict(r_item))

                effective_rev_rows = list(merged_rev_map.values())[: max(rev_limit_val, len(rev_rows))]
                self.total_target = max(
                    1,
                    len(ai_rows) + len(effective_rev_rows) + (1 if run_semantic_themes else 0),
                )

                if effective_rev_rows:
                    # 2A. 先用 Danbooru Batch MD5 一次批次秒查所有候選圖 (75 張僅需 1 次 API 請求！)
                    self.status_message = (
                        f"[2/3 批次 MD5 雜湊秒查] 正在對 {len(effective_rev_rows)} 張待查圖片進行 Danbooru 批次 MD5 秒查..."
                    )
                    id_to_full_path = {
                        r["id"]: str(GALLERY_ROOT / r["rel_path"]) for r in effective_rev_rows
                    }
                    md5_matched_posts = search_danbooru_by_md5_batch(id_to_full_path, chunk_size=75)
                    if md5_matched_posts:
                        batch_res = apply_danbooru_posts_batch_to_db(
                            md5_matched_posts,
                            source_label="danbooru",
                            purge_ai_artist=True,
                        )
                        danbooru_md5_found = batch_res.get("updated", 0)
                        rev_found_count += danbooru_md5_found
                        rev_purged_count += batch_res.get("purged_ai_tags", 0)
                        if auto_move_to_artist_folder:
                            for sug_f, iids in batch_res.get("suggested_moves", {}).items():
                                pending_moves.setdefault(sug_f, []).extend(iids)

                    # 扣除已被 MD5 命中的圖片，僅對剩餘圖片進行慢速視覺搜圖 (IQDB / SauceNAO)
                    remaining_rev_rows = []
                    for r_item in effective_rev_rows:
                        if r_item["id"] in md5_matched_posts:
                            self.processed_count += 1
                        else:
                            remaining_rev_rows.append(r_item)

                    saucenao_rate_limited = False
                    for idx, r_item in enumerate(remaining_rev_rows):
                        if self.stop_requested:
                            break

                        img_id = r_item["id"]
                        self.current_file = r_item["filename"]
                        self.status_message = (
                            f"[2/3 以圖搜圖 & 畫師校正] 整體進度：{self.processed_count:,} / {self.total_target:,} "
                            f"— 正在搜圖比對 ({idx + 1}/{len(remaining_rev_rows)}): {r_item['filename']}"
                        )

                        use_sn = "" if saucenao_rate_limited else (saucenao_api_key or "")
                        res = reverse_search_and_tag_image(
                            image_id=img_id,
                            saucenao_api_key=use_sn if not saucenao_rate_limited else None,
                            min_similarity=min_similarity,
                            fallback_to_ai=False,
                            include_google_lens_url=False,
                            run_refresh_counts=False,
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
                            f"（MD5 秒查 {danbooru_md5_found} 張 + 搜圖認證 {rev_found_count - danbooru_md5_found} 位畫師 / 校正清除 {rev_purged_count} 個誤猜標籤）"
                        )

                        if idx < len(remaining_rev_rows) - 1 and not self.stop_requested:
                            time.sleep(0.25)

                    db_conn = get_db()
                    try:
                        run_db_write_with_retry(db_conn, lambda c: refresh_tag_counts(c))
                    finally:
                        db_conn.close()

            # =========================================================================
            # 階段 ③：語意相關性主題聚合 & 自動建立繪師資料夾歸檔
            # =========================================================================
            # Generated theme tags are disabled; continue directly to file actions.
            decensored_count = 0
            if run_decensor and not self.stop_requested:
                try:
                    from decensor_engine import decensor_engine
                    dec_limit = int(limit) if (limit and int(limit) > 0) else 100
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
                            LIMIT ?
                            """,
                            (*cand_ids, dec_limit),
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
                            LIMIT ?
                            """,
                            (dec_limit,),
                        ).fetchall()
                    conn.close()

                    if censor_rows and not ai_rows and not rev_rows:
                        self.total_target = len(censor_rows)
                        self.processed_count = 0

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
                            refresh_counts=False,
                        )
                        if d_res.get("success"):
                            decensored_count += 1
                        if not ai_rows and not rev_rows:
                            self.processed_count = d_idx + 1

                    if decensored_count > 0:
                        db_conn = get_db()
                        try:
                            run_db_write_with_retry(db_conn, lambda c: refresh_tag_counts(c))
                        finally:
                            db_conn.close()
                except Exception:
                    pass

            if auto_move_to_artist_folder and pending_moves and not self.stop_requested:
                from gallery_service import move_images_to_artist_folder as move_images_to_folder
                for target_f, iids in pending_moves.items():
                    unique_iids = list(dict.fromkeys(iids))
                    m_res = move_images_to_folder(unique_iids, target_f, auto_rename=True, refresh_counts=False)
                    moved_total += m_res.get("moved", 0)
                db_conn = get_db()
                try:
                    run_db_write_with_retry(db_conn, lambda c: refresh_tag_counts(c))
                finally:
                    db_conn.close()

            total_elapsed = round(time.time() - t_start, 2)
            if self.stop_requested:
                self.status_message = (
                    f"已暫停（完成 AI 打標 {ai_done_count} 張、搜圖/MD5 認證 {rev_found_count} 張、歸檔 {moved_total} 張）"
                )
            else:
                dec_txt = f" ➔ AI 去馬賽克 {decensored_count} 張" if run_decensor else ""
                self.status_message = (
                    f"✅ 智慧標籤處理完成！AI 打標 {ai_done_count} 張 ➔ "
                    f"搜圖/MD5 認證 {rev_found_count} 位畫師 (校正 {rev_purged_count} 標籤){dec_txt} ➔ "
                    f"歸入繪師資料夾 {moved_total} 張"
                    + ("（已自動卸載模型釋放 VRAM）" if auto_unload else "")
                )
            if ai_failures_count:
                self.status_message+=f"；{ai_failures_count} 張圖片解碼失敗，保留待重試"
                update_job(status="failed",last_error=f"{ai_failures_count} 張圖片解碼失敗",stage="部分完成")
            if identity_pending_count:
                self.status_message += f"；{identity_pending_count} 張畫師身分待確認（設定 → 畫師身分）"
            log_operation(
                category="ai",
                action="智慧標籤批次處理 (已暫停)" if self.stop_requested else "智慧標籤三段式批次處理",
                summary=self.status_message,
                status="warning" if self.stop_requested or ai_failures_count else "success",
                affected_count=ai_done_count + rev_found_count + moved_total,
                duration_sec=total_elapsed,
                details={
                    "scope": scope,
                    "ai_tagged": ai_done_count,
                    "danbooru_md5_found": danbooru_md5_found,
                    "reverse_search_found": rev_found_count,
                    "purged_wrong_ai_tags": rev_purged_count,
                    "decensored_count": decensored_count if run_decensor else 0,
                    "moved_to_artist_folders": moved_total,
                    "pending_artist_identity": identity_pending_count,
                    "speed_img_per_sec": round(self.speed_img_per_sec, 2),
                    "provider": self.active_provider,
                },
            )
        except Exception as e:
            self.status_message = f"❌ 發生錯誤: {str(e)}"
            update_job(status="failed",last_error=str(e))
            log_operation(
                category="ai",
                action="智慧標籤批次處理",
                summary=self.status_message,
                status="error",
                details={"scope": scope, "error": str(e)},
            )
        finally:
            self.running = False
            if "flush_ai_batch" in locals():
                flush_ai_batch()
            if self.stop_requested:
                update_job(status="cancelled",stage="已停止")
            if auto_unload:
                final_msg = self.status_message
                self.unload_model(delete_disk_files=False, stop_running_task=False)
                self.status_message = final_msg


@exclusive_job("Pixiv 批次反查")
def lookup_pixiv_artists_batch(
    limit: int = 200,
    auto_move_to_artist_folder: bool = False,
    enable_google_web_fallback: bool = True,
) -> Dict[str, Any]:
    """
    針對檔名含有 Pixiv ID 的圖片進行三層高速反查與救援：
    1. 批次透過 Danbooru `pixiv:<id1>,<id2>,...` 介面一次秒查 40 個 Pixiv ID（即使 Pixiv 原作已 404 刪除也能救回約 85% 真實畫師與完整標籤！）
    2. 對剩餘未命中的 Pixiv ID 透過平行執行緒呼叫 Pixiv 公開 Ajax 介面反查（網路 I/O 期間完全不持有資料庫鎖）
    3. 若 Pixiv 已 404 刪除且 Danbooru 未收錄，自動啟動 Google Lens 搜圖 + Yande.re / Google 網頁線索救援與視覺搜圖
    4. 所有查驗完畢的圖片（含確認 404 絕版作品）皆會寫入 `source IN ('pixiv', 'pixiv_checked')` 標記，徹底解除 LIMIT 佇列死鎖！
    """
    from reverse_search import (
        search_danbooru_by_pixiv_ids_batch,
        search_danbooru_by_md5_batch,
        apply_danbooru_posts_batch_to_db,
        search_google_and_web_by_clues,
        reverse_search_and_tag_image,
        save_persistent_artist_cache,
    )

    t_pixiv_start = time.time()
    conn = get_db()
    rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, pixiv_id, rel_path, filename, folder
            FROM images
            WHERE pixiv_id IS NOT NULL
              AND missing_since IS NULL
              AND id NOT IN (
                  SELECT image_id FROM image_tags WHERE source = 'pixiv'
              )
              AND NOT EXISTS (SELECT 1 FROM image_processing p WHERE p.image_id=images.id AND p.stage='pixiv'
                  AND (p.status='succeeded' OR p.retry_at > strftime('%s','now')))
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    ]
    conn.close()

    if not rows:
        return {"checked": 0, "found": 0, "message": "所有含 Pixiv ID 的圖片皆已查詢過"}

    # 依 pixiv_id 分組，讓多頁作品 (_p0, _p1) 只查詢一次網路
    pid_to_img_ids: Dict[str, List[int]] = {}
    pid_to_sample_row: Dict[str, Dict[str, Any]] = {}
    img_id_to_folder: Dict[int, str] = {}
    id_to_full_path: Dict[int, str] = {}

    for r in rows:
        pid = str(r["pixiv_id"])
        iid = r["id"]
        pid_to_img_ids.setdefault(pid, []).append(iid)
        pid_to_sample_row.setdefault(pid, r)
        img_id_to_folder[iid] = r["folder"]
        id_to_full_path[iid] = str(GALLERY_ROOT / r["rel_path"])

    all_pids = list(pid_to_img_ids.keys())
    found_works = 0
    rescued_by_danbooru = 0
    rescued_by_pixiv_api = 0
    rescued_by_google_web = 0
    pending_moves: Dict[str, List[int]] = {}
    resolved_pids = set()

    # =========================================================================
    # 第 1 層：Danbooru 批次 `pixiv:<id>` + `md5` 高速反查 (不鎖 DB，一次查 40 個 ID)
    # =========================================================================
    danbooru_pid_map = search_danbooru_by_pixiv_ids_batch(all_pids, chunk_size=40)
    matched_img_to_post: Dict[int, Dict[str, Any]] = {}
    for pid, post in danbooru_pid_map.items():
        resolved_pids.add(pid)
        found_works += 1
        rescued_by_danbooru += 1
        for iid in pid_to_img_ids.get(pid, []):
            matched_img_to_post[iid] = post

    # 針對還沒被 pixiv_id 命中的圖片，順便跑一次批次 MD5 雜湊秒查
    unmatched_id_to_path = {
        iid: path
        for pid, iids in pid_to_img_ids.items()
        if pid not in resolved_pids
        for iid in iids
        if ( path := id_to_full_path.get(iid) )
    }
    if unmatched_id_to_path:
        md5_posts = search_danbooru_by_md5_batch(unmatched_id_to_path, chunk_size=75)
        for iid, post in md5_posts.items():
            matched_img_to_post[iid] = post
        for pid, iids in pid_to_img_ids.items():
            if pid not in resolved_pids and any(iid in md5_posts for iid in iids):
                resolved_pids.add(pid)
                found_works += 1
                rescued_by_danbooru += 1

    if matched_img_to_post:
        batch_res = apply_danbooru_posts_batch_to_db(
            matched_img_to_post,
            source_label="pixiv",
            purge_ai_artist=True,
        )
        if auto_move_to_artist_folder:
            for sug_f, iids in batch_res.get("suggested_moves", {}).items():
                for iid in iids:
                    if img_id_to_folder.get(iid, "") == "":
                        pending_moves.setdefault(sug_f, []).append(iid)

    # =========================================================================
    # 第 2 層：對剩餘未命中的 Pixiv ID 執行平行 Pixiv Ajax 查詢 (8 執行緒，完全不開 DB)
    # =========================================================================
    remaining_pids = [pid for pid in all_pids if pid not in resolved_pids]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.pixiv.net/",
    }

    pixiv_api_results: Dict[str, Dict[str, Any]] = {}
    not_found_404_pids: List[str] = []
    pixiv_results = {}

    def fetch_single_pixiv(pid: str) -> Tuple[str, int, Dict[str, Any]]:
        try:
            resp = requests.get(f"https://www.pixiv.net/ajax/illust/{pid}", headers=headers, timeout=6)
            if resp.status_code == 200:
                body = resp.json().get("body", {}) or {}
                return pid, 200, body
            return pid, resp.status_code, {}
        except Exception:
            return pid, 0, {}

    if remaining_pids:
        with ThreadPoolExecutor(max_workers=2) as pool:
            for pid, status_code, body in pool.map(fetch_single_pixiv, remaining_pids):
                pixiv_results[pid] = status_code
                if status_code == 200 and body and body.get("userName"):
                    pixiv_api_results[pid] = body
                    resolved_pids.add(pid)
                    found_works += 1
                    rescued_by_pixiv_api += 1
                elif status_code in (404, 410):
                    not_found_404_pids.append(pid)
                else:
                    not_found_404_pids.append(pid)

    # =========================================================================
    # 第 3 層：針對 Pixiv 404 已刪除作品，啟動平行 Yande.re / Google 網頁線索與視覺搜圖救援
    # =========================================================================
    google_web_rescued: Dict[str, Dict[str, Any]] = {}
    if enable_google_web_fallback and not_found_404_pids:
        def _rescue_404_pid(pid: str) -> Tuple[str, Dict[str, Any]]:
            sample_row = pid_to_sample_row.get(pid)
            if not sample_row:
                return pid, {}
            try:
                clue_res = search_google_and_web_by_clues(
                    pixiv_id=pid,
                    filename=sample_row["filename"],
                    thumb_bytes=None,
                )
                return pid, clue_res
            except Exception:
                return pid, {}

        with ThreadPoolExecutor(max_workers=2) as pool:
            for pid, clue_res in pool.map(_rescue_404_pid, not_found_404_pids):
                if clue_res.get("found") and (clue_res.get("artists") or clue_res.get("tags")):
                    google_web_rescued[pid] = clue_res
                    resolved_pids.add(pid)
                    found_works += 1
                    rescued_by_google_web += 1

        # 對仍未找到的少數 404 作品 (最多抽查 5 張) 執行 IQDB / SauceNAO 視覺搜圖
        still_unfound_pids = [p for p in not_found_404_pids if p not in resolved_pids][:5]
        for pid in still_unfound_pids:
            sample_row = pid_to_sample_row.get(pid)
            if not sample_row:
                continue
            rev_res = reverse_search_and_tag_image(
                image_id=sample_row["id"],
                min_similarity=70.0,
                fallback_to_ai=False,
                include_google_lens_url=False,
                run_refresh_counts=False,
            )
            if rev_res.get("found"):
                resolved_pids.add(pid)
                found_works += 1
                rescued_by_google_web += 1
                sug_f = rev_res.get("suggested_folder")
                if auto_move_to_artist_folder and sug_f:
                    for iid in pid_to_img_ids.get(pid, []):
                        if img_id_to_folder.get(iid, "") == "":
                            pending_moves.setdefault(sug_f, []).append(iid)

    # =========================================================================
    # 第 4 層：一次性開啟短交易寫入所有結果，並為所有查驗過的圖片加上 pixiv_checked 標記
    # =========================================================================
    from artist_identity import resolve as resolve_identity
    pixiv_identity_folders = {}
    for pid, body in pixiv_api_results.items():
        user_name, user_id = body.get("userName"), body.get("userId")
        if user_name:
            background_checkpoint()
            identity = resolve_identity(user_name, [f"https://www.pixiv.net/users/{user_id}"] if user_id else [])
            pixiv_identity_folders[pid] = identity.get("folder_name")
    db_conn = get_db()
    try:
        tag_cache: Dict[str, int] = {
            r["name"]: r["id"]
            for r in db_conn.execute("SELECT id, name FROM tags").fetchall()
        }

        initial_moves={name:list(ids) for name,ids in pending_moves.items()}
        def _write_pixiv_batch(c):
            pending_moves.clear()  # Callback may be retried after a rollback.
            pending_moves.update({name:list(ids) for name,ids in initial_moves.items()})
            links = []
            sug_updates = []
            # 確保系統有處理完成標記用的虛擬標籤，防止 404 刪除作品永遠卡死在佇列頂端
            checked_tid = ensure_tag(c, tag_cache, "Pixiv已查驗", "source")

            # 寫入 Pixiv Ajax 查獲的結果
            for pid, body in pixiv_api_results.items():
                img_ids = pid_to_img_ids.get(pid, [])
                user_name = body.get("userName")
                if user_name:
                    tid_artist = ensure_tag(c, tag_cache, user_name, "artist", source="pixiv")
                    sug_folder = pixiv_identity_folders.get(pid)
                    for iid in img_ids:
                        if tid_artist > 0:
                            links.append((iid, tid_artist, 1.0, "pixiv"))
                        if sug_folder:
                            sug_updates.append((sug_folder, iid))
                            if auto_move_to_artist_folder and img_id_to_folder.get(iid, "") == "":
                                pending_moves.setdefault(sug_folder, []).append(iid)

                tags_obj = body.get("tags", {}).get("tags", [])
                for t_item in tags_obj:
                    t_str = t_item.get("tag")
                    if t_str and t_str not in ("R-18", "オリジナル"):
                        from reverse_search import get_known_tag_category
                        tid_t = ensure_tag(c, tag_cache, t_str, get_known_tag_category(t_str), source="pixiv")
                        if tid_t > 0:
                            for iid in img_ids:
                                links.append((iid, tid_t, 1.0, "pixiv"))

            # 寫入 Google / Yande.re 救援查獲的結果
            for pid, clue in google_web_rescued.items():
                img_ids = pid_to_img_ids.get(pid, [])
                for art_name in clue.get("artists", []):
                    tid_artist = ensure_tag(c, tag_cache, art_name, "artist", source="pixiv")
                    sug_folder = resolve_artist_folder_name(art_name)
                    for iid in img_ids:
                        if tid_artist > 0:
                            links.append((iid, tid_artist, 0.95, "pixiv"))
                        if sug_folder:
                            sug_updates.append((sug_folder, iid))
                            if auto_move_to_artist_folder and img_id_to_folder.get(iid, "") == "":
                                pending_moves.setdefault(sug_folder, []).append(iid)
                for t_name, t_cat in clue.get("tags", []):
                    tid_t = ensure_tag(c, tag_cache, t_name, t_cat, source="pixiv")
                    if tid_t > 0:
                        for iid in img_ids:
                            links.append((iid, tid_t, 0.92, "pixiv"))

            # 對本次所有查驗過的圖片寫入 source='pixiv_checked'，徹底解決 404 死鎖
            if checked_tid > 0:
                for r in rows:
                    pid = str(r["pixiv_id"])
                    status_code = pixiv_results.get(pid)
                    if pid in resolved_pids:
                        state, retry, error = "succeeded",None,""
                    elif status_code in (404,410):
                        state, retry, error = "not_found",time.time()+7*86400,f"HTTP {status_code}"
                    else:
                        state, retry, error = "failed",time.time()+300,f"HTTP {status_code or 'timeout'}"
                    record_processing(c,r["id"],"pixiv",state,error=error,retry_at=retry)
                    if state != "failed":
                        links.append((r["id"], checked_tid, 1.0, "pixiv_checked"))
                    else:
                        c.execute("DELETE FROM image_tags WHERE image_id=? AND source='pixiv_checked'",(r["id"],))

            if links:
                c.executemany(
                    "INSERT OR IGNORE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, ?, ?)",
                    links,
                )
            if sug_updates:
                c.executemany(
                    "UPDATE images SET suggested_folder = COALESCE(?, suggested_folder) WHERE id = ?",
                    sug_updates,
                )
            refresh_tag_counts(c)

        run_db_write_with_retry(db_conn, _write_pixiv_batch)
    finally:
        db_conn.close()

    save_persistent_artist_cache()

    moved_total = 0
    if auto_move_to_artist_folder and pending_moves:
        from gallery_service import move_images_to_artist_folder as move_images_to_folder
        for target_f, iids in pending_moves.items():
            unique_iids = list(dict.fromkeys(iids))
            m_res = move_images_to_folder(unique_iids, target_f, auto_rename=True, refresh_counts=False)
            moved_total += m_res.get("moved", 0)
        db_conn = get_db()
        try:
            run_db_write_with_retry(db_conn, lambda c: refresh_tag_counts(c))
        finally:
            db_conn.close()

    pixiv_elapsed = round(time.time() - t_pixiv_start, 2)
    not_found_404 = len(pid_to_img_ids) - found_works
    result_dict = {
        "checked_illusts": len(pid_to_img_ids),
        "found_illusts": found_works,
        "rescued_by_danbooru": rescued_by_danbooru,
        "rescued_by_pixiv_api": rescued_by_pixiv_api,
        "rescued_by_google_web": rescued_by_google_web,
        "not_found_404_marked_checked": not_found_404,
        "images_updated": len(rows),
        "moved_to_artist_folders": moved_total,
    }
    log_operation(
        category="search_calibrate",
        action="Pixiv ID 批次反查與 404 救援",
        summary=(
            f"查驗 {len(pid_to_img_ids)} 個作品 ({len(rows)} 張圖片)：命中/救回 {found_works} 個 "
            f"(Danbooru {rescued_by_danbooru}、Pixiv API {rescued_by_pixiv_api}、網頁搜圖 {rescued_by_google_web})，"
            f"標記已查驗 404 共 {not_found_404} 個"
            + (f"，自動歸檔 {moved_total} 張" if moved_total else "")
        ),
        status="success",
        affected_count=len(rows),
        duration_sec=pixiv_elapsed,
        details=result_dict,
    )
    return result_dict


tagger_engine = AITaggerEngine()
