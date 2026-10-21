"""Deletion exercises real files and SQLite only inside temporary galleries."""
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import database
import file_operations
from gallery_deletion import delete_images, restore_deleted_images
from management_api import router
from test_reliability import GalleryCase


class ImageDeletionTests(GalleryCase):
    def prepare(self):
        self.image("畫師/圖片.png")
        self.image("other.png")
        database.scan_library()
        rows = self.rows("SELECT * FROM images ORDER BY id")
        for row in rows:
            self.manual(row["id"])
        return rows

    def test_batch_delete_and_restore_preserve_files_ids_tags_and_processing(self):
        rows = self.prepare()
        ids = [row["id"] for row in rows]
        conn = database.get_db()
        with conn:
            conn.execute("UPDATE tags SET zh_name='手動標籤' WHERE name='keep-manual'")
            conn.execute("INSERT INTO image_processing(image_id,stage,status,updated_at) VALUES(?,'ai','succeeded',123)", (ids[0],))
        tags = self.rows("SELECT * FROM image_tags ORDER BY image_id,tag_id")
        processing = self.rows("SELECT * FROM image_processing")
        conn.close()
        originals = {row["rel_path"]: (self.root / row["rel_path"]).read_bytes() for row in rows}
        result = delete_images(ids + ids)
        self.assertEqual(result["deleted"], 2)
        self.assertFalse(self.rows("SELECT * FROM images"))
        self.assertFalse(self.rows("SELECT * FROM image_tags"))
        self.assertFalse(self.rows("SELECT * FROM image_processing"))
        self.assertEqual(self.rows("SELECT post_count FROM tags WHERE name='keep-manual'"), [{"post_count": 0}])
        for relative in originals:
            self.assertFalse((self.root / relative).exists())
        self.assertEqual(database.scan_library()["total_images"], 0, "Scanning must skip the private trash")
        self.assertEqual(restore_deleted_images(result["operation_id"]), {"restored": 2})
        self.assertEqual(self.rows("SELECT * FROM images ORDER BY id"), rows)
        self.assertEqual(self.rows("SELECT * FROM image_tags ORDER BY image_id,tag_id"), tags)
        self.assertEqual(self.rows("SELECT * FROM image_processing"), processing)
        for relative, content in originals.items():
            self.assertEqual((self.root / relative).read_bytes(), content)
        self.assertEqual(self.rows("SELECT post_count FROM tags WHERE name='keep-manual'"), [{"post_count": 2}])
        with self.assertRaisesRegex(ValueError, "已經復原"):
            restore_deleted_images(result["operation_id"])

    def test_unknown_id_leaves_entire_batch_untouched(self):
        rows = self.prepare()
        ids = [row["id"] for row in rows]
        with self.assertRaises(ValueError):
            delete_images(ids + [99999])
        self.assertTrue(all((self.root / row["rel_path"]).exists() for row in rows))
        self.assertEqual(len(self.rows("SELECT id FROM images")), 2)

    def test_missing_file_deletes_stale_index_and_links_without_restoring_a_ghost(self):
        rows = self.prepare()
        iid = rows[0]["id"]
        (self.root / rows[0]["rel_path"]).unlink()
        result = delete_images([iid])
        self.assertEqual(result["deleted_ids"], [iid])
        self.assertEqual(result["restorable"], 0)
        self.assertEqual(result["missing_count"], 1)
        self.assertFalse(self.rows("SELECT id FROM images WHERE id=?", iid))
        self.assertFalse(self.rows("SELECT * FROM image_tags WHERE image_id=?", iid))
        with self.assertRaisesRegex(ValueError, "無可復原"):
            restore_deleted_images(result["operation_id"])
        self.assertEqual(len(self.rows("SELECT id FROM images")), 1)

    def test_mixed_batch_removes_all_indices_but_only_restores_existing_files(self):
        rows = self.prepare()
        (self.root / rows[0]["rel_path"]).unlink()
        result = delete_images([row["id"] for row in rows])
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["restorable"], 1)
        self.assertFalse(self.rows("SELECT id FROM images"))
        self.assertEqual(restore_deleted_images(result["operation_id"]), {"restored": 1})
        self.assertEqual(self.rows("SELECT id FROM images"), [{"id": rows[1]["id"]}])

    def test_delete_and_restore_refresh_cached_lists_even_if_wal_signature_is_unchanged(self):
        from gallery_query import search_images
        rows = self.prepare()
        with patch("query_cache.database_version", return_value=(str(self.root), "unchanged")):
            self.assertEqual(search_images()["total"], 2)
            result = delete_images([rows[0]["id"]])
            remaining = search_images()
            self.assertEqual(remaining["total"], 1)
            self.assertEqual([item["id"] for item in remaining["items"]], [rows[1]["id"]])
            restore_deleted_images(result["operation_id"])
            self.assertEqual(search_images()["total"], 2)

    def test_missing_index_cleanup_rolls_back_on_database_error(self):
        rows = self.prepare()
        (self.root / rows[0]["rel_path"]).unlink()
        with patch.object(database, "refresh_tag_counts", side_effect=sqlite3.OperationalError("write failed")):
            with self.assertRaises(sqlite3.OperationalError):
                delete_images([row["id"] for row in rows])
        self.assertEqual(self.rows("SELECT * FROM images ORDER BY id"), rows)
        self.assertTrue((self.root / rows[1]["rel_path"]).is_file())
        self.assertTrue(self.rows("SELECT * FROM image_tags"))

    def test_delete_filesystem_failure_rolls_back_entire_batch(self):
        rows = self.prepare()
        rename = os.rename
        def fail(source, destination):
            if Path(destination).name == Path(rows[1]["rel_path"]).name and "trash" in Path(destination).parts:
                raise PermissionError("file in use")
            return rename(source, destination)
        with patch.object(file_operations.os, "rename", side_effect=fail):
            with self.assertRaises(PermissionError):
                delete_images([row["id"] for row in rows])
        self.assertEqual(self.rows("SELECT * FROM images ORDER BY id"), rows)
        self.assertTrue(all((self.root / row["rel_path"]).exists() for row in rows))

    def test_delete_database_failure_rolls_back_files_and_metadata(self):
        rows = self.prepare()
        with patch.object(database, "refresh_tag_counts", side_effect=sqlite3.OperationalError("write failed")):
            with self.assertRaises(sqlite3.OperationalError):
                delete_images([row["id"] for row in rows])
        self.assertEqual(self.rows("SELECT * FROM images ORDER BY id"), rows)
        self.assertTrue(self.rows("SELECT * FROM image_tags"))
        self.assertTrue(all((self.root / row["rel_path"]).exists() for row in rows))

    def test_restore_collision_and_database_failure_preserve_deleted_batch(self):
        rows = self.prepare()
        result = delete_images([row["id"] for row in rows])
        original = self.root / rows[0]["rel_path"]
        original.write_bytes(b"new file")
        with self.assertRaises(FileExistsError):
            restore_deleted_images(result["operation_id"])
        self.assertEqual(original.read_bytes(), b"new file")
        original.unlink()
        with patch.object(database, "refresh_tag_counts", side_effect=sqlite3.OperationalError("write failed")):
            with self.assertRaises(sqlite3.OperationalError):
                restore_deleted_images(result["operation_id"])
        self.assertFalse(self.rows("SELECT id FROM images"))
        self.assertFalse(any((self.root / row["rel_path"]).exists() for row in rows))
        self.assertEqual(restore_deleted_images(result["operation_id"])["restored"], 2)

    def test_paths_cannot_escape_gallery_or_target_system_files(self):
        rows = self.prepare()
        conn = database.get_db()
        for relative in ("../outside.png", ".tag_gallery/gallery.db"):
            with conn:
                conn.execute("UPDATE images SET rel_path=? WHERE id=?", (relative, rows[0]["id"]))
            with self.assertRaises(ValueError):
                delete_images([rows[0]["id"]])
        conn.close()
        with self.assertRaises(ValueError):
            restore_deleted_images("../../gallery")

    def test_restore_rejects_modified_trash_file(self):
        rows = self.prepare()
        result = delete_images([rows[0]["id"]])
        plan = json.loads((self.data / "file_journals" / (result["operation_id"] + ".json")).read_text(encoding="utf-8"))
        (self.root / plan["entries"][0]["new_rel_path"]).write_bytes(b"replacement")
        with self.assertRaisesRegex(ValueError, "已變更"):
            restore_deleted_images(result["operation_id"])

    def test_api_validates_ids_and_executes_single_delete_and_restore(self):
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
            url = "/api/images/deletions/" + response.json()["operation_id"] + "/restore"
            self.assertEqual(client.post(url).json(), {"restored": 1})
            self.assertEqual(client.post(url).status_code, 400)

    def test_repeating_single_delete_advances_a_stale_view_without_touching_other_images(self):
        rows = self.prepare()
        result = delete_images([rows[0]["id"]])
        repeated = delete_images([rows[0]["id"]])
        self.assertTrue(repeated["already_deleted"])
        self.assertEqual(repeated["deleted_ids"], [rows[0]["id"]])
        self.assertEqual(repeated["restorable"], 0)
        self.assertEqual(self.rows("SELECT id FROM images"), [{"id": rows[1]["id"]}])
        self.assertEqual(restore_deleted_images(result["operation_id"])["restored"], 1)
