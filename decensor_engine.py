# -*- coding: utf-8 -*-
"""
本機 ONNX DirectML 二次元專用去馬賽克引擎 (decensor_engine.py)
結合：
1. YOLOv8 二次元遮罩部位自動偵測模型 (deepghs/anime_censor_detection: censor_detect_v1.0_s/model.onnx)
2. 智慧馬賽克格紋 (Mosaic Grid) 與黑條 (Bar Censor) 像素級遮罩分割演算法
3. DeepCreamPy v2 ONNX 局部重繪模型 (vadik82/dcp-mosaic-onnx & vadik82/dcp-bar-onnx)
4. 支援 AMD Radeon RX 9070 XT (DmlExecutionProvider) 硬體加速與一鍵釋放顯卡記憶體 (VRAM)
"""

import base64
import gc
import io
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageFilter, ImageFile

from database import (
    GALLERY_ROOT,
    MODEL_DIR,
    THUMB_DIR,
    get_db,
    ensure_tag,
    refresh_tag_counts,
)

ImageFile.LOAD_TRUNCATED_IMAGES = True

YOLO_LABELS = ["nipple_f", "penis", "pussy"]


def _nms_boxes(boxes: np.ndarray, scores: np.ndarray, iou_thresh: float = 0.45) -> List[int]:
    """簡易高效的 Non-Maximum Suppression (boxes: [N, 4] as x1, y1, x2, y2)"""
    if len(boxes) == 0:
        return []
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        inds = np.where(ovr <= iou_thresh)[0]
        order = order[inds + 1]
    return keep


def _build_region_mask_256(
    crop_rgb_256: np.ndarray,
    inner_box_256: Tuple[int, int, int, int],
    mode: str = "auto",
) -> Tuple[np.ndarray, str]:
    """
    針對 256x256 局部區塊建立精準去碼遮罩 (1.0 = 保留原圖, 0.0 = 需去馬賽克/去黑條重繪區域)。
    自動判斷該區域屬於 'mosaic' (方格馬賽克) 或 'bar' (黑條/白條遮罩)。
    """
    ix1, iy1, ix2, iy2 = inner_box_256
    ix1 = max(16, min(240, ix1))
    iy1 = max(16, min(240, iy1))
    ix2 = max(ix1 + 12, min(244, ix2))
    iy2 = max(iy1 + 12, min(244, iy2))

    roi = crop_rgb_256[iy1:iy2, ix1:ix2].astype(np.float32)
    rh, rw = roi.shape[:2]

    # 1. 檢查是否有黑條 (Bar Censor) 或純白遮罩條
    gray = roi.mean(axis=2)
    channel_spread = roi.max(axis=2) - roi.min(axis=2)
    dark_bar_mask = (gray < 28.0) & (channel_spread < 15.0)
    white_bar_mask = (gray > 246.0) & (channel_spread < 10.0)
    bar_pixels = dark_bar_mask | white_bar_mask
    bar_ratio = float(bar_pixels.mean()) if bar_pixels.size > 0 else 0.0

    actual_mode = mode
    if actual_mode == "auto":
        actual_mode = "bar" if bar_ratio >= 0.18 else "mosaic"

    mask_2d = np.zeros((256, 256), dtype=np.uint8)

    if actual_mode == "bar" and bar_ratio >= 0.08:
        # 針對黑條/白條像素建立遮罩並向外膨脹 4px 以消除黑條邊緣殘影
        sub_m = (bar_pixels.astype(np.uint8)) * 255
        mask_2d[iy1:iy2, ix1:ix2] = sub_m
        pil_m = Image.fromarray(mask_2d, mode="L").filter(ImageFilter.MaxFilter(9))
    else:
        actual_mode = "mosaic"
        # 偵測 ROI 內的方格馬賽克區域 (Mosaic flat blocks + step edges)
        # 同時結合橢圓核心遮罩確保馬賽克中心 100% 被涵蓋，且不傷及四個角落的正常背景線條
        yy, xx = np.ogrid[:rh, :rw]
        cy, cx = rh / 2.0, rw / 2.0
        ry, rx = max(1.0, rh * 0.48), max(1.0, rw * 0.48)
        ellipse = (((yy - cy) / ry) ** 2 + ((xx - cx) / rx) ** 2) <= 1.05

        # 計算局部平坦度 (馬賽克方格內部梯度極小)
        gx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
        gy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
        flat_pixels = (gx < 3.5) & (gy < 3.5)

        combined_roi = (ellipse | (flat_pixels & (((yy - cy) / (ry * 1.18)) ** 2 + ((xx - cx) / (rx * 1.18)) ** 2 <= 1.0))).astype(np.uint8) * 255
        mask_2d[iy1:iy2, ix1:ix2] = combined_roi
        # 膨脹與閉合以完整覆蓋馬賽克格線邊界
        pil_m = (
            Image.fromarray(mask_2d, mode="L")
            .filter(ImageFilter.MaxFilter(7))
            .filter(ImageFilter.MinFilter(3))
        )

    mask_arr = (np.asarray(pil_m, dtype=np.float32) > 96.0).astype(np.float32)
    # DCP contract: 1.0 = keep original, 0.0 = reconstruct censored area
    keep_mask = 1.0 - mask_arr
    keep_mask_3c = np.repeat(keep_mask[:, :, None], 3, axis=2)
    return keep_mask_3c, actual_mode


class AnimeDecensorEngine:
    def __init__(self):
        self.yolo_session = None
        self.mosaic_session = None
        self.bar_session = None
        self.active_provider: str = "尚未載入"
        self.model_loaded: bool = False
        self._lock = threading.Lock()

    def ensure_models_loaded(self):
        if self.model_loaded and self.yolo_session is not None and self.mosaic_session is not None:
            return

        with self._lock:
            if self.model_loaded and self.yolo_session is not None and self.mosaic_session is not None:
                return

            import onnxruntime as ort
            from huggingface_hub import hf_hub_download

            MODEL_DIR.mkdir(parents=True, exist_ok=True)

            yolo_path = hf_hub_download(
                repo_id="deepghs/anime_censor_detection",
                filename="censor_detect_v1.0_s/model.onnx",
                local_dir=str(MODEL_DIR),
            )
            mosaic_path = hf_hub_download(
                repo_id="vadik82/dcp-mosaic-onnx",
                filename="decensor_mosaic.onnx",
                local_dir=str(MODEL_DIR),
            )
            bar_path = hf_hub_download(
                repo_id="vadik82/dcp-bar-onnx",
                filename="decensor_bar.onnx",
                local_dir=str(MODEL_DIR),
            )

            avail = ort.get_available_providers()
            providers = []
            if "DmlExecutionProvider" in avail:
                providers.append("DmlExecutionProvider")
            if "CUDAExecutionProvider" in avail:
                providers.append("CUDAExecutionProvider")
            providers.append("CPUExecutionProvider")

            sess_opts = ort.SessionOptions()
            sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

            self.yolo_session = ort.InferenceSession(yolo_path, sess_options=sess_opts, providers=providers)
            self.mosaic_session = ort.InferenceSession(mosaic_path, sess_options=sess_opts, providers=providers)
            self.bar_session = ort.InferenceSession(bar_path, sess_options=sess_opts, providers=providers)
            self.active_provider = self.mosaic_session.get_providers()[0]
            self.model_loaded = True

    def unload_models(self) -> Dict[str, Any]:
        with self._lock:
            was_loaded = self.model_loaded
            self.yolo_session = None
            self.mosaic_session = None
            self.bar_session = None
            self.model_loaded = False
            self.active_provider = "已卸載 (已釋放 VRAM)"
        gc.collect()
        return {
            "status": "unloaded",
            "was_loaded": was_loaded,
            "model_loaded": False,
        }

    def detect_censor_boxes(
        self,
        img_rgb: Image.Image,
        conf_thresh: float = 0.22,
    ) -> List[Dict[str, Any]]:
        """
        使用 YOLOv8 (censor_detect_v1.0_s) 偵測圖片中的遮罩區域，
        回傳原圖座標下的 bounding boxes: [{"bbox": [x1, y1, x2, y2], "label": str, "score": float}, ...]
        """
        self.ensure_models_loaded()
        W, H = img_rgb.size
        input_size = 640
        scale = min(input_size / W, input_size / H)
        nw, nh = int(round(W * scale)), int(round(H * scale))
        pad_x = (input_size - nw) // 2
        pad_y = (input_size - nh) // 2

        canvas = Image.new("RGB", (input_size, input_size), (114, 114, 114))
        canvas.paste(img_rgb.resize((nw, nh), Image.Resampling.BILINEAR), (pad_x, pad_y))

        arr = np.transpose(np.asarray(canvas, dtype=np.float32) / 255.0, (2, 0, 1))[None]
        preds = self.yolo_session.run(None, {"images": arr})[0][0]  # (7, 8400)

        cx = preds[0, :]
        cy = preds[1, :]
        bw = preds[2, :]
        bh = preds[3, :]
        class_probs = preds[4:, :]
        scores = class_probs.max(axis=0)
        cls_ids = class_probs.argmax(axis=0)

        valid = scores >= conf_thresh
        if not np.any(valid):
            return []

        cx, cy, bw, bh = cx[valid], cy[valid], bw[valid], bh[valid]
        scores = scores[valid]
        cls_ids = cls_ids[valid]

        # 轉回原圖像素座標 (x1, y1, x2, y2)
        x1 = np.clip((cx - bw / 2.0 - pad_x) / scale, 0, W)
        y1 = np.clip((cy - bh / 2.0 - pad_y) / scale, 0, H)
        x2 = np.clip((cx + bw / 2.0 - pad_x) / scale, 0, W)
        y2 = np.clip((cy + bh / 2.0 - pad_y) / scale, 0, H)

        boxes = np.stack([x1, y1, x2, y2], axis=1)
        keep_indices = _nms_boxes(boxes, scores, iou_thresh=0.42)

        results = []
        for idx in keep_indices:
            bx1, by1, bx2, by2 = boxes[idx]
            if (bx2 - bx1) < 8 or (by2 - by1) < 8:
                continue
            cid = int(cls_ids[idx])
            results.append({
                "bbox": [int(round(bx1)), int(round(by1)), int(round(bx2)), int(round(by2))],
                "label": YOLO_LABELS[cid] if cid < len(YOLO_LABELS) else "censor",
                "score": round(float(scores[idx]), 4),
            })
        return results

    def decensor_pil_image(
        self,
        img_rgb: Image.Image,
        mode: str = "auto",
        conf_thresh: float = 0.22,
        custom_boxes: Optional[List[List[float]]] = None,
    ) -> Tuple[Image.Image, List[Dict[str, Any]]]:
        """
        對單張 PIL Image (RGB) 執行自動偵測 (或自訂框選) + DeepCreamPy v2 ONNX 局部重繪去碼。
        回傳: (去碼後的 PIL.Image, 處理的區域清單)
        """
        self.ensure_models_loaded()
        W, H = img_rgb.size
        work_arr = np.asarray(img_rgb, dtype=np.float32).copy()

        regions: List[Dict[str, Any]] = []
        if custom_boxes:
            for cb in custom_boxes:
                if len(cb) == 4:
                    # 支援 0~1 正規化座標或像素座標
                    if max(cb) <= 1.05:
                        x1, y1, x2, y2 = int(cb[0] * W), int(cb[1] * H), int(cb[2] * W), int(cb[3] * H)
                    else:
                        x1, y1, x2, y2 = int(cb[0]), int(cb[1]), int(cb[2]), int(cb[3])
                    x1, x2 = max(0, min(x1, x2)), min(W, max(x1, x2))
                    y1, y2 = max(0, min(y1, y2)), min(H, max(y1, y2))
                    if (x2 - x1) >= 8 and (y2 - y1) >= 8:
                        regions.append({"bbox": [x1, y1, x2, y2], "label": "custom_region", "score": 1.0})
        else:
            regions = self.detect_censor_boxes(img_rgb, conf_thresh=conf_thresh)

        if not regions:
            return img_rgb, []

        processed_regions = []
        for reg in regions:
            bx1, by1, bx2, by2 = reg["bbox"]
            bw = bx2 - bx1
            bh = by2 - by1
            cx = (bx1 + bx2) / 2.0
            cy = (by1 + by2) / 2.0

            # 取約 1.75 倍的正方形 Context 視窗供 256x256 模型參考周圍膚色與線條
            side = int(round(max(bw, bh) * 1.75))
            side = max(64, min(max(W, H), side))
            half = side / 2.0

            wx1 = int(round(cx - half))
            wy1 = int(round(cy - half))
            wx2 = wx1 + side
            wy2 = wy1 + side

            # 邊界平移修正
            if wx1 < 0:
                wx2 -= wx1
                wx1 = 0
            if wy1 < 0:
                wy2 -= wy1
                wy1 = 0
            if wx2 > W:
                wx1 = max(0, wx1 - (wx2 - W))
                wx2 = W
            if wy2 > H:
                wy1 = max(0, wy1 - (wy2 - H))
                wy2 = H

            cw = wx2 - wx1
            ch = wy2 - wy1
            if cw < 16 or ch < 16:
                continue

            crop_pil = Image.fromarray(np.clip(work_arr[wy1:wy2, wx1:wx2], 0, 255).astype(np.uint8), mode="RGB")
            crop_256_pil = crop_pil.resize((256, 256), Image.Resampling.BICUBIC)
            crop_256_arr = np.asarray(crop_256_pil, dtype=np.float32)

            # 計算原 bbox 在 256x256 視窗中的相對座標
            ix1 = int(round((bx1 - wx1) * 256.0 / cw))
            iy1 = int(round((by1 - wy1) * 256.0 / ch))
            ix2 = int(round((bx2 - wx1) * 256.0 / cw))
            iy2 = int(round((by2 - wy1) * 256.0 / ch))

            keep_mask_3c, used_mode = _build_region_mask_256(crop_256_arr, (ix1, iy1, ix2, iy2), mode=mode)

            # 正規化至 [-1.0, 1.0]
            norm_img = (crop_256_arr / 127.5) - 1.0
            input_unused = norm_img[None, :, :, :].astype(np.float32)
            input_mask = keep_mask_3c[None, :, :, :].astype(np.float32)

            if used_mode == "bar":
                # Bar 模式：將遮罩處歸零後交由 decensor_bar.onnx 修補
                input_img = (norm_img * keep_mask_3c)[None, :, :, :].astype(np.float32)
                pred_256 = self.bar_session.run(
                    None,
                    {"input_image": input_img, "input_unused": input_unused, "input_mask": input_mask},
                )[0][0]
            else:
                # Mosaic 模式：保留馬賽克原有的色塊資訊供 decensor_mosaic.onnx 還原細節
                input_img = input_unused
                pred_256 = self.mosaic_session.run(
                    None,
                    {"input_image": input_img, "input_unused": input_unused, "input_mask": input_mask},
                )[0][0]

            pred_rgb_256 = np.clip((pred_256 + 1.0) * 127.5, 0.0, 255.0).astype(np.uint8)
            pred_crop_pil = Image.fromarray(pred_rgb_256, mode="RGB").resize((cw, ch), Image.Resampling.LANCZOS)
            pred_crop_arr = np.asarray(pred_crop_pil, dtype=np.float32)

            # 建立羽化融合遮罩 (Feather Alpha Blend)，確保接縫處 100% 平滑自然且不影響周圍原圖
            reconstruct_mask_256 = ((1.0 - keep_mask_3c[:, :, 0]) * 255.0).astype(np.uint8)
            alpha_pil = (
                Image.fromarray(reconstruct_mask_256, mode="L")
                .resize((cw, ch), Image.Resampling.BILINEAR)
                .filter(ImageFilter.GaussianBlur(radius=max(2.0, min(cw, ch) * 0.025)))
            )
            alpha = (np.asarray(alpha_pil, dtype=np.float32) / 255.0)[:, :, None]

            orig_crop = work_arr[wy1:wy2, wx1:wx2]
            work_arr[wy1:wy2, wx1:wx2] = pred_crop_arr * alpha + orig_crop * (1.0 - alpha)

            processed_regions.append({
                "bbox": [bx1, by1, bx2, by2],
                "label": reg["label"],
                "score": reg["score"],
                "mode": used_mode,
            })

        out_pil = Image.fromarray(np.clip(work_arr, 0, 255).astype(np.uint8), mode="RGB")
        return out_pil, processed_regions

    def preview_decensor_image(
        self,
        image_id: int,
        mode: str = "auto",
        conf_thresh: float = 0.22,
        custom_boxes: Optional[List[List[float]]] = None,
    ) -> Dict[str, Any]:
        """
        對指定圖片執行去馬賽克並回傳 Base64 預覽圖（尚未覆蓋硬碟原檔，供使用者對比預覽）
        """
        t0 = time.time()
        conn = get_db()
        row = conn.execute(
            "SELECT id, rel_path, filename, folder FROM images WHERE id = ?",
            (image_id,),
        ).fetchone()
        conn.close()
        if not row:
            return {"ok": False, "success": False, "message": "找不到該圖片", "error": "找不到該圖片"}

        full_path = GALLERY_ROOT / row["rel_path"]
        if not full_path.exists():
            return {"ok": False, "success": False, "message": "硬碟上找不到該圖片檔案", "error": "硬碟上找不到該圖片檔案"}

        with Image.open(str(full_path)) as img:
            img_rgb = img.convert("RGB")
            W, H = img_rgb.size

        out_pil, regions = self.decensor_pil_image(
            img_rgb,
            mode=mode,
            conf_thresh=conf_thresh,
            custom_boxes=custom_boxes,
        )
        if not regions:
            return {
                "ok": False,
                "success": False,
                "image_id": image_id,
                "regions_count": 0,
                "message": "未在此圖片偵測到二次元馬賽克或黑條遮罩區域",
            }

        buf = io.BytesIO()
        out_pil.save(buf, format="JPEG", quality=94)
        b64_str = base64.b64encode(buf.getvalue()).decode("ascii")
        elapsed = round(time.time() - t0, 2)

        return {
            "ok": True,
            "success": True,
            "image_id": image_id,
            "width": W,
            "height": H,
            "regions_count": len(regions),
            "regions": regions,
            "provider": self.active_provider,
            "elapsed_sec": elapsed,
            "message": f"✨ 已在 {elapsed} 秒內透過 {self.active_provider} 偵測並重繪 {len(regions)} 處馬賽克區域！",
            "preview_data_url": f"data:image/jpeg;base64,{b64_str}",
        }

    def apply_and_save_decensor(
        self,
        image_id: int,
        save_mode: str = "overwrite",
        mode: str = "auto",
        conf_thresh: float = 0.22,
        custom_boxes: Optional[List[List[float]]] = None,
    ) -> Dict[str, Any]:
        """
        執行去馬賽克並寫入硬碟：
        - save_mode='overwrite': 直接更新原圖檔案並重新產生縮圖
        - save_mode='new_copy': 另存為 <原檔名>_decensored.<ext> 並寫入資料庫
        """
        t0 = time.time()
        conn = get_db()
        row = conn.execute(
            "SELECT id, rel_path, filename, folder, ext FROM images WHERE id = ?",
            (image_id,),
        ).fetchone()
        if not row:
            conn.close()
            return {"ok": False, "success": False, "message": "找不到該圖片", "error": "找不到該圖片"}

        full_path = GALLERY_ROOT / row["rel_path"]
        if not full_path.exists():
            conn.close()
            return {"ok": False, "success": False, "message": "硬碟上找不到該圖片檔案", "error": "硬碟上找不到該圖片檔案"}

        with Image.open(str(full_path)) as img:
            img_rgb = img.convert("RGB")

        out_pil, regions = self.decensor_pil_image(
            img_rgb,
            mode=mode,
            conf_thresh=conf_thresh,
            custom_boxes=custom_boxes,
        )
        if not regions:
            conn.close()
            return {
                "ok": False,
                "success": False,
                "regions_count": 0,
                "message": "未在此圖片偵測到馬賽克或黑條遮罩區域",
            }

        target_id = image_id
        target_rel_path = row["rel_path"]

        if save_mode == "new_copy":
            stem = full_path.stem
            ext = full_path.suffix or ".png"
            new_fname = f"{stem}_decensored{ext}"
            new_full_path = full_path.parent / new_fname
            seq = 2
            while new_full_path.exists():
                new_fname = f"{stem}_decensored_{seq}{ext}"
                new_full_path = full_path.parent / new_fname
                seq += 1

            if ext.lower() in (".jpg", ".jpeg", ".webp"):
                out_pil.save(str(new_full_path), quality=95)
            else:
                out_pil.save(str(new_full_path))

            stat = new_full_path.stat()
            target_rel_path = str(new_full_path.relative_to(GALLERY_ROOT)).replace("\\", "/")
            with conn:
                cur = conn.execute(
                    """
                    INSERT INTO images (rel_path, filename, folder, ext, size_bytes, mtime, media_type, ai_tagged)
                    VALUES (?, ?, ?, ?, ?, ?, 'image', 1)
                    """,
                    (target_rel_path, new_fname, row["folder"], ext.lower(), stat.st_size, stat.st_mtime),
                )
                target_id = cur.lastrowid
                # 複製原圖既有標籤至新圖
                conn.execute(
                    """
                    INSERT OR IGNORE INTO image_tags (image_id, tag_id, confidence, source)
                    SELECT ?, tag_id, confidence, source FROM image_tags WHERE image_id = ?
                    """,
                    (target_id, image_id),
                )
        else:
            ext = full_path.suffix.lower()
            if ext in (".jpg", ".jpeg", ".webp"):
                out_pil.save(str(full_path), quality=95)
            else:
                out_pil.save(str(full_path))

            stat = full_path.stat()
            with conn:
                conn.execute(
                    "UPDATE images SET size_bytes = ?, mtime = ? WHERE id = ?",
                    (stat.st_size, stat.st_mtime, image_id),
                )
            # 清除舊縮圖快取以便重新生成
            thumb_file = THUMB_DIR / f"{image_id}.webp"
            if thumb_file.exists():
                try:
                    thumb_file.unlink()
                except Exception:
                    pass

        # 更新標籤：加入 uncensored / AI去馬賽克，移除 mosaic_censoring
        tag_cache: Dict[str, int] = {}
        for r in conn.execute("SELECT id, name FROM tags"):
            tag_cache[r["name"]] = r["id"]

        with conn:
            tid_dec = ensure_tag(conn, tag_cache, "AI去馬賽克", "meta")
            tid_unc = ensure_tag(conn, tag_cache, "uncensored", "general")
            conn.execute(
                "INSERT OR REPLACE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, 1.0, 'manual')",
                (target_id, tid_dec),
            )
            conn.execute(
                "INSERT OR REPLACE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, 1.0, 'manual')",
                (target_id, tid_unc),
            )
            conn.execute(
                """
                DELETE FROM image_tags
                WHERE image_id = ?
                  AND tag_id IN (SELECT id FROM tags WHERE name IN ('mosaic_censoring', 'bar_censor'))
                """,
                (target_id,),
            )
            refresh_tag_counts(conn)

        conn.close()
        return {
            "ok": True,
            "success": True,
            "image_id": target_id,
            "rel_path": target_rel_path,
            "save_mode": save_mode,
            "regions_count": len(regions),
            "regions": regions,
            "elapsed_sec": round(time.time() - t0, 2),
            "message": f"✅ 已成功去除 {len(regions)} 處馬賽克/遮罩並{'覆蓋原圖' if save_mode == 'overwrite' else '另存新檔'}！",
        }


decensor_engine = AnimeDecensorEngine()
