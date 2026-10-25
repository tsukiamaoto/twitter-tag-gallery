"""Image Skill migration and model dispatch use disposable roots and fake GPU calls."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import agent_skills
from agent_skills_api import router as agent_router
import image_skills
from image_skills_api import router as image_router
from decensor_engine import AnimeDecensorEngine
import gguf_models


class ImageAgentSkillTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.agents = self.root / "agent-skills"
        self.agents.mkdir()
        self.legacy = self.root / "image-skills"
        self.legacy.mkdir()
        self.index = self.legacy / "skills.json"
        self.template = json.loads(Path(image_skills.__file__).with_name("image_skill_templates.json").read_text(encoding="utf-8"))[0]
        self.index.write_text(json.dumps({"version": 1, "skills": [self.template], "active_id": self.template["id"], "archived": []}), encoding="utf-8")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"TWITTER_AGENT_SKILLS_ROOT": str(self.agents),
                                                       "TWITTER_IMAGE_SKILLS_ROOT": str(self.legacy),
                                                       "TWITTER_GGUF_ROOT": str(self.root / "models")}))
        self.stack.enter_context(patch("agent_workflows.migrate", return_value={}))
        app = FastAPI()
        app.include_router(agent_router)
        app.include_router(image_router)
        self.client = self.stack.enter_context(TestClient(app))
        self.skill_id = self.template["id"]

    def request(self, value, **changes):
        return image_skills.SkillInput.model_validate({k: v for k, v in {**value, **changes}.items()
                                                     if k not in ("id", "path", "kind")})

    def test_legacy_migrates_to_one_agent_document_with_model_and_active_selection(self):
        original = self.index.read_text(encoding="utf-8")
        result = image_skills.status()
        self.assertEqual(result["active_id"], self.skill_id)
        for key, value in self.template.items():
            self.assertEqual(result["skills"][0][key], value)
        doc = agent_skills.require(self.skill_id)
        self.assertEqual(doc["kind"], "image-repair")
        self.assertEqual(doc["metadata"]["gallery"]["config"]["model_id"], self.template["model_id"])
        self.assertIn("先預覽", doc["body"])
        self.assertEqual(json.loads(self.index.read_text(encoding="utf-8"))["skills"], [self.skill_id])
        backup = self.legacy / "skills.before-agent-skills.json"
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), json.loads(original))
        self.assertEqual(image_skills.status(), result)
        values = self.client.get("/api/agent-skills").json()["skills"]
        self.assertEqual([item["id"] for item in values], [self.skill_id])

    def test_interrupted_migration_reuses_document_without_overwriting_subsequent_edits(self):
        atomic = image_skills.atomic_json
        def fail_index(path, value, **options):
            if path == self.index and value.get("version") == 2:
                raise OSError("injected index failure")
            return atomic(path, value, **options)
        with patch.object(image_skills, "atomic_json", side_effect=fail_index):
            with self.assertRaises(OSError):
                image_skills.status()
        self.assertEqual(json.loads(self.index.read_text(encoding="utf-8"))["version"], 1)
        doc = agent_skills.require(self.skill_id)
        agent_skills.save(self.skill_id, doc["content"] + "\nKeep edited instructions.\n", doc["revision"])
        self.assertIn("Keep edited instructions.", image_skills.status()["skills"][0]["instructions"])
        self.assertEqual(len(agent_skills.catalog()["skills"]), 1)

    def test_migration_conflict_preserves_unrelated_skill_and_legacy_source(self):
        target = agent_skills.skill_path(self.skill_id)
        target.parent.mkdir()
        content = f"---\nname: {self.skill_id}\ndescription: unrelated workflow\n---\nUnrelated body.\n"
        target.write_text(content, encoding="utf-8")
        before = self.index.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "已存在"):
            image_skills.status()
        self.assertEqual(target.read_text(encoding="utf-8"), content)
        self.assertEqual(self.index.read_bytes(), before)

    def test_both_editors_share_configuration_and_reject_stale_form(self):
        value = image_skills.status()["skills"][0]
        model = "diffusion_models/custom-wai-illustrious.gguf"
        saved = image_skills.save(self.request(value, model_id=model, instructions="Use a small mask."), self.skill_id)
        self.assertEqual(image_skills.prepare(self.skill_id)["model_id"], model)
        self.assertEqual(agent_skills.require(self.skill_id)["body"], "Use a small mask.")
        with self.assertRaisesRegex(RuntimeError, "已被其他"):
            image_skills.save(self.request(value, prompt="Stale edit"), self.skill_id)
        doc = agent_skills.require(self.skill_id)
        response = self.client.put("/api/agent-skills/" + self.skill_id, json={
            "content": doc["content"].replace(model, "diffusion_models/another-wai-illustrious.gguf"), "revision": doc["revision"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(image_skills.find(self.skill_id)["model_id"], "diffusion_models/another-wai-illustrious.gguf")
        stale = self.client.put("/api/image-skills/" + self.skill_id, json=self.request(saved, prompt="Stale").model_dump())
        self.assertEqual(stale.status_code, 409)

    def test_raw_editor_rejects_invalid_model_metadata(self):
        image_skills.status()
        doc = agent_skills.require(self.skill_id)
        invalid = doc["content"].replace(self.template["model_id"], "../outside.gguf")
        response = self.client.put("/api/agent-skills/" + self.skill_id, json={"content": invalid, "revision": doc["revision"]})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(agent_skills.require(self.skill_id)["revision"], doc["revision"])

    def test_form_edits_preserve_custom_frontmatter_and_legacy_clients_keep_instructions(self):
        image_skills.status()
        doc = agent_skills.require(self.skill_id)
        content = doc["content"].replace("description:", "license: MIT\ndescription:", 1)
        content += "\nKeep this custom workflow.\n"
        agent_skills.save(self.skill_id, content, doc["revision"])
        legacy_request = {k: v for k, v in self.template.items() if k != "id"}
        saved = image_skills.save(image_skills.SkillInput(**legacy_request), self.skill_id)
        self.assertIn("Keep this custom workflow.", saved["instructions"])
        self.assertIn("license: MIT", agent_skills.require(self.skill_id)["content"])

    def test_create_discover_activate_and_archive_use_agent_documents(self):
        image_skills.status()
        saved = image_skills.save(self.request(self.template, name="Background repair", instructions="Preserve the subject."))
        self.assertIn(saved["id"], [item["id"] for item in agent_skills.catalog()["skills"]])
        self.assertEqual(image_skills.find(saved["id"])["instructions"], "Preserve the subject.")
        select = self.stack.enter_context(patch.object(gguf_models.manager, "selection"))
        prepared = image_skills.activate(saved["id"], select)
        select.assert_called_once_with(saved["model_id"], saved["profile"])
        self.assertEqual(prepared["prompt"], saved["prompt"])
        image_skills.archive(saved["id"])
        self.assertIsNone(image_skills.status()["active_id"])
        self.assertNotIn(saved["id"], [item["id"] for item in agent_skills.catalog()["skills"]])
        self.assertTrue(list((self.agents / ".storage" / "archived" / saved["id"]).glob("*.md")))

    def test_repair_uses_skill_model_even_when_global_model_selection_differs(self):
        value = image_skills.status()["skills"][0]
        value = image_skills.save(self.request(value, model_id="diffusion_models/custom-image.gguf", steps=64), self.skill_id)
        runtime = {"port": 9999, "loaded_model": value["model_id"]}
        engine = AnimeDecensorEngine()
        with patch.object(gguf_models.manager, "settings", return_value={"model_id": "diffusion_models/other.gguf", "profile": "qwen21"}), \
             patch.object(gguf_models.manager, "ensure_loaded", return_value=runtime) as load:
            resolved = engine._resolve_decensor_skill_and_runtime()
        load.assert_called_once_with(model_id=value["model_id"], profile=value["profile"])
        self.assertEqual(resolved["prompt"], value["prompt"])
        self.assertEqual(resolved["seed"], value["seed"])
        self.assertEqual(resolved["profile"], "wai_illustrious")
        self.assertEqual(resolved["steps"], 64)

    def test_missing_or_wrong_skill_model_stops_instead_of_using_another_model(self):
        image_skills.status()
        engine = AnimeDecensorEngine()
        with patch.object(gguf_models.manager, "ensure_loaded", side_effect=ValueError("missing model")):
            with self.assertRaisesRegex(ValueError, "missing model"):
                engine._resolve_decensor_skill_and_runtime()
        with patch.object(gguf_models.manager, "ensure_loaded", return_value={"port": 9999, "loaded_model": "wrong.gguf"}):
            with self.assertRaisesRegex(RuntimeError, "不符"):
                engine._resolve_decensor_skill_and_runtime()
        agent_skills.skill_path(self.skill_id).unlink()
        with self.assertRaisesRegex(ValueError, "遺失或格式錯誤"):
            engine._resolve_decensor_skill_and_runtime()


if __name__ == "__main__":
    unittest.main()
