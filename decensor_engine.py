# -*- coding: utf-8 -*-
"""
本機 ONNX DirectML 二次元專用去馬賽克引擎 (decensor_engine.py)
結合：
1. YOLOv11-seg 二次元馬賽克/黑條像素級實例分割模型 (kitsumed/yolov11_seg-2d-censorship-detection: model_fp16_dynamic.onnx)
   — 多尺度 (640x640 + 960x960) 聯合推論，直接輸出精準貼合馬賽克與黑條的 Polygon 像素遮罩，絕不誤傷臉部、頭髮或背景。
2. YOLOv8 二次元部位輔助召回模型 (deepghs/anime_censor_detection: censor_detect_v1.0_s/model.onnx)
   — 搭配「純馬賽克方格紋理 (Step-Edge + Flat-Cell) 與黑條像素驗證」，補捉極細微漏網馬賽克，且不對無碼部位誤觸發。
3. 智慧邊緣擴展與分塊重繪 (Sub-window Tiling) + DeepCreamPy v2 ONNX 局部重繪模型 (vadik82/dcp-mosaic-onnx & vadik82/dcp-bar-onnx)
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
SEG_LABELS = ["bar_censor", "mosaic_censoring"]


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


def _detect_mosaic_grid_pixels(roi_rgb: np.ndarray) -> np.ndarray:
    """
    純物理紋理偵測：找出 ROI (H, W, 3) 內真正具有「馬賽克方格 (內平坦 + 週期性階梯邊緣)」的像素遮罩 (bool)。
    不使用任何強制大橢圓，因此絕不會把框內的正常臉部、五官、髮絲或平滑陰影當成馬賽克。
    """
    if roi_rgb.size == 0 or roi_rgb.shape[0] < 8 or roi_rgb.shape[1] < 8:
        return np.zeros(roi_rgb.shape[:2], dtype=bool)

    gray = roi_rgb.astype(np.float32).mean(axis=2)
    gx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    gy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))

    # 馬賽克方格內部梯度趨近於 0，而方格交界處具有明顯階梯跳變 (Step Edge)
    flat_pixels = (gx < 2.2) & (gy < 2.2)
    step_edges = ((gx > 3.8) & (gx < 48.0)) | ((gy > 3.8) & (gy < 48.0))

    flat_pil = Image.fromarray(flat_pixels.astype(np.uint8) * 255, mode="L")
    step_pil = Image.fromarray(step_edges.astype(np.uint8) * 255, mode="L")

    # 必須同時具備「區塊內部平坦」且「周圍 9px 內有馬賽克格線跳變」
    flat_dense = np.asarray(flat_pil.filter(ImageFilter.MaxFilter(5)), dtype=np.uint8) > 128
    step_dense = np.asarray(step_pil.filter(ImageFilter.MaxFilter(9)), dtype=np.uint8) > 128

    raw_mosaic = (flat_dense & step_dense).astype(np.uint8) * 255
    closed_pil = (
        Image.fromarray(raw_mosaic, mode="L")
        .filter(ImageFilter.MinFilter(3))
        .filter(ImageFilter.MaxFilter(11))
        .filter(ImageFilter.MinFilter(5))
    )
    return np.asarray(closed_pil, dtype=np.uint8) > 128


def _detect_bar_pixels(roi_rgb: np.ndarray) -> np.ndarray:
    """偵測 ROI 內的黑條 (Dark Bar) 或純白條 (White Bar) 像素遮罩 (bool)"""
    if roi_rgb.size == 0:
        return np.zeros(roi_rgb.shape[:2], dtype=bool)
    roi_f = roi_rgb.astype(np.float32)
    gray = roi_f.mean(axis=2)
    spread = roi_f.max(axis=2) - roi_f.min(axis=2)
    dark_bar = (gray < 28.0) & (spread < 15.0)
    white_bar = (gray > 246.0) & (spread < 10.0)
    return dark_bar | white_bar


def _build_region_mask_256(
    crop_rgb_256: np.ndarray,
    inner_box_256: Tuple[int, int, int, int],
    mode: str = "auto",
    seg_mask_256: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, str]:
    """
    針對 256x256 局部區塊建立精準去碼遮罩 (1.0 = 保留原圖, 0.0 = 需去馬賽克/去黑條重繪區域)。
    - 若提供 YOLOv11-seg 的像素級多邊形遮罩 (seg_mask_256)，直接使用該多邊形並自動向外吸收邊緣殘留的馬賽克方格，
      100% 不傷及周圍臉部、頭髮與背景，且不裁切邊界！
    - 若無 seg_mask_256 (如手動框選或輔助召回)，則僅針對真實馬賽克方格紋理與黑條建立遮罩，不再畫盲目大橢圓。
    """
    ix1, iy1, ix2, iy2 = inner_box_256
    ix1 = max(0, min(252, ix1))
    iy1 = max(0, min(252, iy1))
    ix2 = max(ix1 + 4, min(256, ix2))
    iy2 = max(iy1 + 4, min(256, iy2))

    roi = crop_rgb_256[iy1:iy2, ix1:ix2].astype(np.float32)
    bar_pixels = _detect_bar_pixels(roi)
    bar_ratio = float(bar_pixels.mean()) if bar_pixels.size > 0 else 0.0

    actual_mode = mode
    if actual_mode == "auto":
        actual_mode = "bar" if bar_ratio >= 0.22 else "mosaic"

    if seg_mask_256 is not None and np.any(seg_mask_256 > 0.30):
        # 1. 以 YOLOv11-seg 像素級多邊形遮罩為核心
        base_bin = (seg_mask_256 > 0.32).astype(np.uint8) * 255
        base_pil = Image.fromarray(base_bin, mode="L")
        # 閉合內部小孔洞，並向外適度擴張以包覆最外圈的半格馬賽克
        dilated_core = base_pil.filter(ImageFilter.MaxFilter(11)).filter(ImageFilter.MinFilter(3))

        # 2. 在多邊形外圍 18px 環帶內，自動檢查是否還有殘留的馬賽克方格或黑條像素（解決邊緣漏掃問題）
        fringe_band = np.asarray(base_pil.filter(ImageFilter.MaxFilter(21)), dtype=np.uint8) > 128
        if actual_mode == "bar":
            full_bar = _detect_bar_pixels(crop_rgb_256)
            extra = fringe_band & full_bar
        else:
            full_mos = _detect_mosaic_grid_pixels(crop_rgb_256)
            extra = fringe_band & full_mos

        merged = ((np.asarray(dilated_core, dtype=np.uint8) > 128) | extra).astype(np.uint8) * 255
        pil_m = (
            Image.fromarray(merged, mode="L")
            .filter(ImageFilter.MaxFilter(7))
            .filter(ImageFilter.MinFilter(3))
        )
    elif actual_mode == "bar" and bar_ratio >= 0.08:
        mask_2d = np.zeros((256, 256), dtype=np.uint8)
        mask_2d[iy1:iy2, ix1:ix2] = bar_pixels.astype(np.uint8) * 255
        pil_m = Image.fromarray(mask_2d, mode="L").filter(ImageFilter.MaxFilter(9))
    else:
        actual_mode = "mosaic"
        mask_2d = np.zeros((256, 256), dtype=np.uint8)
        mos_pixels = _detect_mosaic_grid_pixels(roi)
        if float(mos_pixels.mean()) >= 0.05:
            mask_2d[iy1:iy2, ix1:ix2] = mos_pixels.astype(np.uint8) * 255
            pil_m = (
                Image.fromarray(mask_2d, mode="L")
                .filter(ImageFilter.MaxFilter(9))
                .filter(ImageFilter.MinFilter(3))
            )
        else:
            # 僅當使用者手動自訂框選 (custom_boxes) 時才對框內進行柔和遮罩
            mask_2d[iy1:iy2, ix1:ix2] = 255
            pil_m = Image.fromarray(mask_2d, mode="L")

    mask_arr = (np.asarray(pil_m, dtype=np.float32) > 96.0).astype(np.float32)
    # DCP contract: 1.0 = keep original, 0.0 = reconstruct censored area
    keep_mask = 1.0 - mask_arr
    keep_mask_3c = np.repeat(keep_mask[:, :, None], 3, axis=2)
    return keep_mask_3c, actual_mode


class AnimeDecensorEngine:
    def __init__(self):
        self.seg_session = None
        self.yolo_session = None
        self.mosaic_session = None
        self.bar_session = None
        self.active_provider: str = "尚未載入"
        self.model_loaded: bool = False
        self._lock = threading.Lock()

    def ensure_models_loaded(self):
        if self.model_loaded and self.seg_session is not None and self.mosaic_session is not None:
            return

        with self._lock:
            if self.model_loaded and self.seg_session is not None and self.mosaic_session is not None:
                return

            import onnxruntime as ort
            from huggingface_hub import hf_hub_download

            MODEL_DIR.mkdir(parents=True, exist_ok=True)

            # 1. YOLOv11-seg 二次元馬賽克/黑條像素級實例分割模型 (主力模型)
            seg_local = MODEL_DIR / "yolov11_seg" / "model_fp16_dynamic.onnx"
            if seg_local.exists():
                seg_path = str(seg_local)
            else:
                seg_path = hf_hub_download(
                    repo_id="kitsumed/yolov11_seg-2d-censorship-detection",
                    filename="model_fp16_dynamic.onnx",
                    local_dir=str(MODEL_DIR / "yolov11_seg"),
                )

            # 2. YOLOv8 部位偵測模型 (輔助召回)
            yolo_local = MODEL_DIR / "censor_detect_v1.0_s" / "model.onnx"
            if yolo_local.exists():
                yolo_path = str(yolo_local)
            else:
                yolo_path = hf_hub_download(
                    repo_id="deepghs/anime_censor_detection",
                    filename="censor_detect_v1.0_s/model.onnx",
                    local_dir=str(MODEL_DIR),
                )

            # 3. DeepCreamPy v2 ONNX 局部重繪模型
            mosaic_local = MODEL_DIR / "decensor_mosaic.onnx"
            if mosaic_local.exists():
                mosaic_path = str(mosaic_local)
            else:
                mosaic_path = hf_hub_download(
                    repo_id="vadik82/dcp-mosaic-onnx",
                    filename="decensor_mosaic.onnx",
                    local_dir=str(MODEL_DIR),
                )

            bar_local = MODEL_DIR / "decensor_bar.onnx"
            if bar_local.exists():
                bar_path = str(bar_local)
            else:
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

            self.seg_session = ort.InferenceSession(seg_path, sess_options=sess_opts, providers=providers)
            self.yolo_session = ort.InferenceSession(yolo_path, sess_options=sess_opts, providers=providers)
            self.mosaic_session = ort.InferenceSession(mosaic_path, sess_options=sess_opts, providers=providers)
            self.bar_session = ort.InferenceSession(bar_path, sess_options=sess_opts, providers=providers)
            self.active_provider = self.seg_session.get_providers()[0]
            self.model_loaded = True

    def unload_models(self) -> Dict[str, Any]:
        with self._lock:
            was_loaded = self.model_loaded
            self.seg_session = None
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

    def _run_yolo11_seg_single_scale(
        self,
        img_rgb: Image.Image,
        input_size: int = 640,
        conf_thresh: float = 0.18,
    ) -> List[Dict[str, Any]]:
        """在指定尺度 (640 或 960) 執行 YOLOv11-seg 實例分割，回傳各區域 bbox 與原圖尺寸的像素遮罩"""
        W, H = img_rgb.size
        scale = min(input_size / W, input_size / H)
        nw, nh = int(round(W * scale)), int(round(H * scale))
        pad_x = (input_size - nw) // 2
        pad_y = (input_size - nh) // 2

        canvas = Image.new("RGB", (input_size, input_size), (114, 114, 114))
        canvas.paste(img_rgb.resize((nw, nh), Image.Resampling.BILINEAR), (pad_x, pad_y))

        arr = np.transpose(np.asarray(canvas, dtype=np.float32) / 255.0, (2, 0, 1))[None]
        out0, out1 = self.seg_session.run(None, {"images": arr})
        preds = out0[0]   # (38, N)
        protos = out1[0]  # (32, mh, mw)

        cls_probs = preds[4:6, :]
        scores = cls_probs.max(axis=0)
        cls_ids = cls_probs.argmax(axis=0)

        valid = scores >= conf_thresh
        if not np.any(valid):
            return []

        cx, cy, bw, bh = preds[0, valid], preds[1, valid], preds[2, valid], preds[3, valid]
        v_scores = scores[valid]
        v_cls = cls_ids[valid]
        v_coeffs = preds[6:, valid].T  # (M, 32)

        x1 = np.clip((cx - bw / 2.0 - pad_x) / scale, 0, W)
        y1 = np.clip((cy - bh / 2.0 - pad_y) / scale, 0, H)
        x2 = np.clip((cx + bw / 2.0 - pad_x) / scale, 0, W)
        y2 = np.clip((cy + bh / 2.0 - pad_y) / scale, 0, H)

        boxes = np.stack([x1, y1, x2, y2], axis=1)
        keep = _nms_boxes(boxes, v_scores, iou_thresh=0.45)

        mh, mw = protos.shape[1], protos.shape[2]
        protos_flat = protos.reshape(32, -1)
        stride_m = input_size / float(mh)

        mx1 = max(0, int(round(pad_x / stride_m)))
        my1 = max(0, int(round(pad_y / stride_m)))
        mx2 = min(mw, max(mx1 + 1, int(round((pad_x + nw) / stride_m))))
        my2 = min(mh, max(my1 + 1, int(round((pad_y + nh) / stride_m))))

        results = []
        for idx in keep:
            bx1, by1, bx2, by2 = [int(round(v)) for v in boxes[idx]]
            if (bx2 - bx1) < 8 or (by2 - by1) < 8:
                continue
            coeff = v_coeffs[idx]
            logits = (coeff @ protos_flat).reshape(mh, mw)
            logits = np.clip(logits, -20.0, 20.0)
            mask_proto = 1.0 / (1.0 + np.exp(-logits))
            mask_unpad = mask_proto[my1:my2, mx1:mx2]

            # 僅將 bbox 對應範圍放大回原圖尺寸，節省記憶體與計算時間
            ux1 = max(0, int(np.floor(bx1 * (mx2 - mx1) / float(W))))
            uy1 = max(0, int(np.floor(by1 * (my2 - my1) / float(H))))
            ux2 = min(mx2 - mx1, max(ux1 + 1, int(np.ceil(bx2 * (mx2 - mx1) / float(W)))))
            uy2 = min(my2 - my1, max(uy1 + 1, int(np.ceil(by2 * (my2 - my1) / float(H)))))

            sub_proto = mask_unpad[uy1:uy2, ux1:ux2]
            sub_pil = Image.fromarray((np.clip(sub_proto, 0.0, 1.0) * 255.0).astype(np.uint8), mode="L").resize(
                (bx2 - bx1, by2 - by1), Image.Resampling.BILINEAR
            )
            sub_mask = np.asarray(sub_pil, dtype=np.float32) / 255.0
            if float((sub_mask > 0.35).mean()) < 0.03:
                continue

            # 根據實際像素遮罩收緊 bbox，避免包含無關區域
            ys, xs = np.where(sub_mask > 0.32)
            if ys.size > 0 and xs.size > 0:
                pad_b = 6
                tx1 = max(0, int(xs.min()) - pad_b)
                ty1 = max(0, int(ys.min()) - pad_b)
                tx2 = min(bx2 - bx1, int(xs.max()) + 1 + pad_b)
                ty2 = min(by2 - by1, int(ys.max()) + 1 + pad_b)
                sub_mask = sub_mask[ty1:ty2, tx1:tx2]
                bx1, by1, bx2, by2 = bx1 + tx1, by1 + ty1, bx1 + tx2, by1 + ty2

            cid = int(v_cls[idx])
            results.append({
                "bbox": [bx1, by1, bx2, by2],
                "label": SEG_LABELS[cid] if cid < len(SEG_LABELS) else "mosaic_censoring",
                "score": round(float(v_scores[idx]), 4),
                "sub_mask": sub_mask,
                "source": f"yolo11_seg_{input_size}",
            })
        return results

    def _run_yolov8_anatomy_fallback(
        self,
        img_rgb: Image.Image,
        conf_thresh: float = 0.22,
    ) -> List[Dict[str, Any]]:
        """使用 YOLOv8 (censor_detect_v1.0_s) 偵測可能漏網的局部遮罩候選框"""
        W, H = img_rgb.size
        input_size = 640
        scale = min(input_size / W, input_size / H)
        nw, nh = int(round(W * scale)), int(round(H * scale))
        pad_x = (input_size - nw) // 2
        pad_y = (input_size - nh) // 2

        canvas = Image.new("RGB", (input_size, input_size), (114, 114, 114))
        canvas.paste(img_rgb.resize((nw, nh), Image.Resampling.BILINEAR), (pad_x, pad_y))

        arr = np.transpose(np.asarray(canvas, dtype=np.float32) / 255.0, (2, 0, 1))[None]
        preds = self.yolo_session.run(None, {"images": arr})[0][0]

        cx, cy, bw, bh = preds[0, :], preds[1, :], preds[2, :], preds[3, :]
        class_probs = preds[4:, :]
        scores = class_probs.max(axis=0)
        cls_ids = class_probs.argmax(axis=0)

        valid = scores >= conf_thresh
        if not np.any(valid):
            return []

        cx, cy, bw, bh = cx[valid], cy[valid], bw[valid], bh[valid]
        scores = scores[valid]
        cls_ids = cls_ids[valid]

        x1 = np.clip((cx - bw / 2.0 - pad_x) / scale, 0, W)
        y1 = np.clip((cy - bh / 2.0 - pad_y) / scale, 0, H)
        x2 = np.clip((cx + bw / 2.0 - pad_x) / scale, 0, W)
        y2 = np.clip((cy + bh / 2.0 - pad_y) / scale, 0, H)

        boxes = np.stack([x1, y1, x2, y2], axis=1)
        keep_indices = _nms_boxes(boxes, scores, iou_thresh=0.42)

        results = []
        for idx in keep_indices:
            bx1, by1, bx2, by2 = [int(round(v)) for v in boxes[idx]]
            if (bx2 - bx1) < 10 or (by2 - by1) < 10:
                continue
            cid = int(cls_ids[idx])
            results.append({
                "bbox": [bx1, by1, bx2, by2],
                "label": YOLO_LABELS[cid] if cid < len(YOLO_LABELS) else "censor",
                "score": round(float(scores[idx]), 4),
            })
        return results

    def detect_censor_boxes(
        self,
        img_rgb: Image.Image,
        conf_thresh: float = 0.18,
        include_masks: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        多尺度聯合偵測 (YOLOv11-seg 640 + 960 像素級遮罩 + YOLOv8 紋理驗證補充)：
        1. 合併 640x640 與 960x960 兩個尺度的 YOLOv11-seg 像素級多邊形遮罩，兼顧大範圍馬賽克與極小局部馬賽克。
        2. 對於 YOLOv8 偵測到但尚未被 YOLOv11-seg 覆蓋的候選框，僅在框內確實含有馬賽克方格紋理或黑條時才精準切出該紋理區域。
        """
        self.ensure_models_loaded()
        W, H = img_rgb.size
        img_arr = np.asarray(img_rgb, dtype=np.float32)

        # 建立全圖級別的馬賽克與黑條像素機率圖 (H, W)
        full_mos_mask = np.zeros((H, W), dtype=np.float32)
        full_bar_mask = np.zeros((H, W), dtype=np.float32)
        score_map = np.zeros((H, W), dtype=np.float32)

        seg_640 = self._run_yolo11_seg_single_scale(img_rgb, input_size=640, conf_thresh=conf_thresh)
        seg_960 = self._run_yolo11_seg_single_scale(img_rgb, input_size=960, conf_thresh=conf_thresh)

        for det in seg_640 + seg_960:
            bx1, by1, bx2, by2 = det["bbox"]
            sm = det["sub_mask"]
            sc = det["score"]
            if det["label"] == "bar_censor":
                full_bar_mask[by1:by2, bx1:bx2] = np.maximum(full_bar_mask[by1:by2, bx1:bx2], sm)
            else:
                full_mos_mask[by1:by2, bx1:bx2] = np.maximum(full_mos_mask[by1:by2, bx1:bx2], sm)
            score_map[by1:by2, bx1:bx2] = np.maximum(score_map[by1:by2, bx1:bx2], (sm > 0.32).astype(np.float32) * sc)

        # 輔助召回：檢查 YOLOv8 部位框內是否有被漏掉的真實馬賽克方格或黑條
        union_seg = (full_mos_mask > 0.32) | (full_bar_mask > 0.32)
        v8_dets = self._run_yolov8_anatomy_fallback(img_rgb, conf_thresh=max(0.20, conf_thresh))
        for vd in v8_dets:
            bx1, by1, bx2, by2 = vd["bbox"]
            existing_cov = float(union_seg[by1:by2, bx1:bx2].mean()) if (bx2 > bx1 and by2 > by1) else 0.0
            if existing_cov >= 0.15:
                # 已被 YOLOv11-seg 覆蓋的區域，微調補充框內緊鄰的真實馬賽克紋理即可
                roi_rgb = img_arr[by1:by2, bx1:bx2]
                mos_px = _detect_mosaic_grid_pixels(roi_rgb)
                near_seg = np.asarray(
                    Image.fromarray(union_seg[by1:by2, bx1:bx2].astype(np.uint8) * 255, mode="L").filter(ImageFilter.MaxFilter(25)),
                    dtype=np.uint8,
                ) > 128
                add_px = (mos_px & near_seg).astype(np.float32) * 0.85
                full_mos_mask[by1:by2, bx1:bx2] = np.maximum(full_mos_mask[by1:by2, bx1:bx2], add_px)
                continue

            # 若該部位完全未被 YOLOv11-seg 標記，嚴格檢查是否真有馬賽克方格或黑條（避免誤傷無碼乳頭/部位）
            roi_rgb = img_arr[by1:by2, bx1:bx2]
            bar_px = _detect_bar_pixels(roi_rgb)
            if float(bar_px.mean()) >= 0.14:
                full_bar_mask[by1:by2, bx1:bx2] = np.maximum(full_bar_mask[by1:by2, bx1:bx2], bar_px.astype(np.float32))
                score_map[by1:by2, bx1:bx2] = np.maximum(score_map[by1:by2, bx1:bx2], bar_px.astype(np.float32) * vd["score"])
                continue

            mos_px = _detect_mosaic_grid_pixels(roi_rgb)
            if float(mos_px.mean()) >= 0.16 and vd["label"] in ("penis", "pussy"):
                full_mos_mask[by1:by2, bx1:bx2] = np.maximum(full_mos_mask[by1:by2, bx1:bx2], mos_px.astype(np.float32))
                score_map[by1:by2, bx1:bx2] = np.maximum(score_map[by1:by2, bx1:bx2], mos_px.astype(np.float32) * vd["score"])

        # 從全圖遮罩提取獨立的連通區塊或合併後的預覽/重繪區域
        combined_prob = np.maximum(full_mos_mask, full_bar_mask)
        if not np.any(combined_prob > 0.32):
            return []

        # 收集所有高信心非重疊區域框，並附上各自的像素級遮罩
        all_raw_boxes = []
        all_raw_scores = []
        all_raw_labels = []
        for det in seg_960 + seg_640:
            all_raw_boxes.append(det["bbox"])
            all_raw_scores.append(det["score"])
            all_raw_labels.append(det["label"])
        for vd in v8_dets:
            bx1, by1, bx2, by2 = vd["bbox"]
            if float((combined_prob[by1:by2, bx1:bx2] > 0.32).mean()) >= 0.12:
                all_raw_boxes.append([bx1, by1, bx2, by2])
                all_raw_scores.append(vd["score"])
                all_raw_labels.append(vd["label"])

        if not all_raw_boxes:
            return []

        boxes_np = np.asarray(all_raw_boxes, dtype=np.float32)
        scores_np = np.asarray(all_raw_scores, dtype=np.float32)
        keep_final = _nms_boxes(boxes_np, scores_np, iou_thresh=0.38)

        final_regions = []
        for idx in keep_final:
            bx1, by1, bx2, by2 = [int(round(v)) for v in boxes_np[idx]]
            # 將兩個尺度在該區域附近的遮罩完整納入（稍微向外擴 16px 檢查是否有超出單一尺度框的遮罩）
            ex1 = max(0, bx1 - 16)
            ey1 = max(0, by1 - 16)
            ex2 = min(W, bx2 + 16)
            ey2 = min(H, by2 + 16)
            sub_prob = combined_prob[ey1:ey2, ex1:ex2]
            ys, xs = np.where(sub_prob > 0.32)
            if ys.size == 0:
                continue
            rx1 = max(0, ex1 + int(xs.min()) - 4)
            ry1 = max(0, ey1 + int(ys.min()) - 4)
            rx2 = min(W, ex1 + int(xs.max()) + 5)
            ry2 = min(H, ey1 + int(ys.max()) + 5)
            if (rx2 - rx1) < 8 or (ry2 - ry1) < 8:
                continue

            item = {
                "bbox": [rx1, ry1, rx2, ry2],
                "label": all_raw_labels[idx],
                "score": round(float(scores_np[idx]), 4),
            }
            if include_masks:
                item["sub_mask"] = combined_prob[ry1:ry2, rx1:rx2].copy()
                item["is_bar"] = bool(
                    full_bar_mask[ry1:ry2, rx1:rx2].mean() > full_mos_mask[ry1:ry2, rx1:rx2].mean()
                )
            final_regions.append(item)

        return final_regions

    def _generate_crop_windows_for_region(
        self,
        reg: Dict[str, Any],
        W: int,
        H: int,
    ) -> List[Tuple[int, int, int, int, int, int, int, int]]:
        """
        針對單一遮罩區域產生一個或多個正方形 Context 重繪視窗 (wx1, wy1, wx2, wy2, bx1, by1, bx2, by2)。
        若馬賽克區域為大範圍長條/斜向區域 (例如長寬比 > 1.5 且長邊 > 280px)，自動切分為 2 個重疊的局部正方形視窗，
        確保馬賽克尖端與邊緣不會因大視窗縮放而被遺漏或模糊！
        """
        bx1, by1, bx2, by2 = reg["bbox"]
        bw = bx2 - bx1
        bh = by2 - by1
        sub_mask = reg.get("sub_mask")

        sub_boxes = [(bx1, by1, bx2, by2)]
        if sub_mask is not None and max(bw, bh) >= 260 and (max(bw, bh) / max(1.0, float(min(bw, bh)))) >= 1.42:
            # 將長條形/斜向遮罩拆成兩段重疊 30% 的子框，分別根據各自非零遮罩收緊邊界
            if bw >= bh:
                mid1 = int(round(bw * 0.62))
                mid2 = int(round(bw * 0.38))
                slices = [(0, mid1, 0, bh), (mid2, bw, 0, bh)]
            else:
                mid1 = int(round(bh * 0.62))
                mid2 = int(round(bh * 0.38))
                slices = [(0, bw, 0, mid1), (0, bw, mid2, bh)]

            refined_subs = []
            for sx1, sx2, sy1, sy2 in slices:
                part = sub_mask[sy1:sy2, sx1:sx2]
                ys, xs = np.where(part > 0.32)
                if ys.size > 16:
                    px1 = max(0, bx1 + sx1 + int(xs.min()) - 4)
                    py1 = max(0, by1 + sy1 + int(ys.min()) - 4)
                    px2 = min(W, bx1 + sx1 + int(xs.max()) + 5)
                    py2 = min(H, by1 + sy1 + int(ys.max()) + 5)
                    refined_subs.append((px1, py1, px2, py2))
            if len(refined_subs) >= 2:
                sub_boxes = refined_subs

        windows = []
        for sbx1, sby1, sbx2, sby2 in sub_boxes:
            sbw = sbx2 - sbx1
            sbh = sby2 - sby1
            cx = (sbx1 + sbx2) / 2.0
            cy = (sby1 + sby2) / 2.0

            # 取約 1.45 倍的正方形 Context 視窗供 256x256 模型參考周圍膚色與線條
            side = int(round(max(sbw, sbh) * 1.45))
            side = max(64, min(max(W, H), side))
            half = side / 2.0

            wx1 = int(round(cx - half))
            wy1 = int(round(cy - half))
            wx2 = wx1 + side
            wy2 = wy1 + side

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

            if (wx2 - wx1) >= 16 and (wy2 - wy1) >= 16:
                windows.append((wx1, wy1, wx2, wy2, sbx1, sby1, sbx2, sby2))
        return windows

    def decensor_pil_image(
        self,
        img_rgb: Image.Image,
        mode: str = "auto",
        conf_thresh: float = 0.18,
        custom_boxes: Optional[List[List[float]]] = None,
    ) -> Tuple[Image.Image, List[Dict[str, Any]]]:
        """
        對單張 PIL Image (RGB) 執行 YOLOv11-seg 多尺度像素級遮罩偵測 (或自訂框選) + DeepCreamPy v2 ONNX 局部重繪去碼。
        回傳: (去碼後的 PIL.Image, 處理的區域清單)
        """
        self.ensure_models_loaded()
        W, H = img_rgb.size
        orig_arr = np.asarray(img_rgb, dtype=np.float32)
        work_arr = orig_arr.copy()

        regions: List[Dict[str, Any]] = []
        if custom_boxes:
            for cb in custom_boxes:
                if len(cb) == 4:
                    if max(cb) <= 1.05:
                        x1, y1, x2, y2 = int(cb[0] * W), int(cb[1] * H), int(cb[2] * W), int(cb[3] * H)
                    else:
                        x1, y1, x2, y2 = int(cb[0]), int(cb[1]), int(cb[2]), int(cb[3])
                    x1, x2 = max(0, min(x1, x2)), min(W, max(x1, x2))
                    y1, y2 = max(0, min(y1, y2)), min(H, max(y1, y2))
                    if (x2 - x1) >= 8 and (y2 - y1) >= 8:
                        regions.append({"bbox": [x1, y1, x2, y2], "label": "custom_region", "score": 1.0})
        else:
            regions = self.detect_censor_boxes(img_rgb, conf_thresh=conf_thresh, include_masks=True)
            if not regions and conf_thresh > 0.12:
                regions = self.detect_censor_boxes(img_rgb, conf_thresh=0.12, include_masks=True)

        if not regions:
            return img_rgb, []

        # 建立全圖級別的像素遮罩以便任意子視窗直接裁切對應的 256x256 遮罩
        global_seg_mask = np.zeros((H, W), dtype=np.float32)
        has_global_seg = False
        for reg in regions:
            sm = reg.get("sub_mask")
            if sm is not None:
                rx1, ry1, rx2, ry2 = reg["bbox"]
                global_seg_mask[ry1:ry2, rx1:rx2] = np.maximum(global_seg_mask[ry1:ry2, rx1:rx2], sm)
                has_global_seg = True

        processed_regions = []
        for reg in regions:
            bx1, by1, bx2, by2 = reg["bbox"]
            reg_mode = mode
            if reg_mode == "auto" and reg.get("is_bar"):
                reg_mode = "bar"

            windows = self._generate_crop_windows_for_region(reg, W, H)
            used_mode_final = "mosaic"

            for wx1, wy1, wx2, wy2, sbx1, sby1, sbx2, sby2 in windows:
                cw = wx2 - wx1
                ch = wy2 - wy1
                # 從原圖取未修改的底圖，確保重疊子視窗輸入穩定，再將結果融合至 work_arr
                crop_pil = Image.fromarray(np.clip(work_arr[wy1:wy2, wx1:wx2], 0, 255).astype(np.uint8), mode="RGB")
                crop_256_pil = crop_pil.resize((256, 256), Image.Resampling.BICUBIC)
                crop_256_arr = np.asarray(crop_256_pil, dtype=np.float32)

                ix1 = int(round((sbx1 - wx1) * 256.0 / cw))
                iy1 = int(round((sby1 - wy1) * 256.0 / ch))
                ix2 = int(round((sbx2 - wx1) * 256.0 / cw))
                iy2 = int(round((sby2 - wy1) * 256.0 / ch))

                seg_crop_256 = None
                if has_global_seg:
                    # 僅保留當前子框 (sbx1..sbx2, sby1..sby2) 附近的像素遮罩
                    win_seg = np.zeros((ch, cw), dtype=np.float32)
                    lx1, ly1 = max(0, sbx1 - wx1 - 8), max(0, sby1 - wy1 - 8)
                    lx2, ly2 = min(cw, sbx2 - wx1 + 8), min(ch, sby2 - wy1 + 8)
                    win_seg[ly1:ly2, lx1:lx2] = global_seg_mask[wy1 + ly1 : wy1 + ly2, wx1 + lx1 : wx1 + lx2]
                    seg_pil_256 = Image.fromarray((np.clip(win_seg, 0.0, 1.0) * 255.0).astype(np.uint8), mode="L").resize(
                        (256, 256), Image.Resampling.BILINEAR
                    )
                    seg_crop_256 = np.asarray(seg_pil_256, dtype=np.float32) / 255.0

                keep_mask_3c, used_mode = _build_region_mask_256(
                    crop_256_arr,
                    (ix1, iy1, ix2, iy2),
                    mode=reg_mode,
                    seg_mask_256=seg_crop_256,
                )
                used_mode_final = used_mode

                norm_img = (crop_256_arr / 127.5) - 1.0
                input_unused = norm_img[None, :, :, :].astype(np.float32)
                input_mask = keep_mask_3c[None, :, :, :].astype(np.float32)

                if used_mode == "bar":
                    input_img = (norm_img * keep_mask_3c)[None, :, :, :].astype(np.float32)
                    pred_256 = self.bar_session.run(
                        None,
                        {"input_image": input_img, "input_unused": input_unused, "input_mask": input_mask},
                    )[0][0]
                else:
                    input_img = input_unused
                    pred_256 = self.mosaic_session.run(
                        None,
                        {"input_image": input_img, "input_unused": input_unused, "input_mask": input_mask},
                    )[0][0]

                pred_rgb_256 = np.clip((pred_256 + 1.0) * 127.5, 0.0, 255.0).astype(np.uint8)
                pred_crop_pil = Image.fromarray(pred_rgb_256, mode="RGB").resize((cw, ch), Image.Resampling.LANCZOS)
                pred_crop_arr = np.asarray(pred_crop_pil, dtype=np.float32)

                # 建立羽化融合遮罩 (Feather Alpha Blend)，僅融合多邊形遮罩內部，100% 不影響框外的臉部與背景
                reconstruct_mask_256 = ((1.0 - keep_mask_3c[:, :, 0]) * 255.0).astype(np.uint8)
                alpha_pil = (
                    Image.fromarray(reconstruct_mask_256, mode="L")
                    .resize((cw, ch), Image.Resampling.BILINEAR)
                    .filter(ImageFilter.GaussianBlur(radius=max(2.0, min(cw, ch) * 0.02)))
                )
                alpha = (np.asarray(alpha_pil, dtype=np.float32) / 255.0)[:, :, None]

                orig_crop = work_arr[wy1:wy2, wx1:wx2]
                work_arr[wy1:wy2, wx1:wx2] = pred_crop_arr * alpha + orig_crop * (1.0 - alpha)

            processed_regions.append({
                "bbox": [bx1, by1, bx2, by2],
                "label": reg["label"],
                "score": reg["score"],
                "mode": used_mode_final,
                "windows": len(windows),
            })

        out_pil = Image.fromarray(np.clip(work_arr, 0, 255).astype(np.uint8), mode="RGB")
        return out_pil, processed_regions

    def preview_decensor_image(
        self,
        image_id: int,
        mode: str = "auto",
        conf_thresh: float = 0.18,
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
            "message": f"✨ 已在 {elapsed} 秒內透過 {self.active_provider} (YOLOv11-seg 像素級遮罩) 偵測並重繪 {len(regions)} 處馬賽克區域！",
            "preview_data_url": f"data:image/jpeg;base64,{b64_str}",
        }

    def apply_and_save_decensor(
        self,
        image_id: int,
        save_mode: str = "new_copy",
        mode: str = "auto",
        conf_thresh: float = 0.18,
        custom_boxes: Optional[List[List[float]]] = None,
        refresh_counts: bool = True,
    ) -> Dict[str, Any]:
        """
        執行去馬賽克並寫入硬碟：
        - save_mode='new_copy' (預設): 另存為 <原檔名>_decensored.<ext> 並寫入資料庫，100% 保留原圖
        - save_mode='overwrite': 直接更新原圖檔案並重新產生縮圖
        """
        t0 = time.time()
        conn = get_db()
        row = conn.execute(
            "SELECT id, rel_path, filename, folder, ext FROM images WHERE id = ?",
            (image_id,),
        ).fetchone()
        row_dict = dict(row) if row else None
        conn.close()
        if not row_dict:
            return {"ok": False, "success": False, "message": "找不到該圖片", "error": "找不到該圖片"}

        full_path = GALLERY_ROOT / row_dict["rel_path"]
        if not full_path.exists():
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
            return {
                "ok": False,
                "success": False,
                "regions_count": 0,
                "message": "未在此圖片偵測到馬賽克或黑條遮罩區域",
            }

        target_id = image_id
        target_rel_path = row_dict["rel_path"]

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
            conn = get_db()
            try:
                with conn:
                    cur = conn.execute(
                        """
                        INSERT INTO images (rel_path, filename, folder, ext, size_bytes, mtime, media_type, ai_tagged)
                        VALUES (?, ?, ?, ?, ?, ?, 'image', 1)
                        """,
                        (target_rel_path, new_fname, row_dict["folder"], ext.lower(), stat.st_size, stat.st_mtime),
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
            finally:
                conn.close()
        else:
            ext = full_path.suffix.lower()
            if ext in (".jpg", ".jpeg", ".webp"):
                out_pil.save(str(full_path), quality=95)
            else:
                out_pil.save(str(full_path))

            stat = full_path.stat()
            conn = get_db()
            try:
                with conn:
                    conn.execute(
                        "UPDATE images SET size_bytes = ?, mtime = ? WHERE id = ?",
                        (stat.st_size, stat.st_mtime, image_id),
                    )
            finally:
                conn.close()

            # 清除舊縮圖快取以便重新生成
            for old_thumb in (THUMB_DIR / f"{image_id}.webp", THUMB_DIR / f"{image_id}.jpg", *THUMB_DIR.glob(f"{image_id}-*.jpg")):
                if old_thumb.exists():
                    try:
                        old_thumb.unlink()
                    except Exception:
                        pass

        # 更新標籤：加入 uncensored / AI去馬賽克，移除 mosaic_censoring / bar_censor
        conn = get_db()
        try:
            tag_cache: Dict[str, int] = {}
            for r in conn.execute("SELECT id, name FROM tags WHERE name IN ('AI去馬賽克', 'uncensored')"):
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
                if refresh_counts:
                    refresh_tag_counts(conn)
        finally:
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
            "message": f"✅ 已成功透過 YOLOv11-seg 去除 {len(regions)} 處馬賽克/遮罩並{'覆蓋原圖' if save_mode == 'overwrite' else '另存新檔'}！",
        }


decensor_engine = AnimeDecensorEngine()
