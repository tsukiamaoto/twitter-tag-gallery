"""Published MCP contracts and observable defaults on disposable galleries."""
import inspect
import unittest
from unittest.mock import patch

import database
import gallery_tools
import tool_registry as registry
import tool_runtime as runtime
from test_reliability import GalleryCase


class SchemaContractTests(unittest.TestCase):
    def test_all_published_fields_explain_their_meaning_and_examples_bind(self):
        for tool in registry.CATALOG.values():
            with self.subTest(tool=tool.name):
                arguments = tool.validate(tool.example)
                inspect.signature(tool.handler()).bind(**arguments)
                schema = tool.schema()
                for model in [schema, *schema.get("$defs", {}).values()]:
                    for name, field in model.get("properties", {}).items():
                        self.assertTrue(field.get("description"), f"{tool.name}.{name} has no description")

    def test_artist_evidence_matches_the_conditional_published_schema(self):
        from jsonschema import Draft202012Validator
        tool = registry.CATALOG["gallery_submit_artist_identity"]
        valid = {"job_id": "0" * 32, "confirmed": True, "canonical_name": "artist",
                 "profile_urls": ["https://x.com/artist", "https://www.pixiv.net/users/123"],
                 "evidence": [{"source_url": "https://x.com/artist", "linked_url": "https://www.pixiv.net/users/123"}]}
        validator = Draft202012Validator(tool.schema())
        for value in (valid, {"job_id": "0" * 32, "confirmed": False}):
            validator.validate(value)
            tool.validate(value)
        invalid = [{"job_id": "0" * 32, "confirmed": True},
                   {**valid, "canonical_name": ""}, {**valid, "profile_urls": []},
                   {**valid, "evidence": []}, {**valid, "evidence": [{"url": "https://x.com/artist"}]},
                   {**valid, "evidence": [{**valid["evidence"][0], "unexpected": "value"}]}]
        for value in invalid:
            with self.subTest(value=value):
                self.assertTrue(list(validator.iter_errors(value)))
                with self.assertRaises(ValueError):
                    tool.validate(value)

    def test_schema_defaults_agree_with_direct_handler_defaults(self):
        for name in ("gallery_deep_reverse_search_batch", "gallery_purge_all_low_conf_ai_artists"):
            tool = registry.CATALOG[name]
            signature = inspect.signature(tool.handler())
            for field, value in tool.validate({}).items():
                self.assertEqual(signature.parameters[field].default, value, (name, field))
        calibration = registry.Calibration(image_id=1)
        self.assertFalse(calibration.purge_ai_artist_tags)
        self.assertFalse(calibration.move_to_artist_folder)


class ToolBehaviorContractTests(GalleryCase):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.object(gallery_tools, "GALLERY_ROOT", self.root))

    def scanned(self):
        self.image("a.png"); self.image("b.png")
        database.scan_library()
        return self.rows("SELECT id FROM images ORDER BY id")[0]["id"]

    def ai_artist(self, iid):
        conn = database.get_db()
        try:
            with conn:
                tid = database.ensure_tag(conn, {}, "guessed_artist", "artist", source="ai")
                conn.execute("INSERT INTO image_tags(image_id,tag_id,confidence,source) VALUES(?,?,0.3,'ai')", (iid, tid))
        finally:
            conn.close()

    def test_empty_batch_filters_rejected_before_reading_or_writing(self):
        for tags in ([], ["   "]):
            value = {"include_tags": tags, "add_artist_tag": "artist", "only_root_files": False}
            with self.subTest(tags=tags), self.assertRaises(ValueError):
                registry.CATALOG["gallery_batch_tag_or_move"].validate(value)
            with patch.object(gallery_tools, "get_db") as get_db:
                with self.assertRaises(ValueError):
                    gallery_tools.tool_gallery_batch_tag_or_move(**value)
                get_db.assert_not_called()

    def test_batch_tagging_only_updates_the_matched_image(self):
        iid = self.scanned()
        ids = [row["id"] for row in self.rows("SELECT id FROM images")]
        runtime.invoke("gallery_update_image_tags", {"image_ids": ids, "add_tags": [{"name": "selected", "category": "source"}]})
        runtime.invoke("gallery_update_image_tags", {"image_ids": [iid], "add_tags": [{"name": "required", "category": "copyright"}]})
        result = runtime.invoke("gallery_batch_tag_or_move", {"include_tags": ["selected", "required"], "add_artist_tag": "verified_artist"})
        self.assertEqual(result["matched_images"], 1)
        self.assertEqual(self.rows("SELECT it.image_id FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE t.name='verified_artist'"), [{"image_id": iid}])

    def test_character_only_calibration_preserves_ai_artist(self):
        iid = self.scanned(); self.ai_artist(iid); self.manual(iid)
        result = runtime.invoke("gallery_calibrate_image_artist_and_tags", {"calibrations": [{"image_id": iid, "characters": ["verified_character"]}]})
        self.assertEqual(result["purged_wrong_tags"], 0)
        self.assertTrue(self.rows("SELECT 1 FROM image_tags WHERE image_id=? AND source='ai'", iid))
        self.assertTrue(self.rows("SELECT 1 FROM image_tags WHERE image_id=? AND source='manual'", iid))

    def test_artist_calibration_only_purges_and_moves_when_explicitly_enabled(self):
        iid = self.scanned(); self.ai_artist(iid); self.manual(iid)
        calibration = {"image_id": iid, "verified_artist": "verified_artist"}
        with patch("artist_identity.resolve", return_value={"folder_name": "verified_artist"}), patch("gallery_service.move_images_to_artist_folder", return_value={"moved": 1}) as move:
            result = runtime.invoke("gallery_calibrate_image_artist_and_tags", {"calibrations": [calibration]})
            move.assert_not_called()
            self.assertEqual(result["purged_wrong_tags"], 0)
            self.assertTrue(self.rows("SELECT 1 FROM image_tags WHERE image_id=? AND source='ai'", iid))
            self.assertTrue((self.root / "a.png").exists())
            result = runtime.invoke("gallery_calibrate_image_artist_and_tags", {"calibrations": [{**calibration, "purge_ai_artist_tags": True, "move_to_artist_folder": True}]})
            self.assertEqual(result["purged_wrong_tags"], 1)
            move.assert_called_once_with([iid], "verified_artist", auto_rename=True)
        self.assertTrue(self.rows("SELECT 1 FROM image_tags WHERE image_id=? AND source='manual'", iid))

    def test_deep_reverse_only_moves_when_explicitly_enabled(self):
        iid = self.scanned()
        found = {"found": True, "artists": ["verified_artist"], "suggested_folder": "verified_artist"}
        with patch.object(gallery_tools, "reverse_search_and_tag_image", return_value=found), patch.object(gallery_tools.time, "sleep"), patch("gallery_service.move_images_to_artist_folder", return_value={"moved": 1}) as move:
            result = runtime.invoke("gallery_deep_reverse_search_batch", {"image_ids": [iid]})
            self.assertEqual(result["moved_to_correct_folder_count"], 0)
            move.assert_not_called()
            self.assertTrue((self.root / "a.png").exists())
            result = runtime.invoke("gallery_deep_reverse_search_batch", {"image_ids": [iid], "auto_move_verified": True})
            self.assertEqual(result["moved_to_correct_folder_count"], 1)
            move.assert_called_once_with([iid], "verified_artist", auto_rename=True)

    def test_purge_only_reverts_folders_when_explicitly_enabled(self):
        self.image("unverified/a.png")
        database.scan_library()
        iid = self.rows("SELECT id FROM images")[0]["id"]
        self.ai_artist(iid)
        conn = database.get_db()
        try:
            with conn:
                conn.execute("UPDATE images SET ai_tagged=1 WHERE id=?", (iid,))
        finally:
            conn.close()
        with patch.object(gallery_tools, "move_images_to_folder", return_value={"moved": 1}) as move:
            runtime.invoke("gallery_purge_all_low_conf_ai_artists", {})
            move.assert_not_called()
            self.assertTrue((self.root / "unverified/a.png").exists())
            runtime.invoke("gallery_purge_all_low_conf_ai_artists", {"revert_unverified_ai_folders": True})
            move.assert_called_once_with([iid], "__ROOT__", auto_rename=False)
