"""Deletion exercises real files and SQLite only inside temporary galleries."""
import sqlite3
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import database
import gallery_deletion
from gallery_deletion import delete_images, recycle_files
from management_api import router
from test_reliability import GalleryCase


class ImageDeletionTests(GalleryCase):
    def setUp(self):
        super().setUp()
        self.recycled = []

        def fake_recycle(paths):
            for p in paths:
                target = Path(p)
                self.recycled.append(target)
                target.unlink()

        self.recycle_mock = self.stack.enter_context(
            patch.object(gallery_deletion, "recycle_files", side_effect=fake_recycle)
        )

    def prepare(self):
        self.image("畫師/圖片.png")
        self.image("other.png")
        database.scan_library()
        rows = self.rows("SELECT * FROM images ORDER BY id")
        for row in rows:
            self.manual(row["id"])
        return rows

    def test_batch_delete_moves_files_to_recycle_bin_and_removes_ids_tags_and_processing(self):
        rows = self.prepare()
        ids = [row["id"] for row in rows]
        conn = database.get_db()
        with conn:
            conn.execute("UPDATE tags SET zh_name='手動標籤' WHERE name='keep-manual'")
            conn.execute(
                "INSERT INTO image_processing(image_id,stage,status,updated_at) VALUES(?,'ai','succeeded',123)",
                (ids[0],),
            )
        conn.close()
        result = delete_images(ids + ids)
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["recycled"], 2)
        self.assertEqual(result["missing_count"], 0)
        self.assertFalse(self.rows("SELECT * FROM images"))
        self.assertFalse(self.rows("SELECT * FROM image_tags"))
        self.assertFalse(self.rows("SELECT * FROM image_processing"))
        self.assertEqual(self.rows("SELECT post_count FROM tags WHERE name='keep-manual'"), [{"post_count": 0}])
        for row in rows:
            self.assertFalse((self.root / row["rel_path"]).exists())
        self.assertEqual(len(self.recycled), 2)

    def test_recycle_files_calls_windows_shell_and_surfaces_errors(self):
        sample = self.image("recycle_me.png")
        calls = []

        def fake_shfileop(op):
            calls.append(op)
            return (0, False)

        with patch("win32com.shell.shell.SHFileOperation", side_effect=fake_shfileop):
            recycle_files([sample])
        self.assertEqual(len(calls), 1)
        hwnd, func, joined, to, flags, _, _ = calls[0]
        self.assertEqual(func, 3)  # FO_DELETE
        self.assertEqual(joined, str(sample.resolve()))
        self.assertEqual(flags, 0x0040 | 0x0010 | 0x0400 | 0x0004)

        with patch("win32com.shell.shell.SHFileOperation", return_value=(32, False)):
            with self.assertRaises(OSError):
                recycle_files([sample])

        with patch("win32com.shell.shell.SHFileOperation", return_value=(0, True)):
            with self.assertRaises(OSError):
                recycle_files([sample])

    def test_unknown_id_leaves_entire_batch_untouched(self):
        rows = self.prepare()
        ids = [row["id"] for row in rows]
        with self.assertRaises(ValueError):
            delete_images(ids + [99999])
        self.assertTrue(all((self.root / row["rel_path"]).exists() for row in rows))
        self.assertEqual(len(self.rows("SELECT id FROM images")), 2)
        self.assertEqual(self.recycled, [])

    def test_missing_file_deletes_stale_index_and_links_without_recycling(self):
        rows = self.prepare()
        iid = rows[0]["id"]
        (self.root / rows[0]["rel_path"]).unlink()
        result = delete_images([iid])
        self.assertEqual(result["deleted_ids"], [iid])
        self.assertEqual(result["recycled"], 0)
        self.assertEqual(result["missing_count"], 1)
        self.assertFalse(self.rows("SELECT id FROM images WHERE id=?", iid))
        self.assertFalse(self.rows("SELECT * FROM image_tags WHERE image_id=?", iid))
        self.assertEqual(len(self.rows("SELECT id FROM images")), 1)
        self.assertEqual(self.recycled, [])

    def test_mixed_batch_removes_all_indices_and_recycles_only_existing_files(self):
        rows = self.prepare()
        (self.root / rows[0]["rel_path"]).unlink()
        result = delete_images([row["id"] for row in rows])
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["recycled"], 1)
        self.assertEqual(result["missing_count"], 1)
        self.assertFalse(self.rows("SELECT id FROM images"))
        self.assertEqual(len(self.recycled), 1)

    def test_delete_refreshes_cached_lists_in_place_even_if_wal_signature_is_unchanged(self):
        from gallery_query import search_images

        rows = self.prepare()
        with patch("query_cache.database_version", return_value=(str(self.root), "unchanged")):
            self.assertEqual(search_images()["total"], 2)
            delete_images([rows[0]["id"]])
            remaining = search_images()
            self.assertEqual(remaining["total"], 1)
            self.assertEqual([item["id"] for item in remaining["items"]], [rows[1]["id"]])

    def test_missing_index_cleanup_rolls_back_on_database_error(self):
        rows = self.prepare()
        (self.root / rows[0]["rel_path"]).unlink()
        with patch.object(database, "refresh_tag_counts", side_effect=sqlite3.OperationalError("write failed")):
            with self.assertRaises(sqlite3.OperationalError):
                delete_images([row["id"] for row in rows])
        self.assertEqual(self.rows("SELECT * FROM images ORDER BY id"), rows)
        self.assertTrue((self.root / rows[1]["rel_path"]).is_file())
        self.assertTrue(self.rows("SELECT * FROM image_tags"))
        self.assertEqual(self.recycled, [])

    def test_delete_filesystem_failure_rolls_back_entire_batch(self):
        rows = self.prepare()
        with patch.object(gallery_deletion, "recycle_files", side_effect=PermissionError("file in use")):
            with self.assertRaises(PermissionError):
                delete_images([row["id"] for row in rows])
        self.assertEqual(self.rows("SELECT * FROM images ORDER BY id"), rows)
        self.assertTrue(all((self.root / row["rel_path"]).exists() for row in rows))

    def test_delete_database_failure_does_not_recycle_files_and_rolls_back_metadata(self):
        rows = self.prepare()
        with patch.object(database, "refresh_tag_counts", side_effect=sqlite3.OperationalError("write failed")):
            with self.assertRaises(sqlite3.OperationalError):
                delete_images([row["id"] for row in rows])
        self.assertEqual(self.rows("SELECT * FROM images ORDER BY id"), rows)
        self.assertTrue(self.rows("SELECT * FROM image_tags"))
        self.assertTrue(all((self.root / row["rel_path"]).exists() for row in rows))
        self.assertEqual(self.recycled, [])

    def test_paths_cannot_escape_gallery_or_target_system_files(self):
        rows = self.prepare()
        conn = database.get_db()
        for relative in ("../outside.png", ".tag_gallery/gallery.db"):
            with conn:
                conn.execute("UPDATE images SET rel_path=? WHERE id=?", (relative, rows[0]["id"]))
            with self.assertRaises(ValueError):
                delete_images([rows[0]["id"]])
        conn.close()

    def test_api_validates_ids_and_executes_single_delete(self):
        rows = self.prepare()
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            for ids in ([], [True], [0], [-1], ["1"]):
                self.assertEqual(client.post("/api/images/delete", json={"image_ids": ids}).status_code, 422)
            gone = client.post("/api/images/delete", json={"image_ids": [99999]})
            self.assertEqual(gone.status_code, 200)
            self.assertTrue(gone.json()["already_deleted"])
            response = client.post("/api/images/delete", json={"image_ids": [rows[0]["id"]]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["deleted"], 1)
            self.assertEqual(response.json()["recycled"], 1)

    def test_repeating_single_delete_advances_a_stale_view_without_touching_other_images(self):
        rows = self.prepare()
        delete_images([rows[0]["id"]])
        repeated = delete_images([rows[0]["id"]])
        self.assertTrue(repeated["already_deleted"])
        self.assertEqual(repeated["deleted_ids"], [rows[0]["id"]])
        self.assertEqual(repeated["recycled"], 0)
        self.assertEqual(self.rows("SELECT id FROM images"), [{"id": rows[1]["id"]}])
