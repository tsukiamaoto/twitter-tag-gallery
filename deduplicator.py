# -*- coding: utf-8 -*-
"""Exact and perceptual/visual duplicate image detection and safe cleanup."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import filecmp
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
from PIL import Image, ImageFile

import database
from background_work import exclusive_job, update_job
from file_safety import gallery_path
from tag_policy import AUTOMATIC_CATEGORIES, allows_tag

ImageFile.LOAD_TRUNCATED_IMAGES = True


def _visual_hash_db_path() -> Path:
    return Path(database.DATA_DIR) / "visual_hashes.db"


def _open_visual_hash_db() -> sqlite3.Connection:
    db_path = _visual_hash_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS visual_hashes (
            id INTEGER PRIMARY KEY,
            rel_path TEXT,
            folder TEXT,
            filename TEXT,
            ext TEXT,
            size_bytes INTEGER,
            width INTEGER,
            height INTEGER,
            dhash TEXT,
            phash TEXT,
            updated_at REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS visual_pair_cache (
            id1 INTEGER,
            id2 INTEGER,
            size1 INTEGER,
            size2 INTEGER,
            mae REAL,
            ssim REAL,
            significant_frac REAL,
            ar_diff REAL,
            PRIMARY KEY (id1, id2)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vh_folder ON visual_hashes(folder)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vh_dhash ON visual_hashes(dhash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vh_phash ON visual_hashes(phash)")
    return conn


def _is_animated_image(img: Image.Image) -> bool:
    try:
        return bool(getattr(img, "is_animated", False) or int(getattr(img, "n_frames", 1)) > 1)
    except Exception:
        return False


def _compute_single_visual_hash(full_path: Path) -> Optional[Tuple[int, int, str, str]]:
    import imagehash

    if full_path.suffix.lower() in (".gif", ".mp4"):
        return None
    try:
        with Image.open(full_path) as img:
            if _is_animated_image(img):
                return None
            w, h = img.size
            try:
                img.draft("RGB", (128, 128))
            except Exception:
                pass
            rgb = img.convert("RGB")
            dhash_str = str(imagehash.dhash(rgb, hash_size=8))
            phash_str = str(imagehash.phash(rgb, hash_size=8))
            return w, h, dhash_str, phash_str
    except Exception:
        return None


def ensure_visual_hashes(rows: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
    """Ensure dHash, pHash, width, and height are cached for all non-video rows."""
    vconn = _open_visual_hash_db()
    try:
        cached_rows = vconn.execute(
            "SELECT id, rel_path, size_bytes, width, height, dhash, phash FROM visual_hashes"
        ).fetchall()
        cached_map: Dict[int, Dict[str, Any]] = {}
        for r in cached_rows:
            if r["dhash"] and r["phash"]:
                cached_map[r["id"]] = dict(r)

        result: Dict[int, Dict[str, Any]] = {}
        to_compute: List[Dict[str, Any]] = []

        for r in rows:
            ext = str(r.get("ext") or "").lower()
            if ext in (".mp4", ".gif") or r.get("media_type") in ("video", "gif"):
                continue
            iid = r["id"]
            c = cached_map.get(iid)
            if (
                c
                and c.get("size_bytes") == r["size_bytes"]
                and c.get("width", 0) > 0
                and c.get("height", 0) > 0
            ):
                result[iid] = {
                    "width": int(c["width"]),
                    "height": int(c["height"]),
                    "dhash": str(c["dhash"]),
                    "phash": str(c["phash"]),
                    "d_int": int(c["dhash"], 16),
                    "p_int": int(c["phash"], 16),
                }
            else:
                to_compute.append(r)

        if to_compute:
            batch_inserts = []
            now = time.time()
            total_new = len(to_compute)

            def _worker(r: Dict[str, Any]):
                try:
                    full_path = gallery_path(database.GALLERY_ROOT, r["rel_path"])
                    if not full_path.is_file():
                        return None
                    computed = _compute_single_visual_hash(full_path)
                    if not computed:
                        return None
                    return (r, computed)
                except Exception:
                    return None

            with ThreadPoolExecutor(max_workers=8) as pool:
                for idx, item in enumerate(pool.map(_worker, to_compute)):
                    if idx % 50 == 0:
                        update_job(
                            stage=f"計算視覺特徵雜湊 ({idx}/{total_new})",
                            processed_count=idx,
                            total_target=total_new,
                        )
                    if not item:
                        continue
                    r, (w, h, dhash_str, phash_str) = item
                    iid = r["id"]
                    result[iid] = {
                        "width": w,
                        "height": h,
                        "dhash": dhash_str,
                        "phash": phash_str,
                        "d_int": int(dhash_str, 16),
                        "p_int": int(phash_str, 16),
                    }
                    batch_inserts.append(
                        (
                            iid,
                            r["rel_path"],
                            r.get("folder") or "",
                            r.get("filename") or "",
                            r.get("ext") or "",
                            int(r.get("size_bytes") or 0),
                            w,
                            h,
                            dhash_str,
                            phash_str,
                            now,
                        )
                    )
                    if len(batch_inserts) >= 250:
                        with vconn:
                            vconn.executemany(
                                """
                                INSERT OR REPLACE INTO visual_hashes
                                (id, rel_path, folder, filename, ext, size_bytes, width, height, dhash, phash, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                batch_inserts,
                            )
                        batch_inserts.clear()

            if batch_inserts:
                with vconn:
                    vconn.executemany(
                        """
                        INSERT OR REPLACE INTO visual_hashes
                        (id, rel_path, folder, filename, ext, size_bytes, width, height, dhash, phash, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        batch_inserts,
                    )
        return result
    finally:
        vconn.close()


def _load_512_array(path: Path) -> Optional[Tuple[int, int, np.ndarray]]:
    if path.suffix.lower() in (".gif", ".mp4"):
        return None
    try:
        with Image.open(path) as img:
            if _is_animated_image(img):
                return None
            w, h = img.size
            if w <= 0 or h <= 0:
                return None
            try:
                img.draft("RGB", (512, 512))
            except Exception:
                pass
            rgb = img.convert("RGB").resize((512, 512), Image.Resampling.BICUBIC)
            return w, h, np.asarray(rgb, dtype=np.float32)
    except Exception:
        return None


def _compute_metrics_from_arrays(
    arr1: np.ndarray,
    arr2: np.ndarray,
    w1: int,
    h1: int,
    w2: int,
    h2: int,
) -> Dict[str, float]:
    ar1 = w1 / h1
    ar2 = w2 / h2
    ar_diff = abs(ar1 - ar2) / max(ar1, ar2)

    diff = np.abs(arr1 - arr2)
    mae = float(np.mean(diff))
    max_ch_diff = np.max(diff, axis=2)
    significant_frac = float(np.mean(max_ch_diff > 45.0))

    mu1 = float(np.mean(arr1))
    mu2 = float(np.mean(arr2))
    sigma1_sq = float(np.var(arr1))
    sigma2_sq = float(np.var(arr2))
    sigma12 = float(np.mean((arr1 - mu1) * (arr2 - mu2)))
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    ssim = ((2 * mu1 * mu2 + c1) * (2 * sigma12 + c2)) / (
        (mu1 ** 2 + mu2 ** 2 + c1) * (sigma1_sq + sigma2_sq + c2)
    )
    return {
        "mae": round(mae, 3),
        "ssim": round(float(ssim), 4),
        "significant_frac": round(significant_frac, 5),
        "ar_diff": round(ar_diff, 4),
    }


def compare_visual_identity(
    path1: Path,
    path2: Path,
    w1: int = 0,
    h1: int = 0,
    w2: int = 0,
    h2: int = 0,
    max_mae: float = 6.5,
    min_ssim: float = 0.95,
    max_diff_frac: float = 0.005,
    max_ar_diff: float = 0.025,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Compare two images at 512x512 RGB to determine if they are true visual duplicates
    (resize, recompression, format conversion) while strictly preserving artist variations
    (differentials / 差分圖 such as expression, clothing, text, or watermark changes).
    """
    try:
        with Image.open(path1) as im1, Image.open(path2) as im2:
            if _is_animated_image(im1) or _is_animated_image(im2):
                return False, {"reason": "animated_media"}
    except Exception as exc:
        return False, {"reason": f"error: {exc}"}

    loaded1 = _load_512_array(path1)
    loaded2 = _load_512_array(path2)
    if not loaded1 or not loaded2:
        return False, {"reason": "load_error"}
    lw1, lh1, arr1 = loaded1
    lw2, lh2, arr2 = loaded2
    w1 = w1 or lw1
    h1 = h1 or lh1
    w2 = w2 or lw2
    h2 = h2 or lh2
    ar1 = w1 / h1
    ar2 = w2 / h2
    ar_diff = abs(ar1 - ar2) / max(ar1, ar2)
    if ar_diff > max_ar_diff:
        return False, {"reason": "aspect_ratio_mismatch", "ar_diff": round(ar_diff, 4)}

    metrics = _compute_metrics_from_arrays(arr1, arr2, w1, h1, w2, h2)
    is_dup = (
        metrics["mae"] <= max_mae
        and metrics["ssim"] >= min_ssim
        and metrics["significant_frac"] <= max_diff_frac
    )
    return is_dup, metrics


def _verify_candidate_pairs_cached(
    candidate_pairs: List[Tuple[int, int, int, int]],
    by_id: Dict[int, Dict[str, Any]],
) -> Dict[Tuple[int, int], Dict[str, float]]:
    """
    Return {(min_id, max_id): metrics} for all candidate pairs, reading from and updating
    the persistent visual_pair_cache table in visual_hashes.db.
    """
    if not candidate_pairs:
        return {}

    vconn = _open_visual_hash_db()
    try:
        cached_rows = vconn.execute(
            "SELECT id1, id2, size1, size2, mae, ssim, significant_frac, ar_diff FROM visual_pair_cache"
        ).fetchall()
        pair_map: Dict[Tuple[int, int], Dict[str, float]] = {}
        for r in cached_rows:
            k = (int(r["id1"]), int(r["id2"]))
            r1 = by_id.get(k[0])
            r2 = by_id.get(k[1])
            if (
                r1
                and r2
                and int(r1.get("size_bytes") or 0) == int(r["size1"])
                and int(r2.get("size_bytes") or 0) == int(r["size2"])
            ):
                pair_map[k] = {
                    "mae": float(r["mae"]),
                    "ssim": float(r["ssim"]),
                    "significant_frac": float(r["significant_frac"]),
                    "ar_diff": float(r["ar_diff"]),
                }

        uncached_pairs: List[Tuple[int, int]] = []
        needed_ids: Set[int] = set()
        for id1, id2, _, _ in candidate_pairs:
            k = (id1, id2) if id1 < id2 else (id2, id1)
            if k not in pair_map:
                uncached_pairs.append(k)
                needed_ids.add(k[0])
                needed_ids.add(k[1])

        if uncached_pairs:
            # Group uncached pairs by bucket/chunk so memory stays bounded while decoding each image at most once per chunk
            new_cache_rows = []
            total_uncached = len(uncached_pairs)
            chunk_size = 400
            for start in range(0, total_uncached, chunk_size):
                sub_pairs = uncached_pairs[start : start + chunk_size]
                update_job(
                    stage=f"像素級結構比對快取建立 ({start}/{total_uncached})",
                    processed_count=start,
                    total_target=total_uncached,
                )
                sub_ids = sorted({iid for p in sub_pairs for iid in p})

                def _load_id(iid: int):
                    row = by_id.get(iid)
                    if not row:
                        return (iid, None)
                    try:
                        p = gallery_path(database.GALLERY_ROOT, row["rel_path"])
                        if not p.is_file():
                            return (iid, None)
                        return (iid, _load_512_array(p))
                    except Exception:
                        return (iid, None)

                loaded_arrays: Dict[int, Tuple[int, int, np.ndarray]] = {}
                with ThreadPoolExecutor(max_workers=8) as pool:
                    for iid, data in pool.map(_load_id, sub_ids):
                        if data is not None:
                            loaded_arrays[iid] = data

                for id1, id2 in sub_pairs:
                    d1 = loaded_arrays.get(id1)
                    d2 = loaded_arrays.get(id2)
                    if not d1 or not d2:
                        continue
                    r1 = by_id[id1]
                    r2 = by_id[id2]
                    w1 = r1.get("width") or d1[0]
                    h1 = r1.get("height") or d1[1]
                    w2 = r2.get("width") or d2[0]
                    h2 = r2.get("height") or d2[1]
                    metrics = _compute_metrics_from_arrays(d1[2], d2[2], w1, h1, w2, h2)
                    pair_map[(id1, id2)] = metrics
                    new_cache_rows.append(
                        (
                            id1,
                            id2,
                            int(r1.get("size_bytes") or 0),
                            int(r2.get("size_bytes") or 0),
                            metrics["mae"],
                            metrics["ssim"],
                            metrics["significant_frac"],
                            metrics["ar_diff"],
                        )
                    )

                if new_cache_rows:
                    with vconn:
                        vconn.executemany(
                            """
                            INSERT OR REPLACE INTO visual_pair_cache
                            (id1, id2, size1, size2, mae, ssim, significant_frac, ar_diff)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            new_cache_rows,
                        )
                    new_cache_rows.clear()

        return pair_map
    finally:
        vconn.close()


def _quality_rank_key(item: Dict[str, Any]) -> Tuple[int, int, int, int, int, int]:
    """
    Higher tuple value = higher priority to KEEP:
    1. Resolution (width * height)
    2. Lossless PNG format (.png > others)
    3. File size in bytes
    4. Organized artist folder (prefer keeping organized copy over unorganized root copy on tie)
    5. Clean filename (prefer original over '(1)' or 'copy')
    6. Smaller ID (-id)
    """
    w = int(item.get("width") or 0)
    h = int(item.get("height") or 0)
    pixels = w * h
    ext = str(item.get("ext") or "").lower()
    is_png = 1 if ext == ".png" else 0
    size_bytes = int(item.get("size_bytes") or 0)
    folder = str(item.get("folder") or "")
    in_artist_folder = 1 if (folder and folder not in database.NON_ARTIST_FOLDERS) else 0
    fname = str(item.get("filename") or "")
    clean_name = 0 if ("(" in fname or "copy" in fname.lower()) else 1
    return (pixels, is_png, size_bytes, in_artist_folder, clean_name, -int(item["id"]))


def _format_image_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    w = int(item.get("width") or 0)
    h = int(item.get("height") or 0)
    return {
        "id": int(item["id"]),
        "filename": str(item.get("filename") or ""),
        "rel_path": str(item.get("rel_path") or ""),
        "folder": str(item.get("folder") or ""),
        "ext": str(item.get("ext") or ""),
        "size_bytes": int(item.get("size_bytes") or 0),
        "width": w,
        "height": h,
        "pixels": w * h,
        "media_type": str(item.get("media_type") or "image"),
        "content_hash": str(item.get("content_hash") or ""),
    }


def _connected_components(nodes: Set[int], adj: Dict[int, Set[int]]) -> List[List[int]]:
    visited: Set[int] = set()
    components: List[List[int]] = []
    for start in sorted(nodes):
        if start in visited:
            continue
        comp: List[int] = []
        stack = [start]
        visited.add(start)
        while stack:
            curr = stack.pop()
            comp.append(curr)
            for nxt in adj.get(curr, ()):
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append(nxt)
        if len(comp) > 1:
            components.append(comp)
    return components


def scan_duplicates(
    mode: str = "both",
    scope: str = "same_folder",
    folder: str = "__ALL__",
    max_mae: float = 6.5,
    min_ssim: float = 0.95,
    max_diff_frac: float = 0.005,
    max_groups: Optional[int] = 300,
) -> Dict[str, Any]:
    """
    Scan the gallery for exact duplicates (SHA-256 + byte-for-byte verification)
    and/or visually similar duplicates (dHash/pHash + 512x512 SSIM/MAE verification).
    """
    if mode not in ("both", "exact", "visual"):
        raise ValueError("mode 必須為 'both'、'exact' 或 'visual'")
    if scope not in ("same_folder", "all"):
        raise ValueError("scope 必須為 'same_folder' 或 'all'")

    conn = database.get_db()
    try:
        sql = """
            SELECT id, filename, rel_path, folder, ext, size_bytes, mtime, media_type, content_hash
            FROM images
            WHERE missing_since IS NULL
        """
        params: List[Any] = []
        if folder == "__ROOT__":
            sql += " AND folder = ''"
        elif folder and folder not in ("__ALL__", "__FOLDERS__"):
            sql += " AND folder = ?"
            params.append(folder)
        elif folder == "__FOLDERS__":
            sql += " AND folder != ''"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()

    total_scanned = len(rows)
    if not rows:
        return {
            "mode": mode,
            "scope": scope,
            "folder": folder,
            "total_scanned": 0,
            "total_groups": 0,
            "exact_groups_count": 0,
            "visual_groups_count": 0,
            "total_duplicate_images": 0,
            "exact_duplicate_images": 0,
            "visual_duplicate_images": 0,
            "total_reclaim_bytes": 0,
            "total_reclaim_mb": 0.0,
            "all_delete_ids": [],
            "groups": [],
        }

    update_job(stage="準備視覺特徵與尺寸索引", processed_count=0, total_target=total_scanned)
    vh_map = ensure_visual_hashes(rows)
    by_id: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        iid = r["id"]
        vh = vh_map.get(iid)
        if vh:
            r["width"] = vh["width"]
            r["height"] = vh["height"]
            r["dhash"] = vh["dhash"]
            r["phash"] = vh["phash"]
            r["d_int"] = vh["d_int"]
            r["p_int"] = vh["p_int"]
        else:
            r["width"] = 0
            r["height"] = 0
        by_id[iid] = r

    groups: List[Dict[str, Any]] = []
    exact_deleted_ids: Set[int] = set()
    exact_groups_count = 0
    visual_groups_count = 0
    exact_duplicate_images = 0
    visual_duplicate_images = 0

    # --- Phase 1: Exact Duplicate Scan (content_hash + byte-for-byte filecmp) ---
    if mode in ("both", "exact"):
        update_job(stage="比對 SHA-256 與位元組完全重複檔案", processed_count=0, total_target=total_scanned)
        hash_buckets: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            chash = str(r.get("content_hash") or "").strip()
            if not chash:
                continue
            bucket_folder = str(r.get("folder") or "") if scope == "same_folder" else "__ALL__"
            hash_buckets[(bucket_folder, chash)].append(r)

        for (_, chash), bucket in hash_buckets.items():
            if len(bucket) < 2:
                continue
            ranked = sorted(bucket, key=_quality_rank_key, reverse=True)
            used_in_bucket: Set[int] = set()
            for i, candidate_keep in enumerate(ranked):
                if candidate_keep["id"] in used_in_bucket:
                    continue
                try:
                    keep_path = gallery_path(database.GALLERY_ROOT, candidate_keep["rel_path"])
                except Exception:
                    continue
                if not keep_path.is_file():
                    continue

                verified_dups: List[Dict[str, Any]] = []
                for other in ranked[i + 1 :]:
                    if other["id"] in used_in_bucket:
                        continue
                    try:
                        other_path = gallery_path(database.GALLERY_ROOT, other["rel_path"])
                        if not other_path.is_file():
                            continue
                        st1 = keep_path.stat()
                        st2 = other_path.stat()
                        if st1.st_size != st2.st_size:
                            is_identical = False
                        elif st1.st_size <= 8 * 1024 * 1024:
                            is_identical = filecmp.cmp(str(keep_path), str(other_path), shallow=False)
                        else:
                            with keep_path.open("rb") as f1, other_path.open("rb") as f2:
                                head_match = f1.read(65536) == f2.read(65536)
                                if head_match:
                                    f1.seek(max(0, st1.st_size - 65536))
                                    f2.seek(max(0, st2.st_size - 65536))
                                    is_identical = f1.read(65536) == f2.read(65536)
                                else:
                                    is_identical = False
                    except Exception:
                        is_identical = False
                    if is_identical:
                        used_in_bucket.add(other["id"])
                        exact_deleted_ids.add(other["id"])
                        dup_summary = _format_image_summary(other)
                        dup_summary["reason"] = "完全重複 (SHA-256 與位元組 100% 相同)"
                        dup_summary["metrics"] = {"mae": 0.0, "ssim": 1.0, "significant_frac": 0.0, "d_dist": 0, "p_dist": 0}
                        verified_dups.append(dup_summary)

                if verified_dups:
                    used_in_bucket.add(candidate_keep["id"])
                    exact_groups_count += 1
                    exact_duplicate_images += len(verified_dups)
                    reclaim = sum(d["size_bytes"] for d in verified_dups)
                    folders_involved = {candidate_keep.get("folder") or ""} | {d.get("folder") or "" for d in verified_dups}
                    group_folder = next(iter(folders_involved)) if len(folders_involved) == 1 else "跨資料夾"
                    groups.append(
                        {
                            "group_id": len(groups) + 1,
                            "match_type": "exact",
                            "folder": group_folder,
                            "keep": _format_image_summary(candidate_keep),
                            "duplicates": verified_dups,
                            "reclaim_bytes": reclaim,
                        }
                    )

    # --- Phase 2: Visual Similarity / Recompression Scan (dHash + pHash + 512x512 SSIM/MAE) ---
    if mode in ("both", "visual"):
        update_job(stage="比對視覺感知雜湊與像素結構相似度 (SSIM/MAE)", processed_count=0, total_target=total_scanned)
        visual_buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            iid = r["id"]
            if iid in exact_deleted_ids:
                continue
            if "d_int" not in r or "p_int" not in r:
                continue
            if r.get("width", 0) <= 0 or r.get("height", 0) <= 0:
                continue
            bucket_key = str(r.get("folder") or "") if scope == "same_folder" else "__ALL__"
            visual_buckets[bucket_key].append(r)

        candidate_pairs: List[Tuple[int, int, int, int]] = []
        for bucket_key, items in visual_buckets.items():
            n = len(items)
            if n < 2:
                continue
            if n > 600:
                block_index: Dict[Tuple[int, int], List[int]] = defaultdict(list)
                for idx, it in enumerate(items):
                    d_val = it["d_int"]
                    for blk in range(4):
                        part = (d_val >> (blk * 16)) & 0xFFFF
                        block_index[(blk, part)].append(idx)
                seen_idx_pairs: Set[Tuple[int, int]] = set()
                for indices in block_index.values():
                    if len(indices) < 2:
                        continue
                    for a_i in range(len(indices)):
                        i = indices[a_i]
                        it1 = items[i]
                        ar1 = it1["width"] / it1["height"]
                        for b_i in range(a_i + 1, len(indices)):
                            j = indices[b_i]
                            pair_key = (i, j) if i < j else (j, i)
                            if pair_key in seen_idx_pairs:
                                continue
                            seen_idx_pairs.add(pair_key)
                            it2 = items[j]
                            if (
                                scope == "all"
                                and it1.get("folder") != it2.get("folder")
                                and (
                                    it1.get("folder") in database.NON_ARTIST_FOLDERS
                                    or it2.get("folder") in database.NON_ARTIST_FOLDERS
                                )
                            ):
                                continue
                            ar2 = it2["width"] / it2["height"]
                            if abs(ar1 - ar2) / max(ar1, ar2) > 0.025:
                                continue
                            d_dist = (it1["d_int"] ^ it2["d_int"]).bit_count()
                            if d_dist > 4:
                                continue
                            p_dist = (it1["p_int"] ^ it2["p_int"]).bit_count()
                            if (d_dist <= 2 and p_dist <= 4) or (d_dist <= 4 and p_dist <= 2):
                                candidate_pairs.append((it1["id"], it2["id"], d_dist, p_dist))
            else:
                for i in range(n):
                    it1 = items[i]
                    ar1 = it1["width"] / it1["height"]
                    for j in range(i + 1, n):
                        it2 = items[j]
                        ar2 = it2["width"] / it2["height"]
                        if abs(ar1 - ar2) / max(ar1, ar2) > 0.025:
                            continue
                        d_dist = (it1["d_int"] ^ it2["d_int"]).bit_count()
                        if d_dist > 4:
                            continue
                        p_dist = (it1["p_int"] ^ it2["p_int"]).bit_count()
                        if (d_dist <= 2 and p_dist <= 4) or (d_dist <= 4 and p_dist <= 2):
                            candidate_pairs.append((it1["id"], it2["id"], d_dist, p_dist))

        pair_metrics_map = _verify_candidate_pairs_cached(candidate_pairs, by_id)

        adj: Dict[int, Set[int]] = defaultdict(set)
        nodes: Set[int] = set()
        pair_dist_map: Dict[Tuple[int, int], Tuple[int, int]] = {}

        for id1, id2, d_dist, p_dist in candidate_pairs:
            k = (id1, id2) if id1 < id2 else (id2, id1)
            metrics = pair_metrics_map.get(k)
            if not metrics:
                continue
            if (
                metrics["ar_diff"] <= 0.025
                and metrics["mae"] <= max_mae
                and metrics["ssim"] >= min_ssim
                and metrics["significant_frac"] <= max_diff_frac
            ):
                nodes.add(id1)
                nodes.add(id2)
                adj[id1].add(id2)
                adj[id2].add(id1)
                pair_dist_map[k] = (d_dist, p_dist)

        components = _connected_components(nodes, adj)
        for comp in components:
            comp_items = [by_id[iid] for iid in comp if iid in by_id]
            if len(comp_items) < 2:
                continue
            ranked = sorted(comp_items, key=_quality_rank_key, reverse=True)
            keep_item = ranked[0]
            try:
                keep_path = gallery_path(database.GALLERY_ROOT, keep_item["rel_path"])
            except Exception:
                continue
            if not keep_path.is_file():
                continue

            verified_dups = []
            for other in ranked[1:]:
                k = (min(keep_item["id"], other["id"]), max(keep_item["id"], other["id"]))
                metrics = pair_metrics_map.get(k)
                if metrics is None:
                    try:
                        other_path = gallery_path(database.GALLERY_ROOT, other["rel_path"])
                    except Exception:
                        continue
                    if not other_path.is_file():
                        continue
                    is_dup, metrics = compare_visual_identity(
                        keep_path,
                        other_path,
                        w1=keep_item["width"],
                        h1=keep_item["height"],
                        w2=other["width"],
                        h2=other["height"],
                        max_mae=max_mae,
                        min_ssim=min_ssim,
                        max_diff_frac=max_diff_frac,
                    )
                    if not is_dup:
                        continue
                else:
                    if not (
                        metrics["ar_diff"] <= 0.025
                        and metrics["mae"] <= max_mae
                        and metrics["ssim"] >= min_ssim
                        and metrics["significant_frac"] <= max_diff_frac
                    ):
                        continue
                metrics = dict(metrics)
                d_dist, p_dist = pair_dist_map.get(k, (0, 0))
                metrics["d_dist"] = d_dist
                metrics["p_dist"] = p_dist
                dup_summary = _format_image_summary(other)
                dup_summary["reason"] = (
                    f"視覺相同/重壓縮或縮圖 (SSIM {metrics['ssim']:.3f}, MAE {metrics['mae']:.2f})"
                )
                dup_summary["metrics"] = metrics
                verified_dups.append(dup_summary)

            if verified_dups:
                visual_groups_count += 1
                visual_duplicate_images += len(verified_dups)
                reclaim = sum(d["size_bytes"] for d in verified_dups)
                folders_involved = {keep_item.get("folder") or ""} | {d.get("folder") or "" for d in verified_dups}
                group_folder = next(iter(folders_involved)) if len(folders_involved) == 1 else "跨資料夾"
                groups.append(
                    {
                        "group_id": len(groups) + 1,
                        "match_type": "visual",
                        "folder": group_folder,
                        "keep": _format_image_summary(keep_item),
                        "duplicates": verified_dups,
                        "reclaim_bytes": reclaim,
                    }
                )

    all_delete_ids = [d["id"] for g in groups for d in g["duplicates"]]
    total_reclaim_bytes = sum(g["reclaim_bytes"] for g in groups)
    shown_groups = groups if max_groups is None else groups[:max_groups]

    return {
        "mode": mode,
        "scope": scope,
        "folder": folder,
        "total_scanned": total_scanned,
        "total_groups": len(groups),
        "exact_groups_count": exact_groups_count,
        "visual_groups_count": visual_groups_count,
        "total_duplicate_images": len(all_delete_ids),
        "exact_duplicate_images": exact_duplicate_images,
        "visual_duplicate_images": visual_duplicate_images,
        "total_reclaim_bytes": total_reclaim_bytes,
        "total_reclaim_mb": round(total_reclaim_bytes / (1024 * 1024), 2),
        "all_delete_ids": all_delete_ids,
        "groups": shown_groups,
        "groups_truncated": False if max_groups is None else len(groups) > max_groups,
    }


@exclusive_job("清除重複與相似圖片")
def execute_dedup_groups(
    groups: List[Dict[str, Any]],
    selected_delete_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """
    Safely execute duplicate cleanup for the given groups:
    1. Create a verified SQLite backup via backup_service.backup().
    2. Merge core tags (artist, character, copyright, source) and manual tags from duplicates onto kept images.
    3. Move duplicate files to the Windows Recycle Bin via gallery_deletion.delete_images().
    4. Clean up visual_hashes.db and write an operation log entry.
    """
    import backup_service
    import gallery_deletion

    allowed_ids: Optional[Set[int]] = set(selected_delete_ids) if selected_delete_ids is not None else None
    keep_to_dups: List[Tuple[int, List[int]]] = []
    delete_ids: List[int] = []
    reclaim_bytes = 0

    for g in groups:
        keep_id = int(g["keep"]["id"])
        dups = [
            int(d["id"])
            for d in g.get("duplicates", [])
            if int(d["id"]) != keep_id and (allowed_ids is None or int(d["id"]) in allowed_ids)
        ]
        if dups:
            keep_to_dups.append((keep_id, dups))
            delete_ids.extend(dups)
            for d in g.get("duplicates", []):
                if int(d["id"]) in dups:
                    reclaim_bytes += int(d.get("size_bytes") or 0)

    delete_ids = list(dict.fromkeys(delete_ids))
    if not delete_ids:
        return {
            "deleted_count": 0,
            "recycled_count": 0,
            "merged_tag_links": 0,
            "reclaimed_bytes": 0,
            "reclaimed_mb": 0.0,
            "deleted_ids": [],
            "message": "沒有需要清除的重複圖片",
        }

    update_job(stage="建立資料庫安全備份", processed_count=0, total_target=len(delete_ids))
    backup_info = backup_service.backup()

    # Merge core & manual tags from duplicates onto kept image before deleting
    update_job(stage="合併保留圖片的核心與手動標籤", processed_count=0, total_target=len(delete_ids))
    merged_tag_links = 0
    conn = database.get_db()
    try:
        def _merge_tags(c: sqlite3.Connection):
            nonlocal merged_tag_links
            for keep_id, dup_ids in keep_to_dups:
                placeholders = ",".join("?" for _ in dup_ids)
                tag_rows = c.execute(
                    f"""
                    SELECT it.tag_id, it.confidence, it.source, t.category
                    FROM image_tags it
                    JOIN tags t ON t.id = it.tag_id
                    WHERE it.image_id IN ({placeholders})
                    """,
                    dup_ids,
                ).fetchall()
                for tr in tag_rows:
                    if not allows_tag(tr["category"], tr["source"]):
                        continue
                    existing = c.execute(
                        "SELECT confidence, source FROM image_tags WHERE image_id = ? AND tag_id = ?",
                        (keep_id, tr["tag_id"]),
                    ).fetchone()
                    if existing is None:
                        c.execute(
                            "INSERT INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, ?, ?)",
                            (keep_id, tr["tag_id"], tr["confidence"], tr["source"]),
                        )
                        merged_tag_links += 1
                    elif existing["source"] == "manual":
                        if tr["source"] == "manual" and float(tr["confidence"] or 0) > float(existing["confidence"] or 0):
                            c.execute(
                                "UPDATE image_tags SET confidence = ? WHERE image_id = ? AND tag_id = ?",
                                (tr["confidence"], keep_id, tr["tag_id"]),
                            )
                    elif tr["source"] == "manual":
                        c.execute(
                            "UPDATE image_tags SET confidence = ?, source = 'manual' WHERE image_id = ? AND tag_id = ?",
                            (max(float(tr["confidence"] or 0), float(existing["confidence"] or 0)), keep_id, tr["tag_id"]),
                        )
                    elif float(tr["confidence"] or 0) > float(existing["confidence"] or 0):
                        c.execute(
                            "UPDATE image_tags SET confidence = ?, source = ? WHERE image_id = ? AND tag_id = ?",
                            (tr["confidence"], tr["source"], keep_id, tr["tag_id"]),
                        )

                # Inherit suggested_folder if keep_id is in root and a duplicate had a folder or suggested_folder
                keep_row = c.execute(
                    "SELECT folder, suggested_folder FROM images WHERE id = ?", (keep_id,)
                ).fetchone()
                if keep_row and not keep_row["folder"] and not keep_row["suggested_folder"]:
                    dup_folders = c.execute(
                        f"SELECT folder, suggested_folder FROM images WHERE id IN ({placeholders})",
                        dup_ids,
                    ).fetchall()
                    for df in dup_folders:
                        cand_folder = df["folder"] or df["suggested_folder"] or ""
                        if cand_folder and cand_folder not in database.NON_ARTIST_FOLDERS:
                            c.execute(
                                "UPDATE images SET suggested_folder = ? WHERE id = ?",
                                (cand_folder, keep_id),
                            )
                            break

        database.run_db_write_with_retry(conn, _merge_tags)
    finally:
        conn.close()

    # Delete files to Windows Recycle Bin in chunks of 500
    total_deleted = 0
    total_recycled = 0
    all_deleted_ids: List[int] = []
    for start in range(0, len(delete_ids), 500):
        chunk = delete_ids[start : start + 500]
        update_job(
            stage=f"移至資源回收筒 ({start}/{len(delete_ids)})",
            processed_count=start,
            total_target=len(delete_ids),
        )
        del_res = gallery_deletion.delete_images(chunk)
        total_deleted += int(del_res.get("deleted", 0))
        total_recycled += int(del_res.get("recycled", 0))
        all_deleted_ids.extend(del_res.get("deleted_ids", []))

    # Clean up visual_hashes.db
    if all_deleted_ids and _visual_hash_db_path().exists():
        vconn = _open_visual_hash_db()
        try:
            with vconn:
                vconn.executemany(
                    "DELETE FROM visual_hashes WHERE id = ?",
                    [(iid,) for iid in all_deleted_ids],
                )
                vconn.executemany(
                    "DELETE FROM visual_pair_cache WHERE id1 = ? OR id2 = ?",
                    [(iid, iid) for iid in all_deleted_ids],
                )
        finally:
            vconn.close()

    reclaimed_mb = round(reclaim_bytes / (1024 * 1024), 2)
    database.log_operation(
        category="files",
        action="清除重複與相似圖片",
        summary=(
            f"已清除 {total_deleted} 張重複/相似圖片（{total_recycled} 個檔案移至系統資源回收筒，"
            f"釋放 {reclaimed_mb} MB，合併 {merged_tag_links} 筆標籤，備份檔：{backup_info.get('filename', '')}）"
        ),
        affected_count=total_deleted,
        details={
            "deleted_count": total_deleted,
            "recycled_count": total_recycled,
            "merged_tag_links": merged_tag_links,
            "reclaimed_bytes": reclaim_bytes,
            "reclaimed_mb": reclaimed_mb,
            "backup_file": backup_info.get("filename", ""),
        },
    )
    return {
        "deleted_count": total_deleted,
        "recycled_count": total_recycled,
        "merged_tag_links": merged_tag_links,
        "reclaimed_bytes": reclaim_bytes,
        "reclaimed_mb": reclaimed_mb,
        "backup_file": backup_info.get("filename", ""),
        "deleted_ids": all_deleted_ids,
        "message": f"已安全清除 {total_deleted} 張重複圖片並移至 Windows 資源回收筒（釋放 {reclaimed_mb} MB）",
    }


@exclusive_job("比對與清除重複圖片")
def clean_duplicates(
    mode: str = "both",
    scope: str = "same_folder",
    folder: str = "__ALL__",
    execute_delete: bool = False,
    max_mae: float = 6.5,
    min_ssim: float = 0.95,
    max_diff_frac: float = 0.005,
    max_groups: int = 200,
) -> Dict[str, Any]:
    """
    Scan and optionally delete exact and visually identical/recompressed duplicate images.
    When execute_delete=False, returns a non-destructive preview of all duplicate groups.
    When execute_delete=True, merges core/manual tags onto the highest-resolution kept image,
    creates a DB backup, and moves redundant copies to the Windows Recycle Bin.
    """
    scan_res = scan_duplicates(
        mode=mode,
        scope=scope,
        folder=folder,
        max_mae=max_mae,
        min_ssim=min_ssim,
        max_diff_frac=max_diff_frac,
        max_groups=None if execute_delete else max_groups,
    )
    if not execute_delete:
        return {
            "preview": True,
            **scan_res,
        }

    exec_res = execute_dedup_groups(scan_res["groups"])
    scan_res["groups"] = scan_res["groups"][:max_groups]
    scan_res["groups_truncated"] = scan_res["total_groups"] > max_groups
    return {
        "preview": False,
        **scan_res,
        "execution": exec_res,
    }
