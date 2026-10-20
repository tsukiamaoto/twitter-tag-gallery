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
        fingerprint = f"{PIPELINE_VERSION}|{row['rel_path']}|{stat.st_size}|{stat.st_mtime_ns}"
        version = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:24]
        return source, stat, self.cache_dir / f"{row['id']}-{version}.jpg", f'"{version}"'

    def cached(self, image_id):
        row = self._record(image_id)
        source, stat, target, etag = self._target(row)
        if row["media_type"] == "video":
            return Thumbnail(None, etag, video=True)
        if target.is_file():
            return Thumbnail(target, etag)
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
                job.future.set_exception(exc)
            else:
                job.future.set_result(result)
            finally:
                with self._lock:
                    if self._pending.get(job.image_id) is job:
                        del self._pending[job.image_id]

    def _generate(self, job):
        row = self._record(job.image_id)
        source, stat, target, etag = self._target(row)
        if row["media_type"] == "video":
            return Thumbnail(None, etag, video=True)
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
                    "SELECT id FROM images WHERE id > ? AND media_type != 'video' AND missing_since IS NULL ORDER BY id LIMIT 32", (cursor,)
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
