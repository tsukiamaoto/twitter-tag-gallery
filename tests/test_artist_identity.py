"""Identity matching and MCP confirmation are tested without external services."""
import json
from unittest.mock import Mock, patch

import database
import artist_identity as identities
from gallery_service import move_images_to_artist_folder, plan_image_moves
from test_reliability import GalleryCase

REAL_DANBOORU = identities._danbooru
PROFILES = ["https://www.pixiv.net/users/123", "https://x.com/art_handle", "https://art-fan.fanbox.cc"]
EVIDENCE = [{"source_url": PROFILES[0], "linked_url": PROFILES[1]},
            {"source_url": PROFILES[1], "linked_url": PROFILES[2]}]


class ArtistIdentityTests(GalleryCase):
    def save(self, name="artist_main", aliases=None, profiles=None, dbid=101):
        return identities.remember(name, aliases or ["日文畫師", "other_name"], profiles or PROFILES,
            source="danbooru", evidence={"artist_url": "https://danbooru.donmai.us/artists/101"}, danbooru_id=dbid)

    def scanned_artist(self, name):
        self.image("new.png")
        database.scan_library()
        conn = database.get_db()
        try:
            with conn:
                tid = database.ensure_tag(conn, {}, name, "artist")
                conn.execute("INSERT INTO image_tags(image_id,tag_id,source) VALUES(1,?,'pixiv')", (tid,))
                database.refresh_tag_counts(conn)
        finally:
            conn.close()
        return 1

    def test_profile_keys_normalize_platforms_but_not_display_names_or_artwork_ids(self):
        self.assertEqual(identities.profile_key("https://twitter.com/Art_Handle/status/111?s=1"), ("twitter", "art_handle"))
        self.assertEqual(identities.profile_key("https://www.fanbox.cc/@art-fan/posts/123"), ("fanbox", "art-fan"))
        self.assertEqual(identities.profile_key("https://www.pixiv.net/en/users/00123"), ("pixiv", "123"))
        self.assertEqual(identities.profile_key("https://twitter.com/intent/user?user_id=456"), ("twitter_id", "456"))
        self.assertEqual(identities.profile_key("https://pixiv.net/member.php?id=123"), ("pixiv", "123"))
        for bad in ("https://evil.test/art_handle", "https://x.com@evil.test/handle", "https://x.com:999/handle", "https://pixiv.net/artworks/123"):
            self.assertIsNone(identities.profile_key(bad))

    def test_remembers_all_aliases_and_platform_handles_across_reloads(self):
        row = self.save()
        identities._index.clear()
        with patch.object(identities, "_danbooru") as network, patch("subprocess.Popen") as cli:
            for name in ("artist_main", "日文畫師", "other name", "@art_handle", "art-fan"):
                self.assertEqual(identities.resolve(name)["folder_name"], row["folder_name"])
            network.assert_not_called()
            cli.assert_not_called()

    def test_existing_alias_folder_is_preferred_and_stays_pinned(self):
        self.image("other name/a.png")
        self.image("other name/b.png")
        self.image("日文畫師/c.png")
        database.scan_library()
        row = self.save()
        self.assertEqual(row["folder_name"], "other name")
        again = self.save(name="renamed_artist")
        self.assertEqual(again["id"], row["id"])
        self.assertEqual(again["folder_name"], "other name")
        self.assertTrue((self.root / "日文畫師/c.png").is_file())

    def test_same_name_different_account_ids_are_not_merged(self):
        first = self.save(name="same", aliases=["same"])
        second = self.save(name="same", aliases=["same"], profiles=["https://pixiv.net/users/456"], dbid=202)
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["folder_name"], second["folder_name"])
        self.assertIsNone(identities.lookup("same"))
        self.assertEqual(identities.lookup(profile_urls=[PROFILES[0]])["id"], first["id"])

    def test_conflicting_accounts_rollback_without_reassigning_or_adding_aliases(self):
        self.save()
        self.save(name="second", aliases=["second"], profiles=["https://pixiv.net/users/456"], dbid=202)
        with self.assertRaisesRegex(ValueError, "不同"):
            self.save(name="unwanted_alias", aliases=["unwanted_alias"], profiles=[PROFILES[0], "https://pixiv.net/users/456"])
        self.assertIsNone(identities.lookup("unwanted_alias"))
        self.assertEqual(len(self.rows("SELECT * FROM artist_identities")), 2)

    def test_new_unknown_account_does_not_silently_join_a_known_name(self):
        self.save()
        self.assertIsNone(identities.lookup("artist_main", ["https://pixiv.net/users/999"]))

    def test_danbooru_exact_url_match_saves_other_names_and_avoids_cli(self):
        response = Mock()
        response.json.return_value = [{"id": 555, "name": "db_artist", "other_names": ["原名"],
                                      "urls": [{"url": u, "is_active": True} for u in PROFILES]}]
        with patch.object(identities, "_danbooru", wraps=REAL_DANBOORU), patch.object(identities.requests, "get", return_value=response) as http, patch("subprocess.Popen") as cli:
            result = identities.resolve("原名", [PROFILES[0]])
        self.assertEqual(result["danbooru_id"], 555)
        self.assertEqual(http.call_count, 1)
        cli.assert_not_called()
        self.assertEqual(identities.resolve("@art_handle")["folder_name"], result["folder_name"])

    def test_ambiguous_danbooru_results_are_not_selected_by_order(self):
        response = Mock()
        response.json.return_value = [{"id": n, "name": f"artist_{n}", "other_names": ["same"], "urls": []} for n in (1, 2)]
        with patch.object(identities, "_danbooru", wraps=REAL_DANBOORU), patch.object(identities.requests, "get", return_value=response):
            result = identities.resolve("same")
        self.assertIsNone(result["folder_name"])
        self.assertIn("多個", result["last_error"])
        self.assertFalse(self.rows("SELECT * FROM artist_identities"))

    def test_danbooru_miss_queues_mcp_research_and_never_starts_cli(self):
        events = []
        def miss(*args):
            events.append("danbooru")
        with patch.object(identities, "_danbooru", side_effect=miss), patch("subprocess.Popen") as process:
            result = identities.resolve("原名")
            process.assert_not_called()
        self.assertEqual(events, ["danbooru"])
        self.assertEqual(result["status"], "pending_model")
        self.assertIsNone(result["folder_name"])
        self.assertEqual(identities.status()["requests"][0]["request_key"], result["request_key"])
        self.assertIn("gallery_confirm_artist_identity", result["next_step"])

    def test_model_needs_link_evidence_covering_every_account_and_original_query(self):
        base = dict(canonical_name="artist_main", aliases=["原名"], profile_urls=PROFILES, evidence=EVIDENCE)
        for changes in ({"evidence": []}, {"evidence": EVIDENCE[:1]}, {"query_name": "another"},
                        {"query_urls": ["https://pixiv.net/users/999"]}, {"profile_urls": [PROFILES[0]]}):
            with self.assertRaises(ValueError):
                identities.confirm_model(**{**base, **changes})
        self.assertFalse(self.rows("SELECT * FROM artist_identities"))

    def test_unavailable_or_unconfirmed_model_is_retryable_and_never_creates_folder(self):
        iid = self.scanned_artist("unknown_artist")
        with patch("subprocess.Popen") as cli:
            result = move_images_to_artist_folder([iid], "unknown artist", auto_rename=False)
            self.assertEqual(result["pending_identity"], 1)
            identities.resolve("unknown_artist")
        cli.assert_not_called()
        self.assertEqual(result["moved"], 0)
        self.assertFalse((self.root / "unknown artist").exists())
        self.assertIsNone(self.rows("SELECT suggested_folder FROM images")[0]["suggested_folder"])
        self.assertEqual(identities.status()["requests"][0]["status"], "pending_model")

    def test_automatic_moves_and_explicit_alias_previews_share_pinned_folder(self):
        iid = self.scanned_artist("other_name")
        self.manual(iid)
        self.save()
        preview = plan_image_moves([iid], "日文畫師", auto_rename=False)
        self.assertEqual(preview["target_folder"], "artist main")
        moved = move_images_to_artist_folder([iid], "other name", auto_rename=False)
        self.assertEqual(moved["moved"], 1)
        self.assertTrue((self.root / "artist main/new.png").is_file())
        self.assertEqual(move_images_to_artist_folder([iid], "other name")["moved"], 0)
        self.assertEqual(len(self.rows("SELECT * FROM image_tags WHERE source='manual'")), 1)

    def test_sidebar_and_filter_union_use_persisted_identity(self):
        from gallery_artist_service import artist_filter_names, artist_rows
        self.scanned_artist("other_name")
        self.save()
        conn = database.get_db()
        try:
            with conn:
                tid = database.ensure_tag(conn, {}, "日文畫師", "artist")
                conn.execute("INSERT INTO image_tags VALUES(1,?,1,'twitter')", (tid,))
                database.refresh_tag_counts(conn)
            row = artist_rows(conn)[0]
            self.assertEqual(row["post_count"], 1)
            self.assertTrue({"日文畫師", "other_name"}.issubset(artist_filter_names(conn, "日文畫師")))
            self.assertIn("other_name", artist_filter_names(conn, "ARTIST MAIN"))
            from gallery_query import search_images
            self.assertEqual(search_images(include_tags="@art_handle")["total"], 1)
            with conn:
                database.ensure_tag(conn, {}, "art_handle", "general")
            self.assertEqual(artist_filter_names(conn, "art_handle"), ["art_handle"])
        finally:
            conn.close()

    def test_migration_is_additive_and_repeatable(self):
        self.scanned_artist("unknown")
        self.save()
        database.init_db()
        self.assertEqual(len(self.rows("SELECT * FROM artist_identities")), 1)
        self.assertEqual(len(self.rows("SELECT * FROM images")), 1)

    def test_settings_api_only_exposes_mcp_status_without_model_settings(self):
        from fastapi.testclient import TestClient
        import app
        client = TestClient(app.app)
        response = client.get("/api/settings/artist-identity")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["config"], {"mode": "mcp"})
        self.assertEqual(client.post("/api/settings/artist-identity", json={"enabled": True}).status_code, 405)
        self.assertFalse((self.data / "artist_identity_model.json").exists())

    def test_identity_lookup_never_holds_write_transaction_during_network(self):
        def writer(*args):
            conn = database.get_db()
            try:
                with conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("UPDATE images SET suggested_folder=NULL")
            finally:
                conn.close()
            return None
        with patch.object(identities, "_danbooru", side_effect=writer):
            self.assertEqual(identities.resolve("no_transaction")["status"], "pending_model")

    def test_mcp_queue_confirmation_persistence_and_filing_end_to_end(self):
        import tool_runtime as runtime
        iid = self.scanned_artist("other_name")
        self.manual(iid)
        with patch("subprocess.Popen") as process:
            queued = runtime.invoke("gallery_resolve_artist_identity", {"artist_name": "other_name"})
            self.assertEqual(queued["status"], "pending_model")
            pending = runtime.invoke("gallery_get_artist_identity_status", {})
            self.assertEqual(pending["pending_count"], 1)
            confirmed = runtime.invoke("gallery_confirm_artist_identity", {
                "request_key": queued["request_key"], "canonical_name": "artist_main", "aliases": ["other_name", "日文畫師"],
                "profile_urls": PROFILES, "evidence": EVIDENCE,
            })
            self.assertEqual(confirmed["status"], "resolved")
            self.assertEqual(confirmed["source"], "model_mcp")
            self.assertEqual(runtime.invoke("gallery_get_artist_identity_status", {})["pending_count"], 0)
            identities._index.clear()
            with patch.object(identities, "_danbooru") as http:
                self.assertEqual(identities.resolve("日文畫師")["folder_name"], "artist main")
                result = move_images_to_artist_folder([iid], auto_rename=False)
                http.assert_not_called()
            process.assert_not_called()
        self.assertEqual(result["moved"], 1)
        self.assertTrue((self.root / "artist main/new.png").is_file())
        self.assertEqual(len(self.rows("SELECT * FROM image_tags WHERE source='manual'")), 1)

    def test_mcp_confirmation_rejects_unrelated_evidence_and_respects_readonly_policy(self):
        import tool_runtime as runtime
        import integration_settings
        request = identities.resolve("original_name", [PROFILES[0]])
        args = {"request_key": request["request_key"], "canonical_name": "artist_main", "aliases": ["unrelated_name"],
                "profile_urls": PROFILES, "evidence": EVIDENCE}
        with self.assertRaisesRegex(ValueError, "原本待查的名稱"):
            runtime.invoke("gallery_confirm_artist_identity", args)
        self.assertFalse(self.rows("SELECT * FROM artist_identities"))
        self.assertEqual(identities.status()["pending_count"], 1)
        integration_settings.save({"mcp_read_only": True})
        with self.assertRaises(PermissionError):
            runtime.invoke("gallery_confirm_artist_identity", {**args, "aliases": ["original_name"]})

    def test_organize_preview_only_includes_confirmed_identities_without_network(self):
        import app
        iid = self.scanned_artist("other_name")
        self.save()
        with patch.object(app, "GALLERY_ROOT", self.root), patch.object(identities, "_danbooru") as http, patch("subprocess.Popen") as cli:
            preview = app.api_organize_artist_folders_preview(min_confidence=0.2, min_images_per_artist=1, only_existing_folders=False)
        self.assertEqual(preview["total_ready_images"], 1)
        self.assertEqual(preview["folders"][0]["folder_name"], "artist main")
        http.assert_not_called()
        cli.assert_not_called()

    def test_pixiv_author_name_and_user_id_are_resolved_before_batch_write(self):
        import ai_tagger
        import reverse_search
        self.image("12345678_p0.png")
        database.scan_library()
        response = Mock(status_code=200)
        response.json.return_value = {"body": {"userName": "PixivName", "userId": "123", "tags": {"tags": []}}}
        def resolve_name(name, urls):
            self.assertEqual(name, "PixivName")
            self.assertEqual(urls, [PROFILES[0]])
            # Acquiring a second write connection proves the caller has not started its batch write.
            self.save(aliases=["PixivName"])
            return {"folder_name": "artist main"}
        with patch.object(ai_tagger, "GALLERY_ROOT", self.root), patch.object(ai_tagger, "log_operation"), patch.object(reverse_search, "search_danbooru_by_pixiv_ids_batch", return_value={}), patch.object(reverse_search, "search_danbooru_by_md5_batch", return_value={}), patch.object(reverse_search, "save_persistent_artist_cache"), patch.object(ai_tagger.requests, "get", return_value=response), patch.object(identities, "resolve", side_effect=resolve_name):
            ai_tagger.lookup_pixiv_artists_batch(enable_google_web_fallback=False)
        self.assertEqual(self.rows("SELECT suggested_folder FROM images")[0]["suggested_folder"], "artist main")
