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

    def test_sidebar_pages_include_special_tags_once_and_match_legacy_order(self):
        for category in ("all", "recent", "general", "artist"):
            expected = self.app.api_tags(category=category, q="", limit=500)
            actual = []
            offset = 0
            for _ in range(20):
                page = self.app.api_tags(category=category, q="", limit=2, offset=offset, paged=True)
                self.assertLessEqual(len(page["items"]), 2)
                self.assertEqual(page["next_offset"], offset + len(page["items"]))
                actual.extend(page["items"])
                offset = page["next_offset"]
                if not page["has_more"]:
                    break
            else:
                self.fail("Tag pagination did not terminate")
            self.assertEqual(actual, expected)
            self.assertEqual(len({tag["name"] for tag in actual}), len(actual))

    def test_sidebar_pages_merge_artist_aliases_before_slicing_and_search_all_tags(self):
        c = database.get_db()
        with c:
            c.execute("INSERT INTO tags(id,name,category,zh_name) VALUES(4,'artistA_alias','artist','畫師甲')")
            c.execute("INSERT INTO image_tags(image_id,tag_id) VALUES(4,4)")
            c.executemany("INSERT INTO tags(id,name,category,post_count) VALUES(?,?,'general',1)",
                          [(i, f"lazy-tag-{i:03}") for i in range(10, 105)])
            c.execute("UPDATE tags SET post_count=1 WHERE id=4")
        c.close()
        artist = self.app.api_tags(category="artist", q="", limit=1, paged=True)
        self.assertFalse(artist["has_more"])
        self.assertEqual(set(artist["items"][0]["aliases"]), {"artistA", "artistA_alias"})
        first = self.app.api_tags(category="general", q="", limit=40, paged=True)
        second = self.app.api_tags(category="general", q="", limit=40, offset=40, paged=True)
        last = self.app.api_tags(category="general", q="", limit=40, offset=80, paged=True)
        self.assertEqual([len(p["items"]) for p in (first, second, last)], [40, 40, 17])
        self.assertTrue(first["has_more"])
        self.assertFalse(last["has_more"])
        names = [tag["name"] for p in (first, second, last) for tag in p["items"]]
        self.assertEqual(len(names), len(set(names)))
        searched = self.app.api_tags(category="general", q="lazy-tag-104", limit=40, paged=True)
        self.assertEqual([t["name"] for t in searched["items"]], ["lazy-tag-104"])
        empty = self.app.api_tags(category="all", q="no-such-tag", limit=40, paged=True)
        self.assertEqual(empty, {"items": [], "next_offset": 0, "has_more": False})

    def test_artist_sidebar_batches_alias_counts_and_hits_cache_across_requests(self):
        from query_cache import invalidate_gallery_queries
        c = database.get_db()
        with c:
            c.execute("INSERT INTO tags(id,name,category,zh_name,post_count) VALUES(4,'artistA_alias','artist','畫師甲',1)")
            c.execute("INSERT INTO image_tags(image_id,tag_id) VALUES(4,4)")
            cols = [r[2] for r in c.execute("PRAGMA index_info(idx_image_tags_tag)")]
        c.close()
        self.assertEqual(cols, ["tag_id", "image_id"])
        invalidate_gallery_queries()
        statements = []
        def connect():
            conn = database.get_db()
            conn.set_trace_callback(statements.append)
            return conn
        with patch.object(self.app, "get_db", connect):
            first = self.app.api_tags(category="artist", q="", limit=40, paged=True)
            first_sql_count = len(statements)
            second = self.app.api_tags(category="artist", q="", limit=40, paged=True)
        self.assertEqual(first, second)
        self.assertEqual(len(statements), first_sql_count, "Warm artist sidebar request should hit QueryCache without SQL queries")

    def test_sidebar_page_parameters_are_validated_by_http(self):
        from fastapi.testclient import TestClient
        client = TestClient(self.app.app)
        for params in ({"offset": -1}, {"limit": 0}, {"limit": "invalid"}):
            self.assertEqual(client.get("/api/tags", params={"paged": True, **params}).status_code, 422)

    def test_compact_cards_keep_one_artist_rating_and_full_tag_count(self):
        c = database.get_db()
        with c:
            c.execute("UPDATE image_tags SET confidence=0.4 WHERE image_id=1")
            c.executemany("INSERT INTO tags(id,name,category) VALUES(?,?,?)", [
                (4, "top-artist", "artist"), (5, "tied-artist", "artist"),
                (6, "explicit", "rating"), (7, "safe", "rating"),
                (8, "hidden-character", "character"), (9, "hidden-copyright", "copyright"),
            ])
            c.executemany("INSERT INTO image_tags(image_id,tag_id,confidence) VALUES(1,?,?)", [
                (4, 0.95), (5, 0.95), (6, 0.9), (7, 0.6), (8, 0.8), (9, 0.8), (2, 0.7), (3, 0.7),
            ])
        c.close()
        card = self.images(compact=True)["items"][0]
        self.assertEqual([t["name"] for t in card["artists"]], ["top-artist"])
        self.assertEqual(card["rating"], "explicit")
        self.assertEqual(card["total_tags"], 9)
        for category in ("characters", "copyrights", "preview_tags"):
            self.assertEqual(card[category], [])
        full = self.images()["items"][0]
        self.assertEqual(len(full["artists"]), 3)
        self.assertEqual(full["characters"][0]["name"], "hidden-character")
        self.assertEqual(len(self.app.api_image_detail(1)["tags"]), 9)

    def test_compact_display_keeps_hidden_tag_filters_search_and_empty_images(self):
        options = dict(include_tags="tagB", exclude_tags="tagC", sort="natural_asc")
        self.assertEqual(self.ids(compact=True, **options), self.ids(**options))
        self.assertEqual(self.ids(compact=True, q="條件乙"), self.ids(q="條件乙"))
        empty = self.images(compact=True)["items"][9]
        self.assertEqual(empty["total_tags"], 0)
        self.assertEqual(empty["artists"], [])
        self.assertIsNone(empty["rating"])
        self.assertEqual(self.images(compact=True, page=100)["items"], [])

    def test_compact_tag_selection_reads_covering_keys_before_tag_payloads(self):
        statements = []
        def connect():
            c = database.get_db()
            c.set_trace_callback(statements.append)
            return c
        with patch.object(self.app, "get_db", connect):
            self.images(compact=True)
        sql = next(s for s in statements if "WITH card_links" in s)
        c = database.get_db()
        try:
            plan = " ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN " + sql))
        finally:
            c.close()
        self.assertIn("MATERIALIZE card_links", plan)
        self.assertIn("SEARCH it USING COVERING INDEX", plan)

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

    def test_generated_theme_batch_is_rejected_before_changing_counts(self):
        semantic = importlib.import_module("semantic_engine")
        before = self.images()["total"]
        with self.assertRaisesRegex(ValueError, "自動標籤"):
            semantic.apply_semantic_theme_tags_batch()
        self.assertEqual(self.images()["total"], before)
        c = database.get_db()
        try:
            self.assertIsNone(c.execute("SELECT id FROM tags WHERE name='test-theme'").fetchone())
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
        fallback = self.cache.submit(2).result(timeout=3)
        self.assertTrue(fallback.video)
        self.assertIsNone(self.cache.cached(2))
        with patch("thumbnail_cache._extract_video_thumbnail", return_value=Image.new("RGB", (320, 180), "purple")):
            extracted = self.cache.submit(2).result(timeout=3)
        self.assertFalse(extracted.video)
        self.assertTrue(extracted.path.is_file())
        self.assertNotEqual(fallback.etag, extracted.etag)
        self.assertEqual(self.cache.cached(2).path, extracted.path)
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
