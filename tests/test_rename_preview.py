"""Preview integration tests; no real gallery, model, scheduler, or media changes."""

import ast
import asyncio
import importlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from fastapi import FastAPI, Query

import database
from sqlite_pool import SQLiteConnectionPool


# Importing planning code must not create anything in the real gallery.
with patch.object(Path, "mkdir"):
    renamer = importlib.import_module("renamer")


class RenamePreviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "gallery.db"
        self.queries = []
        self.created = []
        self.reading = False
        with sqlite3.connect(str(self.path)) as conn:
            conn.execute("CREATE TABLE images (id INTEGER PRIMARY KEY, rel_path TEXT, "
                         "filename TEXT, folder TEXT, ext TEXT, mtime REAL)")
            samples = [
                (1, "A/img_10.jpg", "img_10.jpg", "A", ".jpg", 1700000000),
                (2, "A/img_2.jpg", "img_2.jpg", "A", ".jpg", 1700000000),
                (3, "B/new_2.jpg", "new_2.jpg", "B", ".jpg", 1700000000),
                (4, "B/20230101_0002_B.jpg", "20230101_0002_B.jpg", "B", ".jpg", 1700000000),
                (5, "B/20230101_0001_B.jpg", "20230101_0001_B.jpg", "B", ".jpg", 1700000000),
                (6, "20200101_foo.jpg", "20200101_foo.jpg", "", ".jpg", 1700000000),
                (7, "1/theme.jpg", "theme.jpg", "1", ".jpg", 1700000000),
                (8, "巢狀/part 2/img_10.png", "img_10.png", "巢狀", ".png", 1700000000),
                (9, "巢狀/part 2/img_2.png", "img_2.png", "巢狀", ".png", 1700000000),
            ]
            conn.executemany("INSERT INTO images VALUES (?, ?, ?, ?, ?, ?)", samples)

        def factory():
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.set_trace_callback(self.queries.append)
            self.created.append(conn)
            return conn

        self.pool = SQLiteConnectionPool(factory, max_size=1, checkout_timeout=0.1)
        self.addCleanup(self.pool.close)

        @contextmanager
        def preview_db():
            with self.pool.connection() as conn:
                self.reading = True
                try:
                    yield conn
                finally:
                    self.reading = False

        self.preview_patch = patch.object(renamer, "get_preview_db", preview_db)
        self.preview_patch.start()
        self.addCleanup(self.preview_patch.stop)

    def selects(self):
        return [query for query in self.queries if query.lstrip().upper().startswith("SELECT")]

    def test_all_folders_fetch_once_and_return_lease_before_planning(self):
        original = renamer.generate_rename_plan_from_rows

        def check_returned(*args, **kwargs):
            self.assertFalse(self.reading)
            # A size-one pool would time out here if planning still owned its lease.
            with self.pool.connection():
                pass
            return original(*args, **kwargs)

        with patch.object(renamer, "generate_rename_plan_from_rows", side_effect=check_returned):
            result = renamer.generate_rename_preview("__ALL_ARTIST_FOLDERS__")
        self.assertEqual(len(self.selects()), 1)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(result["folder_count"], 3)
        self.assertEqual(result["total"], 7)
        self.assertEqual(result["changed_count"], 5)
        a, b, nested = result["folders"]
        self.assertEqual(a["sample_first_old"], "img_2.jpg")
        self.assertEqual(a["sample_last_old"], "img_10.jpg")
        self.assertEqual(b["sample_first_old"], "20230101_0001_B.jpg")
        self.assertEqual(b["sample_last_old"], "new_2.jpg")
        self.assertEqual(b["sample_last_new"], "20230101_0003_B.jpg")
        self.assertEqual(nested["folder"], "巢狀")

    def test_single_folder_options_keep_numbered_files_ahead_of_new_files(self):
        result = renamer.generate_rename_preview("B", 3, "20261020", "新畫師")
        self.assertEqual(len(self.selects()), 1)
        self.assertEqual(result["base_date"], "20261020")
        self.assertEqual([item["old_filename"] for item in result["items"]], [
            "20230101_0001_B.jpg", "20230101_0002_B.jpg", "new_2.jpg"
        ])
        self.assertEqual([item["new_filename"] for item in result["items"]], [
            "20261020_001_新畫師.jpg", "20261020_002_新畫師.jpg", "20261020_003_新畫師.jpg"
        ])

    def test_root_and_empty_folder_responses_preserve_the_api_contract(self):
        root = renamer.generate_rename_preview("__ROOT__")
        self.assertEqual(root["items"][0]["new_filename"], "20200101_00001.jpg")
        self.assertEqual(root["base_date"], "依檔案日期")
        empty = renamer.generate_rename_preview("missing")
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["total"], 0)
        self.assertEqual(empty["base_date"], "")
        self.assertEqual(len(self.selects()), 2)
        self.assertEqual(len(self.created), 1)

    def test_single_preview_keeps_total_counts_but_limits_rows_to_300(self):
        with sqlite3.connect(str(self.path)) as conn:
            conn.executemany("INSERT INTO images VALUES (?, ?, ?, ?, ?, ?)", [
                (100 + i, f"Large/img_{i}.jpg", f"img_{i}.jpg", "Large", ".jpg", 1700000000)
                for i in range(305)
            ])
        result = renamer.generate_rename_preview("Large")
        self.assertEqual(result["total"], 305)
        self.assertEqual(result["changed_count"], 305)
        self.assertEqual(len(result["items"]), 300)
        self.assertEqual(len(self.selects()), 1)

    def test_query_exception_returns_connection_for_the_next_preview(self):
        with patch.object(renamer, "get_preview_db", self.pool.connection):
            with self.pool.connection() as conn:
                conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY
                                    if action == sqlite3.SQLITE_SELECT else sqlite3.SQLITE_OK)
            with self.assertRaises(sqlite3.DatabaseError):
                renamer.generate_rename_preview("A")
            with self.pool.connection() as conn:
                conn.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
            result = renamer.generate_rename_preview("A")
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(self.created), 1)

    def test_planning_exception_cannot_keep_the_connection_borrowed(self):
        with patch.object(renamer, "generate_rename_plan_from_rows", side_effect=ValueError("plan failed")):
            with self.assertRaisesRegex(ValueError, "plan failed"):
                renamer.generate_rename_preview("A")
        self.assertEqual(renamer.generate_rename_preview("A")["total"], 2)
        self.assertEqual(len(self.created), 1)

    def test_preview_connection_cannot_modify_the_database(self):
        with self.pool.connection() as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM images")
        self.assertEqual(renamer.generate_rename_preview("__ROOT__")["total"], 1)

    def test_http_route_returns_the_existing_json_contract(self):
        # Load the real route without starting models, schedulers, or other chats' work.
        source = Path(__file__).resolve().parents[1] / "app.py"
        route = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                     if isinstance(node, ast.FunctionDef) and node.name == "api_rename_preview")
        route.decorator_list = []
        namespace = {"Optional": Optional, "Query": Query,
                     "generate_rename_preview": renamer.generate_rename_preview}
        exec(compile(ast.Module(body=[route], type_ignores=[]), str(source), "exec"), namespace)
        app = FastAPI()
        app.get("/api/rename/preview")(namespace["api_rename_preview"])

        async def request():
            messages = []
            completed = asyncio.Event()
            delivered = False

            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await completed.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)
                if message["type"] == "http.response.body" and not message.get("more_body", False):
                    completed.set()

            await asyncio.wait_for(app({"type": "http", "http_version": "1.1", "method": "GET",
                       "asgi": {"version": "3.0", "spec_version": "2.4"},
                       "scheme": "http", "path": "/api/rename/preview", "root_path": "",
                       "query_string": b"folder=__ALL_ARTIST_FOLDERS__&pad_digits=4",
                       "headers": [], "server": ("test", 80), "client": ("test", 1)},
                      receive, send), timeout=3)
            return messages

        messages = asyncio.run(request())
        self.assertEqual(messages[0]["status"], 200)
        result = json.loads(b"".join(message.get("body", b"") for message in messages))
        self.assertEqual(result["mode"], "all_folders")
        self.assertEqual(result["folder_count"], 3)
        self.assertEqual(result["total"], 7)
        self.assertEqual(len(self.selects()), 1)


if __name__ == "__main__":
    unittest.main()
