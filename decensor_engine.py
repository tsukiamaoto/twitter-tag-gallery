# -*- coding: utf-8 -*-
"""
本機 ONNX DirectML + GGUF Vulkan 二次元專用去馬賽克引擎 (decensor_engine.py)
結合：
1. YOLOv11-seg 二次元馬賽克/黑條像素級實例分割模型 (kitsumed/yolov11_seg-2d-censorship-detection: model_fp16_dynamic.onnx)
   — 多尺度 (640x640 + 960x960) 聯合推論，直接輸出精準貼合馬賽克與黑條的 Polygon 像素遮罩，絕不誤傷臉部、頭髮或背景。
2. YOLOv8 二次元部位輔助召回模型 (deepghs/anime_censor_detection: censor_detect_v1.0_s/model.onnx)
   — 搭配「純馬賽克方格紋理 (Step-Edge + Flat-Cell) 與黑條像素驗證」，補捉極細微漏網馬賽克，且不對無碼部位誤觸發。
3. 本機 GGUF 影像修復模型 (gguf_models.py / sd-server Vulkan + image_skills.py 主題技能，支援 FLUX.2 klein 4B 與 Qwen-Image-2.1)
   — 512x512 局部重繪與羽化融合，自動對齊周圍膚色、光影與線條。
4. 支援 AMD Radeon RX 9070 XT (DirectML + Vulkan) 硬體加速與一鍵釋放顯卡記憶體 (VRAM)
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
import requests
from PIL import Image, ImageFilter, ImageFile

from database import (
    GALLERY_ROOT,
    MODEL_DIR,
    THUMB_DIR,
    get_db,
    ensure_tag,
    refresh_tag_counts,
)
import gguf_models
import image_skills

ImageFile.LOAD_TRUNCATED_IMAGES = True

YOLO_LABELS = ["nipple_f", "penis", "pussy"]
SEG_LABELS = ["bar_censor", "mosaic_censoring"]

DEFAULT_DECENSOR_PROMPT = (
    "Remove all mosaic pixelation, white borders, censor bars, and blur in the masked region. "
    "Reconstruct and reinforce crisp continuous lineart, natural skin texture, subtle shading, "
    "highlights, and fine anatomical or clothing details that seamlessly match the surrounding "
    "colors, lighting, and anime illustration style. Eliminate any residual grid lines, white "
    "fringes, or blurry patches while keeping all unmasked areas completely intact."
)
DEFAULT_NEGATIVE_PROMPT = (
    "mosaic, censored, pixelation, pixelated, grid lines, white border, white bar, black bar, "
    "white fringe, halo, blurry, out of focus, smudged, lowres, jpeg artifacts, watermark, "
    "text, extra eyes, face on body, deformed"
)


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


def _pil_to_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _build_region_mask_512(
    crop_rgb_512: np.ndarray,
    inner_box_512: Tuple[int, int, int, int],
    mode: str = "auto",
    seg_mask_512: Optional[np.ndarray] = None,
) -> Tuple[Image.Image, str]:
    """
    針對 512x512 局部區塊建立 GGUF 擴散修復遮罩 (mode='L', 255 = 需去馬賽克/去黑條重繪區域, 0 = 保留原圖)。
    - 若提供 YOLOv11-seg 的像素級多邊形遮罩 (seg_mask_512)，直接採用高信心多邊形邊界 (threshold > 0.45) 並僅做極小羽化，
      絕不對周圍執行物理方格擴張 (_detect_mosaic_grid_pixels)，避免把手指賽璐璐平塗與線稿誤判為馬賽克而納入重繪！
    - 若無 seg_mask_512 (如手動框選或輔助召回)，才針對真實馬賽克方格紋理與黑條建立遮罩。
    """
    ix1, iy1, ix2, iy2 = inner_box_512
    ix1 = max(0, min(504, ix1))
    iy1 = max(0, min(504, iy1))
    ix2 = max(ix1 + 8, min(512, ix2))
    iy2 = max(iy1 + 8, min(512, iy2))

    roi = crop_rgb_512[iy1:iy2, ix1:ix2].astype(np.float32)
    bar_pixels = _detect_bar_pixels(roi)
    bar_ratio = float(bar_pixels.mean()) if bar_pixels.size > 0 else 0.0

    actual_mode = mode
    if actual_mode == "auto":
        actual_mode = "bar" if bar_ratio >= 0.22 else "mosaic"

    if seg_mask_512 is not None and np.any(seg_mask_512 > 0.42):
        base_bin = (seg_mask_512 > 0.45).astype(np.uint8) * 255
        pil_m = (
            Image.fromarray(base_bin, mode="L")
            .filter(ImageFilter.MaxFilter(3))
            .filter(ImageFilter.GaussianBlur(radius=1.5))
        )
    elif actual_mode == "bar" and bar_ratio >= 0.08:
        mask_2d = np.zeros((512, 512), dtype=np.uint8)
        mask_2d[iy1:iy2, ix1:ix2] = bar_pixels.astype(np.uint8) * 255
        pil_m = (
            Image.fromarray(mask_2d, mode="L")
            .filter(ImageFilter.MaxFilter(7))
            .filter(ImageFilter.GaussianBlur(radius=2.5))
        )
    else:
        actual_mode = "mosaic"
        mask_2d = np.zeros((512, 512), dtype=np.uint8)
        mos_pixels = _detect_mosaic_grid_pixels(roi)
        if float(mos_pixels.mean()) >= 0.05:
            mask_2d[iy1:iy2, ix1:ix2] = mos_pixels.astype(np.uint8) * 255
            pil_m = (
                Image.fromarray(mask_2d, mode="L")
                .filter(ImageFilter.MaxFilter(7))
                .filter(ImageFilter.MinFilter(3))
                .filter(ImageFilter.GaussianBlur(radius=2.5))
            )
        else:
            # 僅當使用者手動自訂框選 (custom_boxes) 時才對框內進行柔和遮罩
            mask_2d[iy1:iy2, ix1:ix2] = 255
            pil_m = Image.fromarray(mask_2d, mode="L").filter(ImageFilter.GaussianBlur(radius=3.0))

    return pil_m, actual_mode


class AnimeDecensorEngine:
    def __init__(self):
        self.seg_session = None
        self.yolo_session = None
        self.active_provider: str = "尚未載入"
        self.model_loaded: bool = False
        self._lock = threading.Lock()

    def ensure_models_loaded(self):
        """載入第一階段 YOLOv11-seg + YOLOv8 遮罩偵測模型 (ONNX DirectML/CPU)"""
        if self.model_loaded and self.seg_session is not None and self.yolo_session is not None:
            return

        with self._lock:
            if self.model_loaded and self.seg_session is not None and self.yolo_session is not None:
                return

            import onnxruntime as ort
            from huggingface_hub import hf_hub_download

            MODEL_DIR.mkdir(parents=True, exist_ok=True)

            # 1. YOLOv11-seg 二次元馬賽克/黑條像素級實例分割模型 (主力遮罩偵測)
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
            onnx_prov = self.seg_session.get_providers()[0]
            self.active_provider = f"{onnx_prov} + GGUF Vulkan"
            self.model_loaded = True

    def _resolve_decensor_skill_and_runtime(self) -> Dict[str, Any]:
        """確保第二階段 GGUF 擴散修復模型已啟動，並讀取模型設定與提示詞參數"""
        skill_prompt = DEFAULT_DECENSOR_PROMPT
        skill_seed = 42
        skill_steps = None
        skill_cfg = 6.0
        skill_profile = None
        preferred_model_id = None

        skills_state = image_skills.status()
        active_id = skills_state.get("active_id")
        skills_list = skills_state.get("skills") or []
        active_skill = next((s for s in skills_list if s.get("id") == active_id), None)
        if active_id and not active_skill:
            raise ValueError("套用中的影像修補 Skill 遺失或格式錯誤，請至 Skill 管理檢查")
        if not active_skill and skills_list:
            active_skill = skills_list[0]
        if active_skill:
            skill_prompt = active_skill["prompt"]
            skill_seed = active_skill["seed"]
            skill_steps = active_skill["steps"]
            skill_profile = active_skill["profile"]
            preferred_model_id = active_skill["model_id"]
        else:
            user_sel = gguf_models.manager.settings()
            preferred_model_id = user_sel.get("model_id") or None
            skill_profile = user_sel.get("profile") or None

        runtime = gguf_models.manager.ensure_loaded(model_id=preferred_model_id, profile=skill_profile)
        port = int(runtime["port"])
        loaded_model = runtime.get("loaded_model") or ""
        if preferred_model_id and loaded_model != preferred_model_id:
            raise RuntimeError("載入的模型與 Skill 指定的模型不符，請稍後重試")
        loaded_profile = skill_profile or gguf_models.profile_for(loaded_model) or gguf_models.manager.settings().get("profile") or "wai_illustrious"
        profile_spec = gguf_models.CATALOG["profiles"].get(loaded_profile) or {"steps": 20, "label": loaded_profile}

        if skill_steps and skill_profile == loaded_profile:
            steps = max(1, min(100, skill_steps))
        else:
            steps = int(profile_spec.get("steps", 20))

        if loaded_profile == "wai_illustrious" and skill_cfg <= 2.0:
            skill_cfg = 6.0
        elif loaded_profile == "qwen21" and skill_cfg <= 2.0:
            skill_cfg = 4.5

        model_name = Path(loaded_model).name if loaded_model else profile_spec.get("label", "GGUF")
        onnx_prov = self.seg_session.get_providers()[0] if self.seg_session else "ONNX"
        self.active_provider = f"{onnx_prov} + {model_name} (Vulkan)"

        return {
            "port": port,
            "loaded_model": loaded_model,
            "profile": loaded_profile,
            "prompt": skill_prompt,
            "steps": steps,
            "cfg_scale": max(1.0, min(10.0, skill_cfg)),
            "seed": skill_seed,
        }

    def _inpaint_window_gguf(
        self,
        crop_pil: Image.Image,
        mask_512: Image.Image,
        used_mode: str,
        runtime_cfg: Dict[str, Any],
        anatomy: Optional[str] = None,
    ) -> Image.Image:
        """透過 GGUF sd-server (/sdcpp/v1/img_gen) 對 512x512 視窗執行局部重繪"""
        cw, ch = crop_pil.size
        crop_512 = crop_pil.resize((512, 512), Image.Resampling.LANCZOS)
        profile = runtime_cfg.get("profile") or "wai_illustrious"
        is_sdxl = profile == "wai_illustrious"

        if used_mode == "bar":
            crop_arr = np.asarray(crop_512, dtype=np.float32)
            mask_arr = np.asarray(mask_512, dtype=np.float32) / 255.0
            ring_pil = mask_512.filter(ImageFilter.MaxFilter(25))
            ring_arr = (np.asarray(ring_pil, dtype=np.float32) / 255.0 > 0.2) & (mask_arr < 0.2)
            if np.any(ring_arr):
                mean_rgb = crop_arr[ring_arr].mean(axis=0)
            else:
                mean_rgb = np.array([235.0, 200.0, 190.0], dtype=np.float32)
            filled_arr = crop_arr * (1.0 - mask_arr[:, :, None]) + mean_rgb[None, None, :] * mask_arr[:, :, None]
            blurred_512 = Image.fromarray(np.clip(filled_arr, 0, 255).astype(np.uint8), mode="RGB").filter(
                ImageFilter.GaussianBlur(radius=6)
            )
            init_512 = Image.composite(blurred_512, crop_512, mask_512)
            strength = 0.72 if is_sdxl else 0.82
        else:
            # 消除馬賽克方格階梯邊緣 (Downscale 48x48 + Bicubic Upscale)，100% 保留原有立體光影與輪廓對比
            degrid_512 = crop_512.resize((48, 48), Image.Resampling.BILINEAR).resize(
                (512, 512), Image.Resampling.BICUBIC
            )
            init_512 = Image.composite(degrid_512, crop_512, mask_512)
            strength = 0.56 if is_sdxl else 0.68

        init_b64 = _pil_to_data_url(init_512)
        mask_b64 = _pil_to_data_url(mask_512)

        anat_tags = {
            YOLO_LABELS[1]: f"{YOLO_LABELS[1]}, erect, glans, foreskin, urethral slit, veins, detailed shaft",
            YOLO_LABELS[2]: f"{YOLO_LABELS[2]}, cleft of venus, labia, clitoris, detailed anatomy",
            YOLO_LABELS[0]: "breasts, areolae, nipples, pink nipples",
        }
        extra_anat = anat_tags.get(anatomy or "", "detailed anatomy, natural skin texture")

        if is_sdxl:
            prompt = f"masterpiece, best quality, amazing quality, very aesthetic, absurdres, newest, uncensored, {extra_anat}, crisp lineart, anime coloring, smooth shading"
            neg_prompt = "censored, mosaic censoring, bar censor, blurry, out of focus, bokeh, lowres, bad anatomy, worst quality, low quality, blocky, pixelated"
            sample_params = {
                "sample_steps": max(16, int(runtime_cfg.get("steps", 20))),
                "sample_method": "euler_a",
                "scheduler": "karras",
                "guidance": {
                    "txt_cfg": float(runtime_cfg.get("cfg_scale", 6.0)),
                },
            }
            ref_imgs = []
        else:
            prompt = f"{runtime_cfg['prompt']}, uncensored, {extra_anat}, crisp anime lineart, cel shading"
            neg_prompt = DEFAULT_NEGATIVE_PROMPT
            nominal_steps = max(8, int(runtime_cfg.get("steps", 8))) if profile == "klein4b" else max(18, int(runtime_cfg.get("steps", 20)))
            txt_cfg = 1.0 if profile == "klein4b" else max(4.0, float(runtime_cfg.get("cfg_scale", 4.5)))
            sample_params = {
                "sample_steps": nominal_steps,
                "guidance": {
                    "txt_cfg": txt_cfg,
                    "distilled_guidance": 3.5,
                },
            }
            ref_imgs = []

        payload = {
            "prompt": prompt,
            "negative_prompt": neg_prompt,
            "width": 512,
            "height": 512,
            "strength": strength,
            "seed": runtime_cfg["seed"],
            "init_image": init_b64,
            "mask_image": mask_b64,
            "ref_images": ref_imgs,
            "sample_params": sample_params,
            "output_format": "png",
        }

        port = runtime_cfg["port"]
        with requests.Session() as session:
            session.trust_env = False
            resp = session.post(f"http://127.0.0.1:{port}/sdcpp/v1/img_gen", json=payload, timeout=15)
            resp.raise_for_status()
            job_id = resp.json().get("id")
            if not job_id:
                raise RuntimeError("GGUF 模型伺服器未回傳工作 ID")

            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                poll = session.get(f"http://127.0.0.1:{port}/sdcpp/v1/jobs/{job_id}", timeout=10)
                poll.raise_for_status()
                job_data = poll.json()
                status = job_data.get("status")
                if status == "completed":
                    result_obj = job_data.get("result") or {}
                    images = result_obj.get("images") or job_data.get("images") or []
                    if not images or not images[0].get("b64_json"):
                        raise RuntimeError("GGUF 模型重繪完成但未回傳影像資料")
                    raw_bytes = base64.b64decode(images[0]["b64_json"])
                    with Image.open(io.BytesIO(raw_bytes)) as gen_img:
                        pred_512 = gen_img.convert("RGB")
                    if pred_512.size != (512, 512):
                        pred_512 = pred_512.resize((512, 512), Image.Resampling.LANCZOS)
                    comp_512 = Image.composite(pred_512, crop_512, mask_512)
                    return comp_512.resize((cw, ch), Image.Resampling.LANCZOS)
                if status in ("failed", "cancelled", "error"):
                    raise RuntimeError(job_data.get("error") or f"GGUF 局部重繪失敗 ({status})")
                time.sleep(0.18)

        raise RuntimeError("GGUF 模型局部重繪逾時")

    def unload_models(self) -> Dict[str, Any]:
        with self._lock:
            was_loaded = self.model_loaded
            self.seg_session = None
            self.yolo_session = None
            self.model_loaded = False
            self.active_provider = "已卸載 (已釋放 VRAM)"
        try:
            gguf_models.manager.unload()
        except Exception:
            pass
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
        3. 自動合併包含或高度重疊的冗余子框，避免同一區域重複推論。
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

        # 優先寫入高解析度 960x960 的精準多邊形遮罩；640x640 僅補充 960 未偵測到的獨立區域，避免粗解析度遮罩溢入指縫
        for det in seg_960:
            bx1, by1, bx2, by2 = det["bbox"]
            sm = det["sub_mask"]
            sc = det["score"]
            if det["label"] == "bar_censor":
                full_bar_mask[by1:by2, bx1:bx2] = np.maximum(full_bar_mask[by1:by2, bx1:bx2], sm)
            else:
                full_mos_mask[by1:by2, bx1:bx2] = np.maximum(full_mos_mask[by1:by2, bx1:bx2], sm)
            score_map[by1:by2, bx1:bx2] = np.maximum(score_map[by1:by2, bx1:bx2], (sm > 0.42).astype(np.float32) * sc)

        kept_seg_640 = []
        for det in seg_640:
            bx1, by1, bx2, by2 = det["bbox"]
            area_640 = float(max(1, (bx2 - bx1) * (by2 - by1)))
            overlaps_960 = False
            for d9 in seg_960:
                x9_1, y9_1, x9_2, y9_2 = d9["bbox"]
                area_960 = float(max(1, (x9_2 - x9_1) * (y9_2 - y9_1)))
                ix1, iy1 = max(bx1, x9_1), max(by1, y9_1)
                ix2, iy2 = min(bx2, x9_2), min(by2, y9_2)
                inter = float(max(0, ix2 - ix1) * max(0, iy2 - iy1))
                if inter / min(area_640, area_960) >= 0.35:
                    overlaps_960 = True
                    break
            if overlaps_960:
                continue
            kept_seg_640.append(det)
            sm = det["sub_mask"]
            sc = det["score"]
            if det["label"] == "bar_censor":
                full_bar_mask[by1:by2, bx1:bx2] = np.maximum(full_bar_mask[by1:by2, bx1:bx2], sm)
            else:
                full_mos_mask[by1:by2, bx1:bx2] = np.maximum(full_mos_mask[by1:by2, bx1:bx2], sm)
            score_map[by1:by2, bx1:bx2] = np.maximum(score_map[by1:by2, bx1:bx2], (sm > 0.42).astype(np.float32) * sc)

        # 輔助召回：僅在 YOLOv11-seg 完全未覆蓋的 YOLOv8 候選框內，檢查是否有漏掉的馬賽克或黑條
        union_seg = (full_mos_mask > 0.42) | (full_bar_mask > 0.42)
        v8_dets = self._run_yolov8_anatomy_fallback(img_rgb, conf_thresh=max(0.20, conf_thresh))
        v8_fallback_regions = []
        for vd in v8_dets:
            bx1, by1, bx2, by2 = vd["bbox"]
            existing_cov = float(union_seg[by1:by2, bx1:bx2].mean()) if (bx2 > bx1 and by2 > by1) else 0.0
            if existing_cov >= 0.10:
                continue

            roi_rgb = img_arr[by1:by2, bx1:bx2]
            bar_px = _detect_bar_pixels(roi_rgb)
            if float(bar_px.mean()) >= 0.14:
                full_bar_mask[by1:by2, bx1:bx2] = np.maximum(full_bar_mask[by1:by2, bx1:bx2], bar_px.astype(np.float32))
                score_map[by1:by2, bx1:bx2] = np.maximum(score_map[by1:by2, bx1:bx2], bar_px.astype(np.float32) * vd["score"])
                v8_fallback_regions.append(vd)
                continue

            mos_px = _detect_mosaic_grid_pixels(roi_rgb)
            if float(mos_px.mean()) >= 0.16 and vd["label"] in (YOLO_LABELS[1], YOLO_LABELS[2]):
                full_mos_mask[by1:by2, bx1:bx2] = np.maximum(full_mos_mask[by1:by2, bx1:bx2], mos_px.astype(np.float32))
                score_map[by1:by2, bx1:bx2] = np.maximum(score_map[by1:by2, bx1:bx2], mos_px.astype(np.float32) * vd["score"])
                v8_fallback_regions.append(vd)

        combined_prob = np.maximum(full_mos_mask, full_bar_mask)
        if not np.any(combined_prob > 0.32):
            return []

        # 收集所有獨立實例區域，保留各自的實例遮罩與緊密邊界，避免相鄰不同部位被錯誤合併
        all_candidates = []
        for det in seg_960 + kept_seg_640:
            all_candidates.append({
                "bbox": list(det["bbox"]),
                "score": float(det["score"]),
                "label": det["label"],
                "sub_mask": det.get("sub_mask"),
            })
        for vd in v8_fallback_regions:
            bx1, by1, bx2, by2 = vd["bbox"]
            if float((combined_prob[by1:by2, bx1:bx2] > 0.32).mean()) >= 0.12:
                all_candidates.append({
                    "bbox": [bx1, by1, bx2, by2],
                    "score": float(vd["score"]),
                    "label": vd["label"],
                    "sub_mask": combined_prob[by1:by2, bx1:bx2].copy(),
                })

        if not all_candidates:
            return []

        boxes_np = np.asarray([c["bbox"] for c in all_candidates], dtype=np.float32)
        scores_np = np.asarray([c["score"] for c in all_candidates], dtype=np.float32)
        keep_final = _nms_boxes(boxes_np, scores_np, iou_thresh=0.42)

        raw_regions = []
        for idx in keep_final:
            cand = all_candidates[idx]
            rx1, ry1, rx2, ry2 = [int(round(v)) for v in cand["bbox"]]
            if (rx2 - rx1) < 8 or (ry2 - ry1) < 8:
                continue
            raw_regions.append({
                "bbox": [rx1, ry1, rx2, ry2],
                "label": cand["label"],
                "score": round(float(cand["score"]), 4),
                "instance_mask": cand.get("sub_mask"),
            })

        # 僅合併高度重疊 (>= 0.65) 的同部位子框，絕不把相鄰的不同部位合併成巨型框
        merged_regions: List[Dict[str, Any]] = []
        for reg in raw_regions:
            rx1, ry1, rx2, ry2 = reg["bbox"]
            r_area = float(max(1, (rx2 - rx1) * (ry2 - ry1)))
            merged = False
            for existing in merged_regions:
                ex1, ey1, ex2, ey2 = existing["bbox"]
                e_area = float(max(1, (ex2 - ex1) * (ey2 - ey1)))
                ix1, iy1 = max(rx1, ex1), max(ry1, ey1)
                ix2, iy2 = min(rx2, ex2), min(ry2, ey2)
                inter = float(max(0, ix2 - ix1) * max(0, iy2 - iy1))
                if inter / min(r_area, e_area) >= 0.65:
                    existing["bbox"] = [min(rx1, ex1), min(ry1, ey1), max(rx2, ex2), max(ry2, ey2)]
                    existing["instance_mask"] = None
                    if reg["score"] > existing["score"]:
                        existing["score"] = reg["score"]
                        existing["label"] = reg["label"]
                    merged = True
                    break
            if not merged:
                merged_regions.append(dict(reg))

        final_regions = []
        for item in merged_regions:
            rx1, ry1, rx2, ry2 = item["bbox"]
            r_area = float(max(1, (rx2 - rx1) * (ry2 - ry1)))
            best_anat = None
            best_overlap = 0.0
            for vd in v8_dets:
                vx1, vy1, vx2, vy2 = vd["bbox"]
                v_area = float(max(1, (vx2 - vx1) * (vy2 - vy1)))
                ix1, iy1 = max(rx1, vx1), max(ry1, vy1)
                ix2, iy2 = min(rx2, vx2), min(ry2, vy2)
                inter = float(max(0, ix2 - ix1) * max(0, iy2 - iy1))
                ov = inter / min(r_area, v_area)
                if ov >= 0.25 and (ov * vd["score"]) > best_overlap:
                    best_anat = vd["label"]
                    best_overlap = ov * vd["score"]
            item["anatomy"] = best_anat
            inst_m = item.pop("instance_mask", None)
            if include_masks:
                if inst_m is not None and inst_m.shape == (ry2 - ry1, rx2 - rx1):
                    item["sub_mask"] = inst_m.copy()
                else:
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
        針對單一遮罩區域產生正方形 Context 重繪視窗。
        保持單一部位區域在同一個完整 512x512 視窗內一次重繪完成，避免將同一部位切成兩半造成結構斷裂。
        """
        bx1, by1, bx2, by2 = reg["bbox"]
        sbw = bx2 - bx1
        sbh = by2 - by1
        cx = (bx1 + bx2) / 2.0
        cy = (by1 + by2) / 2.0

        side = int(round(max(sbw, sbh) * 1.42))
        side = max(160, min(max(W, H), side))
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
            return [(wx1, wy1, wx2, wy2, bx1, by1, bx2, by2)]
        return []

    def decensor_pil_image(
        self,
        img_rgb: Image.Image,
        mode: str = "auto",
        conf_thresh: float = 0.18,
        custom_boxes: Optional[List[List[float]]] = None,
    ) -> Tuple[Image.Image, List[Dict[str, Any]]]:
        """
        對單張 PIL Image (RGB) 執行 YOLOv11-seg 多尺度像素級遮罩偵測 (或自訂框選) + 本機 GGUF 模型局部重繪去碼。
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

        runtime_cfg = self._resolve_decensor_skill_and_runtime()

        # 建立全圖級別的像素遮罩以便任意子視窗直接裁切對應的 512x512 遮罩
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
                crop_pil = Image.fromarray(np.clip(work_arr[wy1:wy2, wx1:wx2], 0, 255).astype(np.uint8), mode="RGB")
                crop_512_pil = crop_pil.resize((512, 512), Image.Resampling.BICUBIC)
                crop_512_arr = np.asarray(crop_512_pil, dtype=np.float32)

                ix1 = int(round((sbx1 - wx1) * 512.0 / cw))
                iy1 = int(round((sby1 - wy1) * 512.0 / ch))
                ix2 = int(round((sbx2 - wx1) * 512.0 / cw))
                iy2 = int(round((sby2 - wy1) * 512.0 / ch))

                seg_crop_512 = None
                if has_global_seg:
                    win_seg = np.zeros((ch, cw), dtype=np.float32)
                    lx1, ly1 = max(0, sbx1 - wx1 - 8), max(0, sby1 - wy1 - 8)
                    lx2, ly2 = min(cw, sbx2 - wx1 + 8), min(ch, sby2 - wy1 + 8)
                    win_seg[ly1:ly2, lx1:lx2] = global_seg_mask[wy1 + ly1 : wy1 + ly2, wx1 + lx1 : wx1 + lx2]
                    seg_pil_512 = Image.fromarray((np.clip(win_seg, 0.0, 1.0) * 255.0).astype(np.uint8), mode="L").resize(
                        (512, 512), Image.Resampling.BILINEAR
                    )
                    seg_crop_512 = np.asarray(seg_pil_512, dtype=np.float32) / 255.0

                mask_512_pil, used_mode = _build_region_mask_512(
                    crop_512_arr,
                    (ix1, iy1, ix2, iy2),
                    mode=reg_mode,
                    seg_mask_512=seg_crop_512,
                )
                used_mode_final = used_mode

                pred_crop_pil = self._inpaint_window_gguf(
                    crop_pil,
                    mask_512_pil,
                    used_mode,
                    runtime_cfg,
                    anatomy=reg.get("anatomy"),
                )
                pred_crop_arr = np.asarray(pred_crop_pil, dtype=np.float32)

                # 建立羽化融合遮罩 (Feather Alpha Blend)，僅融合多邊形遮罩內部，100% 不影響框外的五官、手指與背景
                alpha_pil = (
                    mask_512_pil.resize((cw, ch), Image.Resampling.BILINEAR)
                    .filter(ImageFilter.GaussianBlur(radius=max(1.5, min(cw, ch) * 0.012)))
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

        try:
            with Image.open(str(full_path)) as img:
                img_rgb = img.convert("RGB")
                W, H = img_rgb.size

            out_pil, regions = self.decensor_pil_image(
                img_rgb,
                mode=mode,
                conf_thresh=conf_thresh,
                custom_boxes=custom_boxes,
            )
        except Exception as exc:
            msg = str(exc) or "GGUF 模型局部重繪失敗"
            return {"ok": False, "success": False, "image_id": image_id, "message": msg, "error": msg}

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
            "message": f"✨ 已在 {elapsed} 秒內透過 {self.active_provider} (YOLOv11-seg 像素級遮罩 + GGUF 局部重繪) 修復 {len(regions)} 處馬賽克區域！",
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

        try:
            with Image.open(str(full_path)) as img:
                img_rgb = img.convert("RGB")

            out_pil, regions = self.decensor_pil_image(
                img_rgb,
                mode=mode,
                conf_thresh=conf_thresh,
                custom_boxes=custom_boxes,
            )
        except Exception as exc:
            msg = str(exc) or "GGUF 模型局部重繪失敗"
            return {"ok": False, "success": False, "image_id": image_id, "message": msg, "error": msg}

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
                        SELECT ?, it.tag_id, it.confidence, it.source
                        FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE it.image_id = ?
                        AND (it.source='manual' OR t.category IN ('artist','character','copyright','source'))
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

        # Record processing as a source marker, without generating content tags.
        conn = get_db()
        try:
            with conn:
                tid_dec = ensure_tag(conn, {}, "AI去馬賽克", "source", source="decensor")
                conn.execute(
                    "INSERT OR IGNORE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, 1.0, 'decensor')",
                    (target_id, tid_dec),
                )
                conn.execute(
                    """
                    DELETE FROM image_tags
                    WHERE image_id = ? AND source != 'manual'
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
            "message": f"✅ 已成功透過 YOLOv11-seg + GGUF 模型去除 {len(regions)} 處馬賽克/遮罩並{'覆蓋原圖' if save_mode == 'overwrite' else '另存新檔'}！",
        }


decensor_engine = AnimeDecensorEngine()
