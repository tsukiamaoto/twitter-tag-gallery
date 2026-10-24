"""Bounded, single-flight thumbnail generation with atomic versioned files."""
import concurrent.futures
from dataclasses import dataclass
import hashlib
import itertools
import logging
import os
from pathlib import Path
import queue
import shutil
import threading
import time
import uuid

from PIL import Image, ImageOps

from background_work import (
    FileLock, background_status, interactive_recently, media_io,
)

log = logging.getLogger(__name__)
PIPELINE_VERSION = "v3"
VIDEO_PIPELINE_VERSION = "v4"


def _extract_windows_video_thumbnail(source: Path, max_size: int = 380) -> Image.Image | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

    class BITMAP(ctypes.Structure):
        _fields_ = [
            ("bmType", wintypes.LONG),
            ("bmWidth", wintypes.LONG),
            ("bmHeight", wintypes.LONG),
            ("bmWidthBytes", wintypes.LONG),
            ("bmPlanes", wintypes.WORD),
            ("bmBitsPixel", wintypes.WORD),
            ("bmBits", ctypes.c_void_p),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32
    gdi32 = ctypes.windll.gdi32
    user32 = ctypes.windll.user32

    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None

    sh_create_item = shell32.SHCreateItemFromParsingName
    sh_create_item.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    sh_create_item.restype = ctypes.c_long

    gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
    gdi32.GetObjectW.restype = ctypes.c_int
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int

    iid_factory = GUID(
        0xBCC18B79,
        0xBA16,
        0x442F,
        (ctypes.c_ubyte * 8)(0x80, 0xC4, 0x8A, 0x59, 0xC3, 0x0C, 0x46, 0x3B),
    )
    get_image_proto = ctypes.WINFUNCTYPE(
        ctypes.c_long,
        ctypes.c_void_p,
        SIZE,
        ctypes.c_int,
        ctypes.POINTER(wintypes.HBITMAP),
    )
    release_proto = ctypes.WINFUNCTYPE(wintypes.ULONG, ctypes.c_void_p)

    hr_init = ole32.CoInitializeEx(None, 0x0)
    should_uninit = hr_init in (0, 1)
    try:
        p_factory = ctypes.c_void_p()
        hr = sh_create_item(
            os.fspath(source.resolve()),
            None,
            ctypes.byref(iid_factory),
            ctypes.byref(p_factory),
        )
        if hr < 0 or not p_factory.value:
            return None
        vtable = ctypes.cast(
            p_factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        get_image = get_image_proto(vtable[3])
        release = release_proto(vtable[2])
        hbitmap = wintypes.HBITMAP()
        try:
            # SIIGBF_THUMBNAILONLY (0x08) | SIIGBF_BIGGERSIZEOK (0x01)
            hr = get_image(p_factory, SIZE(max_size, max_size), 0x08 | 0x01, ctypes.byref(hbitmap))
            if hr < 0 or not hbitmap.value:
                return None
            bmp = BITMAP()
            if not gdi32.GetObjectW(hbitmap, ctypes.sizeof(bmp), ctypes.byref(bmp)):
                return None
            w, h = int(bmp.bmWidth), int(bmp.bmHeight)
            if w <= 0 or h <= 0:
                return None
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h  # Top-down DIB
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0  # BI_RGB
            buf = ctypes.create_string_buffer(w * h * 4)
            hdc = user32.GetDC(None)
            try:
                lines = gdi32.GetDIBits(hdc, hbitmap, 0, h, buf, ctypes.byref(bmi), 0)
            finally:
                if hdc:
                    user32.ReleaseDC(None, hdc)
            if not lines:
                return None
            image = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1).copy()
            image.thumbnail((max_size, max_size), Image.Resampling.BILINEAR)
            return image.convert("RGB")
        finally:
            if hbitmap.value:
                gdi32.DeleteObject(hbitmap)
            release(p_factory)
    except Exception:
        log.debug("Windows shell video thumbnail extraction failed for %s", source, exc_info=True)
        return None
    finally:
        if should_uninit:
            ole32.CoUninitialize()


def _extract_ffmpeg_video_thumbnail(source: Path, max_size: int = 380) -> Image.Image | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    import io
    import subprocess

    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "0.1",
                "-i",
                os.fspath(source),
                "-frames:v",
                "1",
                "-vf",
                f"scale='min({max_size},iw)':'min({max_size},ih)':force_original_aspect_ratio=decrease",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-",
            ],
            capture_output=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if proc.returncode == 0 and proc.stdout:
            with Image.open(io.BytesIO(proc.stdout)) as img:
                img.thumbnail((max_size, max_size), Image.Resampling.BILINEAR)
                return img.convert("RGB")
    except Exception:
        log.debug("ffmpeg video thumbnail extraction failed for %s", source, exc_info=True)
    return None


def _extract_video_thumbnail(source: Path, max_size: int = 380) -> Image.Image | None:
    image = _extract_windows_video_thumbnail(source, max_size=max_size)
    if image is not None:
        return image
    return _extract_ffmpeg_video_thumbnail(source, max_size=max_size)


def ensure_video_screenshot(
    image_id: int,
    source: Path,
    cache_dir: Path | None = None,
    max_size: int = 720,
    override_bytes: bytes | None = None,
) -> Path | None:
    """為影片擷取或更新單一畫面截圖 (.jpg) 供畫師追蹤與 AI 辨識使用。"""
    import io
    from database import THUMB_DIR
    target_dir = Path(cache_dir) if cache_dir is not None else THUMB_DIR
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        existing = next(target_dir.glob(f"{image_id}-*.jpg"), None)
        if override_bytes:
            out_path = existing or (target_dir / f"{image_id}-screenshot.jpg")
            with Image.open(io.BytesIO(override_bytes)) as img:
                rgb = img.convert("RGB")
                rgb.thumbnail((max_size, max_size), Image.Resampling.BILINEAR)
                rgb.save(out_path, format="JPEG", quality=90, optimize=True)
            return out_path
        if existing and existing.is_file():
            return existing
        if not source.is_file():
            return None
        stat = source.stat()
        digest = hashlib.blake2b(
            f"{VIDEO_PIPELINE_VERSION}:{source}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8"),
            digest_size=12,
        ).hexdigest()
        out_path = target_dir / f"{image_id}-{digest}.jpg"
        img = _extract_video_thumbnail(source, max_size=max_size)
        if img is None:
            return None
        with img:
            img.save(out_path, format="JPEG", quality=88, optimize=True)
        return out_path
    except Exception:
        log.debug("Failed to ensure video screenshot for %s", source, exc_info=True)
        return None


@dataclass
class Thumbnail:
    path: Path | None
    etag: str
    video: bool = False


@dataclass
class _Job:
    image_id: int
    future: concurrent.futures.Future
    foreground: bool
    started: bool = False


class ThumbnailQueueFull(RuntimeError):
    pass


class ThumbnailCache:
    def __init__(self, root, cache_dir, get_db, max_pending=128):
        self.root = Path(root)
        self.cache_dir = Path(cache_dir)
        self.get_db = get_db
        self.max_pending = max_pending
        self._queue = queue.PriorityQueue()
        self._counter = itertools.count()
        self._pending = {}
        self._warm_ids = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads = []
        self._warmer = None

    def _record(self, image_id):
        conn = self.get_db()
        try:
            row = conn.execute("SELECT id, rel_path, media_type FROM images WHERE id = ?", (image_id,)).fetchone()
            if row is None:
                raise FileNotFoundError("Image not found")
            return dict(row)
        finally:
            conn.close()

    def _target(self, row):
        source = self.root / row["rel_path"]
        stat = source.stat()
        pipeline = VIDEO_PIPELINE_VERSION if row["media_type"] == "video" else PIPELINE_VERSION
        fingerprint = f"{pipeline}|{row['rel_path']}|{stat.st_size}|{stat.st_mtime_ns}"
        version = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
        return source, stat, self.cache_dir / f"{row['id']}-{version}.jpg", f'"{version}"'

    def cached(self, image_id):
        row = self._record(image_id)
        source, stat, target, etag = self._target(row)
        if target.is_file():
            self._warm_ids.add(image_id)
            return Thumbnail(target, etag)
        self._warm_ids.discard(image_id)
        return None

    def submit(self, image_id, foreground=True):
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("Thumbnail cache is shutting down")
            job = self._pending.get(image_id)
            if job is not None:
                if foreground and not job.foreground:
                    job.foreground = True
                    if not job.started:
                        self._queue.put((0, next(self._counter), job))
                return job.future
            if len(self._pending) >= self.max_pending:
                raise ThumbnailQueueFull("縮圖佇列忙碌中，請稍後重試")
            job = _Job(image_id, concurrent.futures.Future(), foreground)
            self._pending[image_id] = job
            self._queue.put((0 if foreground else 1, next(self._counter), job))
            if not self._threads:
                for index in range(2):
                    thread = threading.Thread(target=self._worker, args=(index,), name=f"thumbnail-{index}", daemon=True)
                    self._threads.append(thread)
                    thread.start()
            return job.future

    def prefetch(self, image_ids):
        # Enqueue only IDs here; no per-image DB connections or source reads on
        # the image-list request's critical path.
        for image_id in image_ids:
            if image_id in self._warm_ids:
                continue
            with self._lock:
                if len(self._pending) >= self.max_pending // 2:
                    break  # Reserve queue capacity for visible image requests.
            try:
                self.submit(image_id, foreground=False)
            except (ThumbnailQueueFull, RuntimeError):
                break

    def _worker(self, index):
        while not self._stop.is_set():
            try:
                _, _, job = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if index == 0 and not job.foreground:
                # Keep one worker available even when backfill has a backlog.
                self._queue.put((1, next(self._counter), job))
                self._stop.wait(0.02)
                continue
            with self._lock:
                if job.started:
                    continue  # An old queue entry after foreground promotion.
                job.started = True
            try:
                result = self._generate(job)
            except BaseException as exc:
                self._warm_ids.discard(job.image_id)
                job.future.set_exception(exc)
            else:
                self._warm_ids.add(job.image_id)
                job.future.set_result(result)
            finally:
                with self._lock:
                    if self._pending.get(job.image_id) is job:
                        del self._pending[job.image_id]

    def _generate(self, job):
        row = self._record(job.image_id)
        source, stat, target, etag = self._target(row)
        if target.is_file():
            return Thumbnail(target, etag)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        lock = FileLock(self.cache_dir / ".locks" / f"{job.image_id}.lock")
        while not lock.acquire():
            if self._stop.wait(0.02):
                raise RuntimeError("Thumbnail cache is shutting down")
        tmp = None
        try:
            # Re-stat after waiting: a decensor/edit may have changed the source.
            source, stat, target, etag = self._target(row)
            if target.is_file():
                return Thumbnail(target, etag)
            tmp = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            with media_io(background=not job.foreground):
                if row["media_type"] == "video":
                    image = _extract_video_thumbnail(source, max_size=380)
                    if image is None:
                        return Thumbnail(None, f'"{etag.strip(chr(34))}-svg"', video=True)
                    image.save(tmp, "JPEG", quality=82)
                else:
                    legacy = self.cache_dir / f"{job.image_id}.jpg"
                    reuse = False
                    if legacy.is_file() and legacy.stat().st_mtime_ns >= stat.st_mtime_ns:
                        try:
                            with Image.open(legacy) as image:
                                image.verify()
                            reuse = True
                        except (OSError, ValueError, SyntaxError):
                            pass
                    if reuse:
                        shutil.copyfile(legacy, tmp)
                    else:
                        with Image.open(source) as image:
                            image.draft("RGB", (760, 760))
                            image = ImageOps.exif_transpose(image)
                            # Resize before converting large RGBA images to RGB.
                            image.thumbnail((380, 380), Image.Resampling.BILINEAR)
                            if image.mode != "RGB":
                                image = image.convert("RGB")
                            image.save(tmp, "JPEG", quality=82)
            if source.stat().st_mtime_ns != stat.st_mtime_ns or source.stat().st_size != stat.st_size:
                raise OSError("Source changed while generating thumbnail; retry")
            os.replace(tmp, target)
            return Thumbnail(target, etag)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            lock.release()

    def start_warming(self):
        if self._warmer is None:
            self._warmer = threading.Thread(target=self._warm_library, name="thumbnail-backfill", daemon=True)
            self._warmer.start()

    def _warm_library(self):
        self.cleanup()
        hash_cursor=0
        cursor = 0
        while not self._stop.is_set():
            # Backfill only while browsing and heavy gallery jobs are idle.
            if interactive_recently() or background_status():
                self._stop.wait(1.0)
                continue
            from library_maintenance import backfill_hashes
            hash_cursor,_=backfill_hashes(hash_cursor,limit=16)
            conn = self.get_db()
            try:
                rows = conn.execute(
                    "SELECT id FROM images WHERE id > ? AND missing_since IS NULL ORDER BY id LIMIT 32", (cursor,)
                ).fetchall()
            finally:
                conn.close()
            if not rows:
                # Also pick up later ingestion without rescanning the library.
                self._stop.wait(5.0)
                continue
            for row in rows:
                if self._stop.is_set() or interactive_recently() or background_status():
                    break
                try:
                    future = self.submit(row["id"], foreground=False)
                except ThumbnailQueueFull:
                    self._stop.wait(0.5)
                    break
                while not self._stop.is_set():
                    try:
                        future.result(timeout=0.25)
                        break
                    except concurrent.futures.TimeoutError:
                        continue
                    except Exception:
                        log.debug("Thumbnail backfill skipped image %s", row["id"], exc_info=True)
                        break
                cursor = row["id"]
                self._stop.wait(0.05)

    def cleanup(self,retention_days=7,max_delete=1000):
        """Bound work and retain recent versions for in-flight FileResponses."""
        import re
        cutoff = time.time()-retention_days*86400
        removed = 0
        if not self.cache_dir.exists():
            return 0
        for path in self.cache_dir.glob("*-*.jpg"):
            if self._stop.is_set() or interactive_recently() or removed>=max_delete:
                break
            match = re.fullmatch(r"(\d+)-[a-f0-9]{24}\.jpg",path.name)
            if not match:
                continue
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                try:
                    row = self._record(int(match[1]))
                    _,_,current,_ = self._target(row)
                except FileNotFoundError:
                    current = None
                if path != current:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def close(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        if self._warmer is not None:
            self._warmer.join(timeout=2.0)
        with self._lock:
            for job in self._pending.values():
                if not job.started and not job.future.done():
                    job.future.set_exception(RuntimeError("Thumbnail cache is shutting down"))
