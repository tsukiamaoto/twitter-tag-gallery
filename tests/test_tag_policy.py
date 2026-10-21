"""Generated tags obey the same limits across model entry points."""
from unittest.mock import patch

import database
from gallery_tag_service import edit_tags
import gallery_workflow_tools as workflows
import tool_registry
from test_reliability import GalleryCase


class TagPolicyTests(GalleryCase):
    def scanned(self, name="image.png"):
        self.image(name)
        database.scan_library()
        return self.rows("SELECT id FROM images")[0]["id"]

    def test_automatic_guard_precedes_cache_and_preserves_manual_categories(self):
        conn = database.get_db()
        try:
            with conn:
                tid = database.ensure_tag(conn, {}, "long_hair", "general")
                cache = {"long_hair": tid}
                self.assertEqual(database.ensure_tag(conn, cache, "long_hair", "general", source="ai"), -1)
                self.assertEqual(database.ensure_tag(conn, cache, "long_hair", "general", source="gemini_mcp"), -1)
                self.assertEqual(database.ensure_tag(conn, cache, "long_hair", "general", source="manual"), tid)
                self.assertEqual(database.ensure_tag(conn, cache, "new_theme", "general", source="pixiv"), -1)
                self.assertIsNone(conn.execute("SELECT 1 FROM tags WHERE name='new_theme'").fetchone())
        finally:
            conn.close()

    def test_mcp_schemas_reject_old_detail_fields_and_manual_source_spoofing(self):
        requests = [
            ("gallery_update_image_tags", {"image_ids": [1], "add_tags": [{"name": "hair", "category": "general"}]}),
            ("gallery_update_image_tags", {"image_ids": [1], "add_tags": [{"name": "name", "category": "artist", "source": "manual"}]}),
            ("gallery_batch_tag_or_move", {"include_tags": [], "add_general_tags": ["hair"]}),
            ("gallery_calibrate_image_artist_and_tags", {"calibrations": [{"image_id": 1, "add_general_tags": ["hair"]}]}),
            ("gallery_apply_gemini_enrichments", {"auto_cluster_builtin_themes": True}),
            ("gallery_apply_gemini_enrichments", {"semantic_theme_groups": [{"theme_name": "theme", "member_tags": ["hair"]}]}),
        ]
        for name, arguments in requests:
            with self.subTest(name=name, arguments=arguments), self.assertRaises(ValueError):
                tool_registry.CATALOG[name].validate(arguments)

    def test_model_edits_are_not_manual_and_cannot_replace_or_remove_manual_tags(self):
        iid = self.scanned()
        edit_tags([iid], [{"name": "human_artist", "category": "artist"}, {"name": "my_note", "category": "general"}], [])
        workflows.tool_gallery_update_image_tags([iid], [{"name": "human_artist", "category": "artist"}, {"name": "model_character", "category": "character"}], ["my_note"])
        workflows.tool_gallery_update_image_tags([iid], [], ["human_artist"])
        rows = self.rows("SELECT t.name,it.source FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE it.image_id=?", iid)
        sources = {row["name"]: row["source"] for row in rows}
        self.assertEqual(sources["human_artist"], "manual")
        self.assertEqual(sources["my_note"], "manual")
        self.assertEqual(sources["model_character"], "gemini_mcp")
        with self.assertRaisesRegex(ValueError, "自動標籤"):
            workflows.tool_gallery_update_image_tags([iid], [{"name": "blocked", "category": "general"}], ["model_character"])
        self.assertTrue(self.rows("SELECT 1 FROM tags WHERE name='model_character' AND post_count=1"))

    def test_model_cannot_reclassify_a_manual_description_as_a_core_tag(self):
        iid = self.scanned()
        edit_tags([iid], [{"name": "my_note", "category": "general"}], [])
        with self.assertRaisesRegex(ValueError, "自動標籤"):
            workflows.tool_gallery_update_image_tags([iid], [{"name": "my_note", "category": "artist"}], [])
        self.assertEqual(self.rows("SELECT t.category,it.source FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE t.name='my_note'"),
                         [{"category": "general", "source": "manual"}])

    def test_direct_legacy_tools_reject_before_translation_or_calibration(self):
        import gallery_tools
        iid = self.scanned()
        edit_tags([iid], [{"name": "artist", "category": "artist"}], [])
        with self.assertRaisesRegex(ValueError, "自動標籤"):
            gallery_tools.tool_gallery_apply_gemini_enrichments({"artist": "changed"}, auto_cluster_builtin_themes=True)
        self.assertNotEqual(self.rows("SELECT zh_name FROM tags WHERE name='artist'")[0]["zh_name"], "changed")
        with self.assertRaisesRegex(ValueError, "自動標籤"):
            gallery_tools.tool_gallery_calibrate_image_artist_and_tags([
                {"image_id": iid, "verified_artist": "should_not_exist", "move_to_artist_folder": False},
                {"image_id": iid, "add_general_tags": ["blocked"]},
            ])
        self.assertFalse(self.rows("SELECT 1 FROM tags WHERE name='should_not_exist'"))
        result = gallery_tools.tool_gallery_apply_gemini_enrichments({"artist": "畫師"})
        self.assertEqual(result["custom_translations_updated"], 1)
        self.assertEqual(result["custom_theme_links_added"], 0)

    def test_local_analysis_filters_predictions_and_keeps_manual_notes(self):
        import ai_tagger
        from vision_service import analyze
        iid = self.scanned()
        self.manual(iid)
        predictions = [("hair", "general", .9), ("rating", "rating", .9),
                       ("character_name", "character", .9), ("work_name", "copyright", .9)]
        with patch.object(ai_tagger, "preprocess_image_fast", return_value=object()), patch.object(ai_tagger.tagger_engine, "predict_single", return_value=(predictions, None)):
            result = analyze(iid)
        self.assertEqual(result["tags_added"], 2)
        self.assertFalse(self.rows("SELECT 1 FROM tags WHERE name IN ('hair','rating')"))
        self.assertEqual(len(self.rows("SELECT 1 FROM image_tags WHERE source='manual'")), 1)
        self.assertEqual(self.rows("SELECT ai_tagged FROM images")[0]["ai_tagged"], 1)

    def test_predictor_outputs_only_core_categories_without_loading_a_model(self):
        import ai_tagger
        import numpy as np
        from types import SimpleNamespace
        engine = ai_tagger.AITaggerEngine()
        engine.model_loaded = True
        engine.input_name = "input"
        engine.session = SimpleNamespace(run=lambda *args: [np.array([[4.] * 7])])
        names = ["artist", "character", "work", "hair", "meta", "year", "rating"]
        categories = ["artist", "character", "copyright", "general", "meta", "year", "rating"]
        engine.idx_to_tag = {str(i): name for i, name in enumerate(names)}
        engine.tag_to_category = dict(zip(names, categories))
        engine.artist_indices = np.array([0])
        engine.rating_indices = np.array([6])
        predictions, _ = engine.predict_single(object())
        self.assertEqual({tag[1] for tag in predictions}, {"artist", "character", "copyright"})

    def test_scanning_does_not_regenerate_year_or_filename_details(self):
        self.scanned("20261021_001.png")
        self.assertFalse(self.rows("SELECT 1 FROM tags WHERE category IN ('general','year','rating')"))
        database.scan_library()
        self.assertFalse(self.rows("SELECT 1 FROM tags WHERE category IN ('general','year','rating')"))

    def test_reverse_search_discards_general_tags_but_keeps_core_and_manual_tags(self):
        import reverse_search
        iid = self.scanned()
        edit_tags([iid], [{"name": "character_name", "category": "character"}], [])
        with patch.object(reverse_search, "fetch_danbooru_artist_metadata", return_value=None):
            reverse_search.apply_danbooru_posts_batch_to_db({iid: {
                "generals": ["long_hair", "solo"], "characters": ["character_name"],
                "copyrights": ["work_name"], "artists": [], "rating": "g",
            }}, refresh_counts=True)
        self.assertFalse(self.rows("SELECT 1 FROM tags WHERE name IN ('long_hair','solo')"))
        self.assertTrue(self.rows("SELECT 1 FROM tags WHERE name='work_name' AND category='copyright'"))
        self.assertTrue(self.rows("SELECT 1 FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE t.name='character_name' AND it.source='manual'"))
