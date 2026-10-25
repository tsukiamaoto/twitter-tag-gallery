# -*- coding: utf-8 -*-
"""Exact file and full-resolution pixel duplicate detection and safe cleanup."""
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
from pathlib import Path
import sqlite3
import struct
import zlib
from typing import Any, Dict, List, Optional, Set, Tuple

import cv2
import numpy as np
from PIL import Image

import database
from background_work import exclusive_job, update_job
from file_safety import gallery_path
from tag_policy import allows_tag


PIXEL_CACHE_VERSION = "opencv-bgra-v1:" + cv2.__version__
HEADER_CACHE_VERSION = "metadata-v1"


def _report(progress=None, **values):
    update_job(**values)
    if progress is not None:
        progress(**values)


def _visual_hash_db_path() -> Path:
    return Path(database.DATA_DIR) / "visual_hashes.db"


def _open_visual_hash_db() -> sqlite3.Connection:
    db_path = _visual_hash_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    # Keep legacy tables for compatibility, but never read their perceptual matches.
    conn.execute("""CREATE TABLE IF NOT EXISTS visual_hashes (
        id INTEGER PRIMARY KEY, rel_path TEXT, folder TEXT, filename TEXT, ext TEXT,
        size_bytes INTEGER, width INTEGER, height INTEGER, dhash TEXT, phash TEXT, updated_at REAL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS visual_pair_cache (
        id1 INTEGER, id2 INTEGER, size1 INTEGER, size2 INTEGER, mae REAL, ssim REAL,
        significant_frac REAL, ar_diff REAL, PRIMARY KEY (id1, id2)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS pixel_hashes (
        id INTEGER PRIMARY KEY, rel_path TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL, version TEXT NOT NULL,
        width INTEGER NOT NULL, height INTEGER NOT NULL, pixel_hash TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS image_headers (
        id INTEGER PRIMARY KEY, rel_path TEXT NOT NULL, size_bytes INTEGER NOT NULL,
        mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL, version TEXT NOT NULL,
        width INTEGER NOT NULL, height INTEGER NOT NULL
    )""")
    return conn


def _is_animated_image(img: Image.Image) -> bool:
    # A failure to determine frame count must not allow first-frame deduplication.
    return bool(getattr(img, "is_animated", False) or int(getattr(img, "n_frames", 1)) > 1)


def _file_signature(path: Path) -> Tuple[int, int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _files_identical(path1: Path, path2: Path) -> bool:
    """Read every byte, including the middle of large files; do not reuse filecmp's cache."""
    try:
        before1, before2 = _file_signature(path1), _file_signature(path2)
        if before1[0] != before2[0]:
            return False
        with path1.open("rb") as first, path2.open("rb") as second:
            while True:
                block = first.read(1024 * 1024)
                if block != second.read(1024 * 1024):
                    return False
                if not block:
                    break
        return before1 == _file_signature(path1) and before2 == _file_signature(path2)
    except OSError:
        return False


def _png_metadata(path: Path) -> Dict[str, Any]:
    """Read display metadata, including chunks after IDAT, without inflating pixels."""
    info = {}
    with path.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            raise ValueError("load_error")
        size = path.stat().st_size
        while True:
            header = stream.read(8)
            if len(header) != 8:
                raise ValueError("load_error")
            length, kind = struct.unpack(">I4s", header)
            end = stream.tell() + length + 4
            if end > size:
                raise ValueError("load_error")
            if kind in (b"eXIf", b"iCCP", b"gAMA", b"sRGB", b"cHRM", b"acTL"):
                if length > 16 * 1024 * 1024:
                    raise ValueError("oversized_metadata")
                data = stream.read(length)
                if kind == b"eXIf":
                    info["exif"] = data
                elif kind == b"iCCP":
                    _, compressed = data.split(b"\0", 1)
                    if not compressed or compressed[0] != 0:
                        raise ValueError("invalid_icc_profile")
                    profile = zlib.decompressobj().decompress(compressed[1:], 16 * 1024 * 1024 + 1)
                    if len(profile) > 16 * 1024 * 1024:
                        raise ValueError("oversized_metadata")
                    info["icc_profile"] = profile
                elif kind == b"gAMA":
                    info["gamma"] = struct.unpack(">I", data)[0] / 100000.0
                elif kind == b"sRGB":
                    info["srgb"] = data[0]
                elif kind == b"cHRM":
                    info["chromaticity"] = tuple(v / 100000.0 for v in struct.unpack(">8I", data))
                elif struct.unpack(">II", data)[0] > 1:
                    raise ValueError("animated_media")
            stream.seek(end)
            if kind == b"IEND":
                return info


def _image_header(path: Path) -> Tuple[int, int, str]:
    """Read dimensions and display semantics without a Pillow raster decode."""
    if path.suffix.lower() in (".gif", ".mp4"):
        raise ValueError("animated_media")
    with Image.open(path) as image:
        if _is_animated_image(image):
            raise ValueError("animated_media")
        width, height = image.size
        if image.format == "PNG":
            info = _png_metadata(path)
            exif = Image.Exif()
            if info.get("exif"):
                exif.load(info["exif"])
        else:
            info = image.info
            exif = image.getexif()
        display_metadata = repr((
            exif.get(274, 1), info.get("icc_profile"),
            info.get("gamma"), info.get("srgb"), info.get("chromaticity"),
        ))
        return width, height, display_metadata


def _load_full_image(path: Path, header=None) -> Tuple[np.ndarray, str]:
    """Decode once, without resizing, preserving alpha, bit depth and display metadata."""
    _, _, display_metadata = header or _image_header(path)
    # Reading the bytes ourselves also supports Windows paths containing Chinese.
    pixels = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if pixels is None or pixels.size == 0 or pixels.dtype not in (np.uint8, np.uint16):
        raise ValueError("load_error")
    if pixels.ndim == 2:
        pixels = cv2.cvtColor(pixels, cv2.COLOR_GRAY2BGRA)
    elif pixels.ndim == 3 and pixels.shape[2] == 3:
        pixels = cv2.cvtColor(pixels, cv2.COLOR_BGR2BGRA)
    elif pixels.ndim != 3 or pixels.shape[2] != 4:
        raise ValueError("unsupported_channels")
    return pixels, display_metadata


def _compute_pixel_fingerprint(path: Path) -> Optional[Dict[str, Any]]:
    try:
        before = _file_signature(path)
        pixels, display_metadata = _load_full_image(path)
        fingerprint = hashlib.sha256()
        fingerprint.update(repr((pixels.shape, pixels.dtype.str, display_metadata)).encode("utf-8"))
        fingerprint.update(memoryview(pixels))
        if before != _file_signature(path):
            return None
        return {
            "width": int(pixels.shape[1]), "height": int(pixels.shape[0]),
            "pixel_hash": fingerprint.hexdigest(),
            "size_bytes": before[0], "mtime_ns": before[1], "ctime_ns": before[2],
        }
    except (OSError, ValueError, cv2.error, struct.error, zlib.error):
        return None


def ensure_pixel_hashes(rows: List[Dict[str, Any]], progress=None) -> Dict[int, Dict[str, Any]]:
    """Cache full pixel fingerprints; invalidate on path, size, timestamps or decoder changes."""
    conn = _open_visual_hash_db()
    try:
        cached = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM pixel_hashes")}
        result: Dict[int, Dict[str, Any]] = {}
        pending = []
        for row in rows:
            if str(row.get("ext") or "").lower() in (".gif", ".mp4") or row.get("media_type") in ("video", "gif"):
                continue
            try:
                path = gallery_path(database.GALLERY_ROOT, row["rel_path"])
                signature = _file_signature(path)
            except (OSError, ValueError):
                continue
            entry = cached.get(row["id"])
            if (entry and entry["rel_path"] == row["rel_path"]
                    and entry["version"] == PIXEL_CACHE_VERSION
                    and (entry["size_bytes"], entry["mtime_ns"], entry["ctime_ns"]) == signature):
                result[row["id"]] = entry
            else:
                pending.append((row, path))

        def compute(item):
            row, path = item
            return row, _compute_pixel_fingerprint(path)

        pending_inserts = []

        def flush():
            with conn:
                conn.executemany("""INSERT OR REPLACE INTO pixel_hashes
                    (id, rel_path, size_bytes, mtime_ns, ctime_ns, version, width, height, pixel_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", pending_inserts)
            pending_inserts.clear()

        _report(progress, stage="建立候選圖像素索引", processed_count=len(result),
                total_target=len(result) + len(pending), cached_images=len(result), decode_target=len(pending))
        # Finish small images without waiting for an earlier large image's decode.
        with ThreadPoolExecutor(max_workers=2) as pool:
            iterator = iter(pending)
            futures = {pool.submit(compute, item) for item in list(pending[:2])}
            for _ in range(min(2, len(pending))):
                next(iterator)
            cached_count = len(result)
            index = 0
            while futures:
                done, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    row, entry = future.result()
                    index += 1
                    _report(progress, stage="建立候選圖像素索引", processed_count=cached_count + index,
                            total_target=cached_count + len(pending), decoded_images=index,
                            cached_images=cached_count, decode_target=len(pending))
                    if entry is not None:
                        result[row["id"]] = entry
                        pending_inserts.append((
                            row["id"], row["rel_path"], entry["size_bytes"], entry["mtime_ns"],
                            entry["ctime_ns"], PIXEL_CACHE_VERSION, entry["width"], entry["height"], entry["pixel_hash"],
                        ))
                    if len(pending_inserts) >= 250:
                        flush()
                    item = next(iterator, None)
                    if item is not None:
                        futures.add(pool.submit(compute, item))
        if pending_inserts:
            flush()
        return result
    finally:
        conn.close()


def _pixel_candidates(rows, scope, progress=None):
    """Only dimensions with another possible match require full raster decoding."""
    conn = _open_visual_hash_db()
    try:
        headers = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM image_headers")}
        pixels = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM pixel_hashes")}
        buckets = defaultdict(list)
        inserts = []
        eligible = [r for r in rows if str(r.get("ext") or "").lower() not in (".gif", ".mp4")
                    and r.get("media_type") not in ("gif", "video")]
        for index, row in enumerate(eligible, 1):
            try:
                path = gallery_path(database.GALLERY_ROOT, row["rel_path"])
                signature = _file_signature(path)
                entry = None
                for cache, version in ((pixels, PIXEL_CACHE_VERSION), (headers, HEADER_CACHE_VERSION)):
                    candidate = cache.get(row["id"])
                    if (candidate and candidate["rel_path"] == row["rel_path"] and candidate["version"] == version
                            and (candidate["size_bytes"], candidate["mtime_ns"], candidate["ctime_ns"]) == signature):
                        entry = candidate
                        break
                if entry is None:
                    # Dimensions and frame count live in the header; no EXIF or
                    # display metadata is needed until this file has a candidate.
                    with Image.open(path) as image:
                        if _is_animated_image(image):
                            continue
                        width, height = image.size
                    if signature != _file_signature(path):
                        continue
                    entry = {"width": width, "height": height}
                    inserts.append((row["id"], row["rel_path"], *signature, HEADER_CACHE_VERSION, width, height))
                row["width"], row["height"] = entry["width"], entry["height"]
                key = (str(row.get("folder") or "") if scope == "same_folder" else "__ALL__",
                       row["width"], row["height"])
                buckets[key].append(row)
            except (OSError, ValueError, cv2.error, struct.error, zlib.error):
                continue
            finally:
                if index == 1 or index % 25 == 0 or index == len(eligible):
                    _report(progress, stage="檢查尺寸與快取", processed_count=index, total_target=len(eligible))
            if len(inserts) >= 250:
                with conn:
                    conn.executemany("INSERT OR REPLACE INTO image_headers VALUES(?,?,?,?,?,?,?,?)", inserts)
                inserts.clear()
        if inserts:
            with conn:
                conn.executemany("INSERT OR REPLACE INTO image_headers VALUES(?,?,?,?,?,?,?,?)", inserts)
        candidates = [r for bucket in buckets.values() if len(bucket) > 1 for r in bucket]
        _report(progress, candidate_images=len(candidates), skipped_images=len(eligible) - len(candidates))
        return candidates
    finally:
        conn.close()


def compare_visual_identity(
    path1: Path, path2: Path,
    w1: int = 0, h1: int = 0, w2: int = 0, h2: int = 0,
    max_mae: float = 6.5, min_ssim: float = 0.95,
    max_diff_frac: float = 0.005, max_ar_diff: float = 0.025,
) -> Tuple[bool, Dict[str, Any]]:
    """Require identical full-resolution pixels, including alpha.

    Legacy dimensions and similarity thresholds are accepted for client compatibility
    only. They cannot relax equality; decoded dimensions always take precedence.
    """
    try:
        before1, before2 = _file_signature(path1), _file_signature(path2)
        header1, header2 = _image_header(path1), _image_header(path2)
        if header1[:2] != header2[:2]:
            return False, {"reason": "dimensions_mismatch"}
        if header1[2] != header2[2]:
            return False, {"reason": "display_metadata_mismatch"}
        arr1, metadata1 = _load_full_image(path1, header1)
        arr2, metadata2 = _load_full_image(path2, header2)
        if arr1.shape != arr2.shape:
            return False, {"reason": "dimensions_mismatch"}
        if arr1.dtype != arr2.dtype:
            return False, {"reason": "bit_depth_mismatch"}
        if metadata1 != metadata2:
            return False, {"reason": "display_metadata_mismatch"}
        changed_pixels = 0
        max_channel_diff = 0
        # Compare every channel at every original pixel in bounded-size strips.
        for start in range(0, arr1.shape[0], 128):
            diff = cv2.absdiff(arr1[start:start + 128], arr2[start:start + 128])
            changed_pixels += int(np.count_nonzero(np.any(diff != 0, axis=2)))
            max_channel_diff = max(max_channel_diff, int(diff.max()))
        if before1 != _file_signature(path1) or before2 != _file_signature(path2):
            return False, {"reason": "file_changed"}
        metrics = {"method": "opencv_exact_pixels", "changed_pixels": changed_pixels,
                   "total_pixels": int(arr1.shape[0] * arr1.shape[1]), "max_channel_diff": max_channel_diff}
        return changed_pixels == 0, metrics
    except (OSError, ValueError, cv2.error, struct.error, zlib.error) as error:
        return False, {"reason": str(error)}


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


def scan_duplicates(
    mode: str = "both",
    scope: str = "same_folder",
    folder: str = "__ALL__",
    max_mae: float = 6.5,
    min_ssim: float = 0.95,
    max_diff_frac: float = 0.005,
    max_groups: Optional[int] = 300,
    progress=None,
) -> Dict[str, Any]:
    """
    Scan the gallery for exact duplicates (SHA-256 + byte-for-byte verification)
    and/or identical decoded static images (OpenCV full-resolution pixel equality).
    Legacy similarity thresholds are ignored and never relax pixel equality.
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

    _report(progress, stage="讀取圖庫索引", processed_count=0, total_target=total_scanned)
    for row in rows:
        row["width"] = row["height"] = 0

    groups: List[Dict[str, Any]] = []
    exact_deleted_ids: Set[int] = set()
    exact_groups_count = 0
    visual_groups_count = 0
    exact_duplicate_images = 0
    visual_duplicate_images = 0

    # --- Phase 1: Exact Duplicate Scan (content_hash + full byte verification) ---
    if mode in ("both", "exact"):
        _report(progress, stage="驗證完全相同的檔案", processed_count=0, total_target=total_scanned)
        hash_buckets: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            chash = str(r.get("content_hash") or "").strip()
            if not chash:
                continue
            bucket_folder = str(r.get("folder") or "") if scope == "same_folder" else "__ALL__"
            hash_buckets[(bucket_folder, chash)].append(r)

        for bucket_index, ((_, chash), bucket) in enumerate(hash_buckets.items(), 1):
            if bucket_index % 50 == 0 or bucket_index == len(hash_buckets):
                _report(progress, stage="驗證完全相同的檔案", processed_count=bucket_index, total_target=len(hash_buckets))
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
                        is_identical = _files_identical(keep_path, other_path)
                    except Exception:
                        is_identical = False
                    if is_identical:
                        used_in_bucket.add(other["id"])
                        exact_deleted_ids.add(other["id"])
                        dup_summary = _format_image_summary(other)
                        dup_summary["reason"] = "完全重複 (SHA-256 與位元組 100% 相同)"
                        dup_summary["metrics"] = {"method": "exact_bytes", "changed_bytes": 0}
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

    # --- Phase 2: full-resolution pixel equality, never perceptual similarity ---
    if mode in ("both", "visual"):
        remaining = [row for row in rows if row["id"] not in exact_deleted_ids]
        _report(progress, exact_copies_skipped=len(exact_deleted_ids))
        candidates = _pixel_candidates(remaining, scope, progress)
        by_id = {row["id"]: row for row in rows}
        for group in groups:
            keep = by_id[group["keep"]["id"]]
            for summary in [group["keep"], *group["duplicates"]]:
                summary.update(width=keep["width"], height=keep["height"], pixels=keep["width"] * keep["height"])
        pixel_map = ensure_pixel_hashes(candidates, progress)
        pixel_buckets: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            entry = pixel_map.get(row["id"])
            if row["id"] in exact_deleted_ids or not entry:
                continue
            bucket_folder = str(row.get("folder") or "") if scope == "same_folder" else "__ALL__"
            pixel_buckets[(bucket_folder, entry["pixel_hash"])].append(row)

        matching_buckets = [bucket for bucket in pixel_buckets.values() if len(bucket) > 1]
        _report(progress, stage="原尺寸逐像素驗證", processed_count=0, total_target=len(matching_buckets))
        for bucket_index, bucket in enumerate(matching_buckets, 1):
            if len(bucket) < 2:
                continue
            ranked = sorted(bucket, key=_quality_rank_key, reverse=True)
            used: Set[int] = set()
            for index, keep_item in enumerate(ranked):
                if keep_item["id"] in used:
                    continue
                keep_path = gallery_path(database.GALLERY_ROOT, keep_item["rel_path"])
                verified_dups = []
                for other in ranked[index + 1:]:
                    if other["id"] in used:
                        continue
                    other_path = gallery_path(database.GALLERY_ROOT, other["rel_path"])
                    matched, metrics = compare_visual_identity(keep_path, other_path)
                    if not matched:
                        continue
                    used.add(other["id"])
                    summary = _format_image_summary(other)
                    summary["reason"] = "原尺寸逐像素完全相同 (OpenCV，差異像素 0)"
                    summary["metrics"] = metrics
                    verified_dups.append(summary)
                if verified_dups:
                    used.add(keep_item["id"])
                    # Fold byte-identical subgroups into the pixel group so a kept
                    # image never becomes a deletion target in another group.
                    matched_ids = {keep_item["id"]} | {d["id"] for d in verified_dups}
                    absorbed = [g for g in groups if g["keep"]["id"] in matched_ids]
                    for group in absorbed:
                        for duplicate in group["duplicates"]:
                            duplicate_path = gallery_path(database.GALLERY_ROOT, duplicate["rel_path"])
                            matched, metrics = compare_visual_identity(keep_path, duplicate_path)
                            if matched:
                                duplicate["reason"] = "原尺寸逐像素完全相同 (OpenCV，差異像素 0)"
                                duplicate["metrics"] = metrics
                                verified_dups.append(duplicate)
                        groups.remove(group)
                    for duplicate in verified_dups:
                        duplicate_path = gallery_path(database.GALLERY_ROOT, duplicate["rel_path"])
                        if _files_identical(keep_path, duplicate_path):
                            duplicate["reason"] = "完全重複 (SHA-256 與位元組 100% 相同)"
                            duplicate["metrics"] = {"method": "exact_bytes", "changed_bytes": 0}
                    folders = {keep_item.get("folder") or ""} | {d["folder"] for d in verified_dups}
                    groups.append({
                        "group_id": len(groups) + 1, "match_type": "visual",
                        "folder": next(iter(folders)) if len(folders) == 1 else "跨資料夾",
                        "keep": _format_image_summary(keep_item), "duplicates": verified_dups,
                        "reclaim_bytes": sum(d["size_bytes"] for d in verified_dups),
                    })
            _report(progress, stage="原尺寸逐像素驗證", processed_count=bucket_index, total_target=len(matching_buckets))

    for index, group in enumerate(groups, 1):
        group["group_id"] = index
        if all(d["metrics"]["method"] == "exact_bytes" for d in group["duplicates"]):
            group["match_type"] = "exact"
    exact_groups_count = sum(g["match_type"] == "exact" for g in groups)
    visual_groups_count = len(groups) - exact_groups_count
    exact_duplicate_images = sum(d["metrics"]["method"] == "exact_bytes" for g in groups for d in g["duplicates"])
    visual_duplicate_images = sum(len(g["duplicates"]) for g in groups) - exact_duplicate_images
    all_delete_ids = [d["id"] for g in groups for d in g["duplicates"]]
    total_reclaim_bytes = sum(g["reclaim_bytes"] for g in groups)
    shown_groups = groups if max_groups is None else groups[:max_groups]

    _report(progress, stage="比對完成", processed_count=total_scanned, total_target=total_scanned)
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


@exclusive_job("清除重複圖片")
def execute_dedup_groups(
    groups: List[Dict[str, Any]],
    selected_delete_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """
    Safely execute duplicate cleanup for the given groups:
    1. Revalidate current file contents, then create a verified SQLite backup.
    2. Merge core tags (artist, character, copyright, source) and manual tags from duplicates onto kept images.
    3. Move duplicate files to the Windows Recycle Bin via gallery_deletion.delete_images().
    4. Clean up visual_hashes.db and write an operation log entry.
    """
    import backup_service
    import gallery_deletion

    allowed_ids: Optional[Set[int]] = set(selected_delete_ids) if selected_delete_ids is not None else None
    keep_to_dups: List[Tuple[int, List[int]]] = []
    delete_ids: List[int] = []

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

    # Web clients can submit old previews. Never trust their paths or similarity results.
    keep_ids = {keep_id for keep_id, _ in keep_to_dups}
    if keep_ids.intersection(delete_ids):
        raise ValueError("保留圖片同時被選為刪除對象，請重新掃描")
    needed_ids = sorted(keep_ids | set(delete_ids))
    conn = database.get_db()
    current_rows = {}
    try:
        for start in range(0, len(needed_ids), 500):
            chunk = needed_ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            current_rows.update({row["id"]: dict(row) for row in conn.execute(
                f"SELECT id, rel_path FROM images WHERE missing_since IS NULL AND id IN ({placeholders})", chunk
            )})
    finally:
        conn.close()
    reclaim_bytes = 0
    counted_ids = set()
    for keep_id, dup_ids in keep_to_dups:
        for dup_id in dup_ids:
            update_job(stage="清除前重新驗證原始檔案", processed_count=len(counted_ids), total_target=len(delete_ids))
            if keep_id not in current_rows or dup_id not in current_rows:
                raise ValueError("圖片已變更或不存在，請重新掃描後再清除")
            keep_path = gallery_path(database.GALLERY_ROOT, current_rows[keep_id]["rel_path"])
            dup_path = gallery_path(database.GALLERY_ROOT, current_rows[dup_id]["rel_path"])
            identical = _files_identical(keep_path, dup_path)
            if not identical:
                identical, _ = compare_visual_identity(keep_path, dup_path)
            if not identical:
                raise ValueError(f"圖片內容不同或已變更，已停止清除，請重新掃描（ID {keep_id} / {dup_id}）")
            if dup_id not in counted_ids:
                reclaim_bytes += dup_path.stat().st_size
                counted_ids.add(dup_id)

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
                    "DELETE FROM pixel_hashes WHERE id = ?",
                    [(iid,) for iid in all_deleted_ids],
                )
                vconn.executemany(
                    "DELETE FROM image_headers WHERE id = ?",
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
        action="清除重複圖片",
        summary=(
            f"已清除 {total_deleted} 張重複圖片（{total_recycled} 個檔案移至系統資源回收筒，"
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
    Scan and optionally delete byte-identical or full-resolution pixel-identical images.
    Legacy similarity thresholds are ignored.
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
