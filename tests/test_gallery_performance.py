"""Regression tests use temporary galleries; no live jobs or images are changed."""
import asyncio
import concurrent.futures
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

import background_work as work
import database
from thumbnail_cache import Thumbnail, ThumbnailCache, ThumbnailQueueFull

ROOT = Path(__file__).resolve().parents[1]


class BackgroundWorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.patch = patch.object(database, "DATA_DIR", self.data)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def child(self, script):
        return subprocess.Popen(
            [sys.executable, "-u", "-c", script, str(self.data)], cwd=ROOT,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    def test_other_process_cannot_start_job_and_crash_releases_ownership(self):
        script = """
import pathlib,sys,time
import database
database.DATA_DIR=pathlib.Path(sys.argv[1])
import background_work as w
lease=w.acquire_job('child')
print('ready',flush=True)
time.sleep(30)
"""
        child = self.child(script)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertEqual(work.background_status()["pid"], child.pid)
            with self.assertRaises(work.BackgroundBusy):
                work.acquire_job("parent")
        finally:
            child.terminate()
            child.communicate(timeout=5)
        lease = work.acquire_job("after crash")
        lease.release()
        self.assertEqual(work.background_status(), {})

    def test_nested_stages_share_lease_but_other_thread_cannot_join(self):
        @work.exclusive_job("inner")
        def inner():
            return "nested"

        @work.exclusive_job("outer")
        def outer():
            self.assertEqual(inner(), "nested")
            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                with self.assertRaises(work.BackgroundBusy):
                    pool.submit(inner).result(timeout=2)
            raise ValueError("failed stage")

        with self.assertRaisesRegex(ValueError, "failed stage"):
            outer()
        self.assertEqual(work.background_status(), {})

    def test_worker_start_failure_releases_reserved_lease(self):
        lease = work.acquire_job("reserved")
        thread = unittest.mock.Mock()
        thread.start.side_effect = RuntimeError("cannot create thread")
        with self.assertRaises(RuntimeError):
            work.start_job_thread(thread, lease)
        self.assertEqual(work.background_status(), {})

    def test_schedule_slot_is_shared_and_persists_after_job_finishes(self):
        with work.acquire_job("schedule").activate():
            self.assertTrue(work.claim_schedule_slot("2026-10-20 22:00"))
        script = """
import pathlib,sys
import database
database.DATA_DIR=pathlib.Path(sys.argv[1])
import background_work as w
with w.acquire_job('other schedule').activate():
 print(w.claim_schedule_slot('2026-10-20 22:00'),flush=True)
"""
        child = self.child(script)
        out, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, err)
        self.assertEqual(out.strip(), "False")
        from datetime import datetime
        self.assertEqual(work.latest_schedule_slot(["03:00", "22:00"], datetime(2026, 10, 20, 22, 1)), "2026-10-20 22:00")
        self.assertEqual(work.latest_schedule_slot(["22:00"], datetime(2026, 10, 20, 0, 1)), "2026-10-19 22:00")

    def test_media_gate_reserves_capacity_for_foreground(self):
        entered = threading.Event()
        release = threading.Event()

        def background():
            with work.media_io(background=True):
                entered.set()
                release.wait(3)

        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            first = pool.submit(background)
            self.assertTrue(entered.wait(2))
            with work.media_io():
                second_entered = threading.Event()
                def second_background():
                    with work.media_io(background=True):
                        second_entered.set()
                second = pool.submit(second_background)
                self.assertFalse(second_entered.wait(0.1))
            release.set()
            first.result(timeout=2)
            second.result(timeout=2)
            self.assertTrue(second_entered.is_set())


class GalleryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.data = cls.root / ".tag_gallery"
        cls.patches = [patch.object(database, name, value) for name, value in {
            "GALLERY_ROOT": cls.root, "DATA_DIR": cls.data,
            "THUMB_DIR": cls.data / "thumbs", "MODEL_DIR": cls.data / "models",
            "DB_PATH": cls.data / "gallery.db",
        }.items()]
        for p in cls.patches:
            p.start()
        database.init_db()
        cls.app = importlib.import_module("app")
        cls.scheduler = importlib.import_module("scheduler")

    @classmethod
    def tearDownClass(cls):
        cls.app.thumbnail_cache.close()
        for p in reversed(cls.patches):
            p.stop()
        cls.tmp.cleanup()

    def setUp(self):
        c = database.get_db()
        with c:
            c.execute("DELETE FROM image_tags")
            c.execute("DELETE FROM tags")
            c.execute("DELETE FROM images")
            now = time.time()
            for i in range(1, 21):
                filename = f"image_{21-i}.png"
                folder = "artist" if i <= 10 else ""
                c.execute("INSERT INTO images(id,rel_path,filename,folder,ext,size_bytes,mtime,media_type,ai_tagged) VALUES(?,?,?,?,?,?,?,?,?)",
                          (i, f"{folder}/{filename}" if folder else filename, filename, folder, ".png", i*100, now-i*86400, "gif" if i == 6 else "image", i % 2))
            c.executemany("INSERT INTO tags(id,name,zh_name,category,post_count) VALUES(?,?,?,?,?)", [
                (1, "artistA", "畫師甲", "artist", 4), (2, "tagB", "條件乙", "general", 3), (3, "tagC", "條件丙", "general", 1),
            ])
            c.executemany("INSERT INTO image_tags(image_id,tag_id) VALUES(?,?)", [(1,1),(2,1),(3,1),(5,1),(2,2),(3,2),(6,2),(3,3)])
        c.close()
        self.prefetch = patch.object(self.app.thumbnail_cache, "prefetch")
        self.prefetch_mock = self.prefetch.start()
        self.addCleanup(self.prefetch.stop)

    def images(self, **changes):
        args = dict(include_tags="", exclude_tags="", folder="__ALL__", media_type="all", ai_status="all", q="", sort="mtime_desc", page=1, page_size=10)
        args.update(changes)
        return self.app.api_images(**args)

    def ids(self, **changes):
        return [i["id"] for i in self.images(**changes)["items"]]

    def test_include_intersection_exclusion_and_unknown_tags(self):
        self.assertEqual(self.ids(include_tags="artistA"), [1,2,3,5])
        self.assertEqual(self.ids(include_tags="artistA,tagB"), [2,3])
        self.assertEqual(self.ids(include_tags="artistA,tagB", exclude_tags="tagC"), [2])
        self.assertEqual(self.ids(include_tags="unknown"), [])
        self.assertEqual(self.ids(include_tags="artistA", exclude_tags="unknown"), [1,2,3,5])
        self.assertEqual(self.ids(include_tags="artistA", exclude_tags="artistA"), [])
        self.assertEqual(self.ids(include_tags="artistA", exclude_tags="tagB,tagC"), [1,5])

    def test_filters_search_sort_and_pagination_remain_composable(self):
        self.assertEqual(self.ids(include_tags="artistA", q="畫師甲"), [1,2,3,5])
        self.assertEqual(self.ids(include_tags="artistA", folder="__ROOT__"), [])
        self.assertEqual(self.ids(include_tags="tagB", media_type="gif_video"), [6])
        self.assertEqual(self.ids(include_tags="artistA", ai_status="untagged"), [2])
        self.assertEqual(self.ids(include_tags="artistA", sort="size_desc"), [5,3,2,1])
        self.assertEqual(self.ids(include_tags="artistA", sort="mtime_asc"), [5,3,2,1])
        self.assertEqual(self.ids(include_tags="artistA", sort="natural_asc"), [5,3,2,1])
        all_items = self.images(sort="natural_asc", page_size=20)
        paged = self.images(sort="natural_asc", page=1)["items"] + self.images(sort="natural_asc", page=2)["items"]
        self.assertEqual([i["id"] for i in paged], [i["id"] for i in all_items["items"]])
        self.assertEqual(self.images(page=100)["items"], [])
        self.assertEqual(self.ids(include_tags="最近,artistA", sort="size_desc"), [1,2,3,5])

    def test_exact_tag_query_uses_tag_index_without_correlated_subquery(self):
        statements = []
        def connect():
            c = database.get_db()
            c.set_trace_callback(statements.append)
            return c
        with patch.object(self.app, "get_db", connect):
            self.images(include_tags="artistA", sort="natural_asc")
        sql = next(s for s in statements if "FROM images i" in s)
        c = database.get_db()
        try:
            plan = " ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql))
        finally:
            c.close()
        self.assertIn("idx_image_tags_tag", plan)
        self.assertNotIn("CORRELATED", plan)
        self.assertIn("INTEGER PRIMARY KEY", plan)
        self.assertEqual(self.prefetch_mock.call_count, 1)

    def test_busy_batch_start_does_not_start_a_second_thread(self):
        from ai_tagger import AITaggerEngine
        from reverse_search import BatchReverseSearchWorker
        lease = work.acquire_job("other process")
        try:
            self.assertEqual(AITaggerEngine().start_batch_tagging()["status"], "already_running")
            self.assertEqual(BatchReverseSearchWorker().start()["status"], "already_running")
            self.assertEqual(self.scheduler.AutoIngestScheduler().trigger_pipeline_async()["status"], "already_running")
        finally:
            lease.release()

    def test_image_query_failure_closes_connection(self):
        connection = unittest.mock.Mock()
        connection.execute.side_effect = sqlite3.OperationalError("query failed")
        with patch.object(self.app, "get_db", return_value=connection):
            with self.assertRaises(sqlite3.OperationalError):
                self.images(include_tags="artistA")
        connection.close.assert_called_once()

    def test_semantic_batches_are_idempotent_and_update_only_their_counts(self):
        semantic = importlib.import_module("semantic_engine")
        with patch.object(semantic, "SEMANTIC_THEME_CLUSTERS", {"test-theme": {"core_tags": ["tagB"]}}):
            first = semantic.apply_semantic_theme_tags_batch()
            second = semantic.apply_semantic_theme_tags_batch()
        self.assertEqual(first["theme_links_added"], 3)
        self.assertEqual(second["theme_links_added"], 0)
        c = database.get_db()
        try:
            self.assertEqual(c.execute("SELECT post_count FROM tags WHERE name='test-theme'").fetchone()[0], 3)
        finally:
            c.close()

    def test_scheduler_same_slot_runs_pipeline_once_and_releases_on_error(self):
        engine = self.scheduler.AutoIngestScheduler()
        # Exercise the real wrapper and slot claim, replace only destructive work.
        def stage(reason, slot):
            if not work.claim_schedule_slot(slot):
                return "duplicate"
            return "executed"
        with patch.object(engine, "_execute_pipeline_owned", side_effect=stage):
            self.assertEqual(engine._execute_pipeline("timer", "test-slot"), "executed")
            self.assertEqual(engine._execute_pipeline("windows", "test-slot"), "duplicate")
        with patch.object(engine, "_execute_pipeline_owned", side_effect=RuntimeError("failed")):
            engine.running_job = True
            with self.assertRaises(RuntimeError):
                engine._execute_pipeline("manual")
        self.assertFalse(engine.running_job)
        self.assertEqual(work.background_status(), {})


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cache_dir = self.root / "thumbs"
        self.patch = patch.object(database, "DATA_DIR", self.root / "data")
        self.patch.start()
        self.db = self.root / "test.db"
        c = self.connect()
        c.execute("CREATE TABLE images(id INTEGER PRIMARY KEY, rel_path TEXT, media_type TEXT)")
        c.executemany("INSERT INTO images VALUES(?,?,?)", [(1,"source.png","image"),(2,"video.mp4","video"),(3,"bad.png","image")])
        c.commit(); c.close()
        Image.new("RGBA", (900,600), (255,0,0,128)).save(self.root / "source.png")
        (self.root / "video.mp4").write_bytes(b"video")
        (self.root / "bad.png").write_bytes(b"not an image")
        self.cache = ThumbnailCache(self.root, self.cache_dir, self.connect, max_pending=4)

    def connect(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def tearDown(self):
        self.cache.close()
        self.patch.stop()
        self.tmp.cleanup()

    def test_single_flight_produces_complete_small_jpeg_and_warm_hit(self):
        entered, release = threading.Event(), threading.Event()
        original = self.cache._generate
        def blocked(job):
            entered.set(); release.wait(3)
            return original(job)
        with patch.object(self.cache, "_generate", side_effect=blocked) as generate:
            first = self.cache.submit(1)
            self.assertTrue(entered.wait(2))
            others = [self.cache.submit(1) for _ in range(20)]
            self.assertTrue(all(f is first for f in others))
            release.set()
            result = first.result(timeout=3)
            self.assertEqual(generate.call_count, 1)
        with Image.open(result.path) as image:
            self.assertEqual(image.mode, "RGB")
            self.assertLessEqual(max(image.size), 380)
            image.verify()
        self.assertEqual(self.cache.cached(1).etag, result.etag)
        self.assertFalse(list(self.cache_dir.glob("*.tmp")))

    def test_source_edit_invalidates_thumbnail_and_bad_files_never_return_original(self):
        first = self.cache.submit(1).result(timeout=3)
        Image.new("RGB", (100,100), "blue").save(self.root / "source.png")
        self.assertIsNone(self.cache.cached(1))
        second = self.cache.submit(1).result(timeout=3)
        self.assertNotEqual(first.etag, second.etag)
        with self.assertRaises(OSError):
            self.cache.submit(3).result(timeout=3)
        self.assertFalse(list(self.cache_dir.glob("3-*.jpg")))
        self.assertFalse(list(self.cache_dir.glob("*.tmp")))

    def test_cache_is_shared_across_instances_and_legacy_jpeg_is_reused(self):
        self.cache_dir.mkdir()
        legacy = self.cache_dir / "1.jpg"
        Image.new("RGB", (100,80), "green").save(legacy)
        source_stat = (self.root / "source.png").stat()
        os.utime(legacy, ns=(source_stat.st_mtime_ns+1000000, source_stat.st_mtime_ns+1000000))
        with patch("thumbnail_cache.ImageOps.exif_transpose", side_effect=AssertionError("must reuse legacy")):
            first = self.cache.submit(1).result(timeout=3)
        second_cache = ThumbnailCache(self.root, self.cache_dir, self.connect)
        try:
            self.assertEqual(second_cache.cached(1).path, first.path)
        finally:
            second_cache.close()

    def test_video_missing_image_and_queue_limits(self):
        self.assertTrue(self.cache.submit(2).result(timeout=3).video)
        with self.assertRaises(FileNotFoundError):
            self.cache.submit(999).result(timeout=3)
        release = threading.Event()
        with patch.object(self.cache, "_generate", side_effect=lambda job: (release.wait(3), Thumbnail(None, '"test"'))[1]):
            futures = [self.cache.submit(i) for i in range(10,14)]
            with self.assertRaises(ThumbnailQueueFull):
                self.cache.submit(14)
            release.set()
            for future in futures:
                future.result(timeout=3)

    def test_http_conditional_cache_and_disconnected_client_keep_shared_work(self):
        # Import without running startup; GalleryTests has already isolated app paths.
        app = importlib.import_module("app")
        from starlette.requests import Request
        async def scenario():
            request = Request({"type":"http", "headers":[]})
            with patch.object(app, "thumbnail_cache", self.cache):
                response = await app.api_thumb(1, request)
                self.assertEqual(response.media_type, "image/jpeg")
                etag = response.headers["etag"]
                conditional = Request({"type":"http", "headers":[(b"if-none-match", etag.encode())]})
                response = await app.api_thumb(1, conditional)
                self.assertEqual(response.status_code, 304)
            shared = concurrent.futures.Future()
            with patch.object(app.thumbnail_cache, "cached", return_value=None), patch.object(app.thumbnail_cache, "submit", return_value=shared):
                task = asyncio.create_task(app.api_thumb(1, request))
                await asyncio.sleep(0.05)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(shared.cancelled())
                shared.set_result(Thumbnail(self.root / "source.png", '"finished"'))
                await asyncio.sleep(0)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
