"""Desktop queue, prompt revisions and real filesystem renaming in offline galleries."""
import datetime as dt
import json
import os
import shutil
import threading
import time
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import agent_jobs as jobs
import database
import prompt_store
import agent_skills
import publication_dates as publication
from agent_api import router
from antigravity_bridge import actual_model
from test_reliability import GalleryCase


class AgentPublicationTests(GalleryCase):
    def setUp(self):
        super().setUp()
        skill_root = self.root.parent / "skills"
        shutil.copytree(agent_skills.skills_root(), skill_root, ignore=shutil.ignore_patterns(".storage"))
        self.stack.enter_context(patch.dict(os.environ, {"TWITTER_AGENT_SKILLS_ROOT": str(skill_root)}))

    def scanned(self, *names):
        for name in names:
            self.image(name)
        database.scan_library()
        return {row["filename"]: row["id"] for row in self.rows("SELECT * FROM images")}

    def running(self, folder="artist"):
        job = jobs.create("publication", {"folder": folder, "pad_digits": 4})
        jobs.heartbeat(jobs.REQUIRED_MODEL)
        self.assertEqual(jobs.claim()["id"], job["id"])
        jobs.launched(job["id"], "00000000-0000-0000-0000-000000000001")
        return jobs.get(job["id"])

    def source(self, iid, date, suffix="1"):
        file = publication.fingerprint(iid)
        url = "https://www.pixiv.net/artworks/" + suffix
        return publication._record(iid, url,
            {"page_url": url, "published_at": date, "artist": {"profile_urls": ["https://www.pixiv.net/users/99"]}},
            file, {"method": "test_verified"})

    def test_prompt_save_changes_both_transports_and_rejects_stale_editor(self):
        from tool_registry import get_tool, catalog
        from mcp_server import protocol_tools
        tool = get_tool("gallery_trace_publication_dates")
        saved = prompt_store.save(tool.name, "查證新說明", 0)
        self.assertEqual(saved["prompt_revision"], 1)
        self.assertTrue(tool.protocol()["description"].startswith("查證新說明\n"))
        self.assertIn("run.result", tool.protocol()["description"])
        self.assertEqual(next(t for t in protocol_tools() if t["name"] == tool.name)["description"], tool.protocol()["description"])
        self.assertTrue(all("task_prompt" not in t and "workflow" not in t for t in catalog()))
        with self.assertRaises(RuntimeError):
            prompt_store.save(tool.name, "stale", 0)
        prompt_store.save(tool.name, "", 1, reset=True)
        self.assertTrue(tool.protocol()["description"].startswith(tool.description + "\n"))

    def test_nonexclusive_tool_exposes_agent_cancellation_and_rejects_late_work(self):
        import tool_runtime as runtime
        self.scanned("artist/a.png")
        job = self.running()
        iid = self.rows("SELECT id FROM images")[0]["id"]
        entered, release = threading.Event(), threading.Event()
        def trace(job_id, image_ids):
            entered.set(); release.wait(3)
            jobs.require_running(job_id, "publication")
            return {"unexpected_completion": True}
        with patch.object(publication, "trace", side_effect=trace):
            started = runtime.dispatch("gallery_trace_publication_dates", {"job_id": job["id"], "image_ids": [iid]})
            try:
                self.assertTrue(entered.wait(1))
                self.assertIsNone(started["job_id"])
                self.assertEqual(started["agent_job_id"], job["id"])
                self.assertNotEqual(started["run_id"], job["id"])
                self.assertEqual(started["cancel_tool"], "gallery_cancel_agent_job")
                stopped = runtime.dispatch(started["cancel_tool"], started["cancel_arguments"])
                self.assertEqual(stopped["status"], "succeeded")
                self.assertEqual(stopped["agent_job"]["status"], "cancelled")
                self.assertFalse(runtime.failed_result(stopped), "A successful stop must not become an MCP tool error")
                self.assertEqual(stopped["agent_job_id"], job["id"])
                with self.assertRaises(ValueError):
                    runtime.dispatch("gallery_cancel_job", {"job_id": job["id"]})
                with self.assertRaises(ValueError):
                    jobs.require_running(job["id"])
            finally:
                release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                run = runtime.get_run(started["run_id"])
                if run["status"] != "running":
                    break
                time.sleep(0.01)
            self.assertEqual(run["status"], "failed")
            self.assertIsNone(run["result"])

    def test_model_gate_duplicate_click_and_frozen_prompt(self):
        self.scanned("artist/a.png")
        first = jobs.create("publication", {"folder": "artist", "pad_digits": 4})
        self.assertEqual(first["id"], jobs.create("publication", {"folder": "artist", "pad_digits": 4})["id"])
        jobs.heartbeat("flash")
        self.assertIsNone(jobs.claim())
        jobs.heartbeat(jobs.REQUIRED_MODEL)
        self.assertEqual(jobs.claim()["id"], first["id"])
        self.assertIsNone(jobs.claim())
        iid = self.rows("SELECT id FROM images")[0]["id"]
        self.source(iid, "2024-01-01T00:00:00Z")
        self.assertEqual(jobs.create("publication", {"folder": "artist", "pad_digits": 4})["id"], first["id"])
        skill = agent_skills.read("gallery-publication-tracing")
        agent_skills.save(skill["id"], skill["content"] + "\nchanged task\n", skill["revision"])
        self.assertNotEqual(jobs.get(first["id"])["prompt"]["prompt"], "changed task")

    def test_cancel_resume_fences_old_conversation_and_preserves_snapshot(self):
        self.scanned("artist/a.png")
        first = self.running()
        jobs.control(first["id"], "stop")
        with self.assertRaises(ValueError):
            jobs.require_running(first["id"])
        resumed = jobs.control(first["id"], "resume")
        self.assertNotEqual(resumed["id"], first["id"])
        self.assertEqual(jobs.get(first["id"])["status"], "cancelled")
        self.assertIn(resumed["id"], resumed["prompt"]["rendered_prompt"])
        self.assertEqual(resumed["prompt"]["rendered_prompt"].splitlines()[0], first["prompt"]["rendered_prompt"].splitlines()[0])
        self.assertEqual(resumed["prompt"]["revision"], first["prompt"]["revision"])

    def test_dates_reorder_folder_preserve_tags_and_support_undo(self):
        ids = self.scanned("artist/new.png", "artist/old_p1.png", "artist/old_p0.png", "artist/unknown.png")
        self.manual(ids["new.png"])
        self.source(ids["new.png"], "2024-04-03T00:00:00+09:00", "3")
        self.source(ids["old_p1.png"], "2024-04-01T18:00:00Z", "2")
        self.source(ids["old_p0.png"], "2024-04-01T18:00:00Z", "2")
        result = publication.rename_folder("artist")
        self.assertEqual(result["renamed"], 3)
        named = {row["id"]: row["filename"] for row in self.rows("SELECT id,filename FROM images")}
        self.assertEqual(named[ids["old_p0.png"]], "20240402_0001.png")
        self.assertEqual(named[ids["old_p1.png"]], "20240402_0002.png")
        self.assertEqual(named[ids["new.png"]], "20240402_0003.png")
        self.assertEqual(named[ids["unknown.png"]], "unknown.png")
        self.assertEqual(publication.rename_folder("artist")["renamed"], 0)
        self.assertEqual(len(self.rows("SELECT * FROM image_tags WHERE source='manual'")), 1)
        import renamer
        with patch.object(renamer, "GALLERY_ROOT", self.root), patch.object(renamer, "DATA_DIR", self.data), patch.object(renamer, "RENAME_LOG_DIR", self.data / "rename_logs"):
            renamer.undo_rename_batch(result["operation_id"])
        self.assertTrue((self.root / "artist/new.png").exists())

    def test_earlier_source_reindexes_and_modified_content_invalidates_date(self):
        ids = self.scanned("artist/a.png", "artist/b.png")
        self.source(ids["a.png"], "2024-05-02T00:00:00Z", "1")
        self.source(ids["b.png"], "2024-05-03T00:00:00Z", "2")
        publication.rename_folder("artist")
        self.source(ids["b.png"], "2023-05-01T00:00:00Z", "3")
        publication.rename_folder("artist")
        self.assertEqual(self.rows("SELECT filename FROM images WHERE id=?", ids["b.png"])[0]["filename"], "20230501_0001.png")
        row = self.rows("SELECT rel_path FROM images WHERE id=?", ids["a.png"])[0]
        from PIL import Image
        Image.new("RGB", (25, 25), "blue").save(self.root / row["rel_path"])
        planned = publication.plan("artist")
        self.assertEqual(planned["verified"], 1)
        self.assertEqual(publication.sources(ids["a.png"]), [])

    def test_conflict_does_not_rename_or_append_suffix(self):
        ids = self.scanned("artist/a.png", "artist/20240101_0001.png")
        self.source(ids["a.png"], "2024-01-01T00:00:00Z")
        with self.assertRaises(FileExistsError):
            publication.rename_folder("artist")
        self.assertTrue((self.root / "artist/a.png").exists())
        self.assertEqual(list((self.root / "artist").glob("*dup*")), [])

    def test_source_url_rejects_cdn_revision_and_timestamp_has_timezone(self):
        self.assertEqual(publication.original_page("https://twitter.com/author/status/123/photo/1?x=1"), "https://x.com/author/status/123")
        for url in ("http://x.com/a/status/123", "https://pbs.twimg.com/media/foo.jpg", "https://i.pximg.net/img/2024/01/01/a.png", "https://127.0.0.1/a"):
            with self.assertRaises(ValueError):
                publication.original_page(url)
        with self.assertRaises(ValueError):
            publication._date("2024-01-01T00:00:00")

    def test_source_submission_checks_scope_author_image_and_cancel(self):
        ids = self.scanned("artist/a.png")
        job = self.running()
        info = {"page_url": "https://www.pixiv.net/artworks/123", "published_at": "2024-01-01T00:00:00Z", "image_urls": ["https://i.pximg.net/original.jpg"], "artist": {"profile_urls": ["https://www.pixiv.net/users/99"]}}
        args = dict(job_id=job["id"], image_id=ids["a.png"], source_url=info["page_url"], image_url="https://i.pximg.net/original.jpg", author_profile_url="https://www.pixiv.net/users/99", match_reason="已逐圖比對作者原作品第零頁，人物構圖及細節皆與本機圖片相符。")
        with patch.object(publication, "_source_info", return_value=info):
            with self.assertRaises(ValueError):
                publication.submit(**{**args, "author_profile_url": "https://www.pixiv.net/users/88"})
            publication.submit(**args)
            jobs.control(job["id"], "stop")
            with self.assertRaises(ValueError):
                publication.submit(**args)

    def test_actual_model_never_accepts_prompt_echo_or_generic_flash_alias(self):
        self.assertEqual(actual_model({"title": "gemini-3.8-flash", "prompt": {"model": "gemini-3.8-flash"}, "model": "flash"}), "")
        self.assertEqual(actual_model({"metadata": {"actualModelId": "gemini-3.8-flash-medium"}}), "gemini-3.8-flash")
        from antigravity_bridge import catalog_models
        catalog = catalog_models({"clientModelConfigs": [{"label": "Gemini 3.8 Flash", "modelOrAlias": {"model": "MODEL_PLACEHOLDER_EXAMPLE"}}]})
        self.assertEqual(actual_model({"plannerConfig": {"planModel": "MODEL_PLACEHOLDER_EXAMPLE"}}, catalog), "gemini-3.8-flash")
        self.assertEqual(actual_model({"plannerConfig": {"planModel": "MODEL_PLACEHOLDER_UNKNOWN"}}, catalog), "")
        self.assertEqual(actual_model({"plannerConfig": {"planModel": "MODEL_PLACEHOLDER_UNKNOWN", "requestedModel": {"model": "MODEL_PLACEHOLDER_EXAMPLE"}}}, catalog), "")

    def test_runtime_probe_requires_actual_generation_and_completed_mcp(self):
        from antigravity_bridge import probe_summary
        catalog = {"MODEL_38": "gemini-3.8-flash"}
        execution = {"trajectory": {"steps": [
            {"metadata": {"modelUsage": {"model": "MODEL_38"}}},
            {"type": "CORTEX_STEP_TYPE_MCP_TOOL", "status": "CORTEX_STEP_STATUS_WAITING",
             "mcpTool": {"serverName": "gallery", "toolCall": {"name": "gallery_get_overview"}}}]}}
        summary = probe_summary(execution, catalog, "gallery")
        self.assertTrue(summary["runtime_verified"])
        self.assertFalse(summary["mcp_ready"])
        execution["trajectory"]["steps"][1]["status"] = "CORTEX_STEP_STATUS_DONE"
        self.assertTrue(probe_summary(execution, catalog, "gallery")["mcp_ready"])
        execution["trajectory"]["steps"][0]["metadata"]["modelUsage"]["model"] = "OTHER_MODEL"
        self.assertFalse(probe_summary(execution, catalog, "gallery")["runtime_verified"])

    def test_desktop_cli_response_envelope_exposes_trusted_metadata(self):
        from antigravity_bridge import command
        from types import SimpleNamespace
        executable = self.root / "desktop.exe"
        executable.write_bytes(b"fixture")
        response = {"response": {"conversationMetadata": {"metadata": {"staticConfig": {
            "cascadeConfig": {"plannerConfig": {"planModel": "MODEL_38"}}}}}}, "error": None}
        with patch.dict("os.environ", {"GALLERY_ANTIGRAVITY_EXECUTABLE": str(executable)}), patch("antigravity_bridge.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr="")):
            metadata = command("get-conversation-metadata", "example")
        self.assertEqual(actual_model(metadata, {"MODEL_38": "gemini-3.8-flash"}), "gemini-3.8-flash")

    def test_native_permissions_preserve_explicit_denials_and_avoid_global_changes(self):
        from antigravity_bridge import native_config
        config = self.root / ".gemini/config"
        config.mkdir(parents=True)
        policy = {"userSettings": {"globalPermissionGrants": {"deny": ["mcp(gallery/gallery_submit_artist_identity)"], "ask": ["read_url(x.com)"]}}}
        path = config / "config.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        with patch("antigravity_bridge.Path.home", return_value=self.root):
            configured = native_config("MODEL_38")
        grants = configured["plannerConfig"]["toolConfig"]["permissionConfig"]["effectiveGrants"]
        self.assertIn("mcp(gallery/gallery_submit_artist_identity)", grants["deny"])
        self.assertIn("read_url(x.com)", grants["ask"])
        self.assertIn("command(*)", grants["deny"])
        self.assertIn("write_file(*)", grants["deny"])
        self.assertNotIn("mcp(*)", grants["allow"])
        self.assertEqual(json.loads(path.read_text()), policy)

    def test_http_queue_returns_202_and_bridge_requires_authentication(self):
        self.scanned("artist/a.png")
        app = FastAPI(); app.include_router(router)
        with TestClient(app) as client:
            value = client.post("/api/agent/jobs", json={"folder": "artist"})
            self.assertEqual(value.status_code, 202)
            self.assertEqual(client.post("/api/agent/bridge", json={"action": "heartbeat", "model_id": jobs.REQUIRED_MODEL}).status_code, 403)
            token = jobs.bridge_token(create=True)
            self.assertEqual(client.post("/api/agent/bridge", json={"action": "heartbeat", "model_id": jobs.REQUIRED_MODEL}, headers={"X-Gallery-Bridge-Token": token}).status_code, 200)

    def test_model_wait_does_not_own_gallery_write_lock(self):
        self.scanned("artist/a.png")
        self.running()
        from background_work import acquire_job
        lease = acquire_job("unrelated write")
        lease.release()

    def test_sidecar_setup_preserves_existing_account_and_plugin_settings(self):
        import antigravity_setup
        config_root = self.root / "antigravity-config"
        (config_root / "projects").mkdir(parents=True)
        (config_root / "projects" / "project.json").write_text('{"id":"project"}')
        previous = {"plugins": {"existing": {"enabled": True}}, "userSettings": {"theme": "dark"}}
        (config_root / "config.json").write_text(json.dumps(previous))
        local = self.root / "local"
        executable = local / "Programs/Antigravity/resources/bin/language_server.exe"
        executable.parent.mkdir(parents=True); executable.write_bytes(b"fixture")
        with patch.dict("os.environ", {"LOCALAPPDATA": str(local)}):
            antigravity_setup.install("project", config_root=config_root)
        updated = json.loads((config_root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(updated["plugins"], previous["plugins"])
        self.assertEqual(updated["userSettings"], previous["userSettings"])
        self.assertEqual(updated["sidecars"]["gallery-publication"]["projectId"], "project")
        self.assertEqual(len(list(config_root.glob("*.bak"))), 1)
        self.assertTrue(jobs.bridge_token())

    def test_publication_mode_preserves_unknown_import_filename(self):
        ids = self.scanned("download.png")
        from background_work import atomic_json
        atomic_json(self.data / "publication_settings.json", {"enabled": True, "pad_digits": 4})
        from gallery_service import move_images_to_folder
        value = move_images_to_folder([ids["download.png"]], "artist", auto_rename=True)
        self.assertEqual(value["moved"], 1)
        self.assertTrue((self.root / "artist/download.png").exists())

    def test_verified_images_are_not_requeued_but_modified_content_is(self):
        ids = self.scanned("artist/a.png", "artist/b.png")
        self.source(ids["a.png"], "2024-01-01T00:00:00Z")
        job = jobs.create("publication", {"folder": "artist", "pad_digits": 4})
        with jobs.connection() as conn:
            selected = [r[0] for r in conn.execute("SELECT image_id FROM agent_items WHERE job_id=?", (job["id"],))]
        self.assertEqual(selected, [ids["b.png"]])
        jobs.control(job["id"], "stop")
        from PIL import Image
        Image.new("RGB", (26, 26), "red").save(self.root / "artist/a.png")
        changed = jobs.create("publication", {"folder": "artist", "pad_digits": 4})
        with jobs.connection() as conn:
            selected = {r[0] for r in conn.execute("SELECT image_id FROM agent_items WHERE job_id=?", (changed["id"],))}
        self.assertEqual(selected, {ids["a.png"], ids["b.png"]})
