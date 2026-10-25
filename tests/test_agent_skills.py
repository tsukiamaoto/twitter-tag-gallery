"""Skill edits, migration and task snapshots use disposable project and gallery roots."""
import json
import os
from pathlib import Path
import shutil
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import agent_jobs
import agent_skills as skills
import agent_workflows as workflows
from agent_skills_api import router
from agent_api import router as agent_router
import database
import prompt_store
import tool_registry
from background_work import atomic_json
from mcp_server import protocol_tools
from test_reliability import GalleryCase


class AgentSkillTests(GalleryCase):
    def setUp(self):
        super().setUp()
        self.skills = self.root.parent / "skills"
        shutil.copytree(skills.skills_root(), self.skills, ignore=shutil.ignore_patterns(".storage"))
        self.image_skills = self.root.parent / "image-skills"
        self.image_skills.mkdir()
        atomic_json(self.image_skills / "skills.json", {"version": 2, "skills": [], "active_id": None})
        self.stack.enter_context(patch.dict(os.environ, {"TWITTER_AGENT_SKILLS_ROOT": str(self.skills),
                                                       "TWITTER_IMAGE_SKILLS_ROOT": str(self.image_skills)}))
        app = FastAPI()
        app.include_router(router)
        app.include_router(agent_router)
        self.client = self.stack.enter_context(TestClient(app))
        self.skill_id = "gallery-publication-tracing"

    def legacy(self, tasks=None):
        value = {"revision": 7, "tools": {"gallery_get_overview": "custom overview"},
                 "tasks": tasks or {"publication": "自訂日期流程", "artist_identity": "自訂身分流程", "deduplication": "自訂去重流程"}}
        atomic_json(prompt_store.path(), value, durable=True)
        return value

    def test_catalog_documents_and_save_are_independent_from_tools(self):
        values = self.client.get('/api/agent-skills').json()["skills"]
        self.assertEqual(len(values), 4)
        self.assertTrue(all(item["valid"] for item in values))
        self.assertTrue(all("content" not in item for item in values))
        tool_before = tool_registry.catalog()
        doc = self.client.get('/api/agent-skills/' + self.skill_id).json()
        content = doc["content"] + "\n新增步驟。\n"
        saved = self.client.put('/api/agent-skills/' + self.skill_id, json={"content": content, "revision": doc["revision"]})
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["content"], content)
        self.assertNotEqual(saved.json()["revision"], doc["revision"])
        self.assertEqual(tool_before, tool_registry.catalog())
        self.assertFalse(prompt_store.path().exists())
        backup = self.skills / '.storage' / 'backups' / self.skill_id / (doc['revision'] + '.md')
        self.assertEqual(backup.read_bytes().decode('utf-8'), doc['content'])

    def test_tools_work_without_skill_directory_and_leave_legacy_tasks_untouched(self):
        old = self.legacy()
        with patch.dict(os.environ, {"TWITTER_AGENT_SKILLS_ROOT": str(self.skills / 'missing')}):
            self.assertEqual(len(protocol_tools()), len(tool_registry.CATALOG))
            saved = prompt_store.save('gallery_get_overview', 'updated description', 7)
            self.assertEqual(saved['usage_prompt'], 'updated description')
            self.assertTrue(all('workflow' not in tool and 'task_prompt' not in tool for tool in tool_registry.catalog()))
            self.assertEqual(prompt_store.load()['tasks'], old['tasks'])
            self.assertFalse((self.skills / 'missing').exists())

    def test_stale_revision_validation_missing_and_no_creation(self):
        doc = skills.read(self.skill_id)
        path = skills.skill_path(self.skill_id)
        path.write_text(doc['content'] + '\nexternal edit\n', encoding='utf-8')
        endpoint = '/api/agent-skills/' + self.skill_id
        self.assertEqual(self.client.put(endpoint, json={'content': doc['content'], 'revision': doc['revision']}).status_code, 409)
        current = skills.read(self.skill_id)
        invalid = ['plain text', current['content'].replace('name: ' + self.skill_id, 'name: renamed'),
                   f'---\nname: {self.skill_id}\ndescription: blank body\n---\n',
                   f'---\nname: {self.skill_id}\ndescription: [\n---\nbody']
        for content in invalid:
            response = self.client.put(endpoint, json={'content': content, 'revision': current['revision']})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(skills.read(self.skill_id)['content'], current['content'])
        self.assertEqual(self.client.put('/api/agent-skills/missing', json={'content': '---\nname: missing\ndescription: test\n---\nbody', 'revision': 'a'*64}).status_code, 404)
        self.assertEqual(self.client.post('/api/agent-skills', json={}).status_code, 405)
        with self.assertRaises(ValueError):
            skills.skill_path('../outside')
        with self.assertRaises(ValueError):
            skills.skill_path('C:\\outside')

    def test_invalid_document_can_be_repaired_and_unreadable_file_is_reported(self):
        path = skills.skill_path(self.skill_id)
        original = path.read_text(encoding='utf-8')
        path.write_text('broken YAML', encoding='utf-8')
        doc = self.client.get('/api/agent-skills/' + self.skill_id).json()
        self.assertFalse(doc['valid'])
        response = self.client.put('/api/agent-skills/' + self.skill_id, json={'content': original, 'revision': doc['revision']})
        self.assertEqual(response.status_code, 200)
        path.write_bytes(b'\xff')
        values = self.client.get('/api/agent-skills').json()['skills']
        item = next(item for item in values if item['id'] == self.skill_id)
        self.assertFalse(item['valid'])
        self.assertIsNone(item['revision'])

    def test_write_failure_keeps_original_and_backup(self):
        doc = skills.read(self.skill_id)
        replace = os.replace
        def fail_target(source, target):
            if Path(target) == skills.skill_path(self.skill_id):
                raise PermissionError('injected replacement failure')
            return replace(source, target)
        with patch.object(skills.os, 'replace', side_effect=fail_target):
            with self.assertRaises(PermissionError):
                skills.save(self.skill_id, doc['content'] + '\nnew\n', doc['revision'])
        self.assertEqual(skills.read(self.skill_id)['revision'], doc['revision'])
        self.assertEqual(len(list((self.skills / '.storage' / 'backups').rglob('*.md'))), 1)
        self.assertEqual(list(self.skills.rglob('*.tmp')), [])

    def test_symlink_cannot_escape_skill_root(self):
        outside = self.root.parent / 'outside'
        outside.mkdir()
        (outside / 'SKILL.md').write_text('outside', encoding='utf-8')
        try:
            (self.skills / 'escaped').symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != 'nt':
                raise
            import _winapi
            _winapi.CreateJunction(str(outside), str(self.skills / 'escaped'))
        with self.assertRaises(ValueError):
            skills.read('escaped')
        item = next(item for item in skills.catalog()['skills'] if item['id'] == 'escaped')
        self.assertFalse(item['valid'])

    def test_locked_editor_and_change_during_backup_preserve_external_edits(self):
        doc = skills.read(self.skill_id)
        with skills.locked():
            response = self.client.put('/api/agent-skills/' + self.skill_id, json={
                'content': doc['content'] + '\nnew\n', 'revision': doc['revision']})
            self.assertEqual(response.status_code, 409)
        write = skills.atomic_write
        def external_edit(path, content):
            write(path, content)
            skills.skill_path(self.skill_id).write_bytes((doc['content'] + '\nexternal edit\n').encode('utf-8'))
        with patch.object(skills, 'atomic_write', side_effect=external_edit):
            with self.assertRaises(RuntimeError):
                skills.save(self.skill_id, doc['content'] + '\nnew\n', doc['revision'])
        self.assertTrue(skills.read(self.skill_id)['content'].endswith('\nexternal edit\n'))

    def test_tool_api_rejects_legacy_task_prompt_without_mutation(self):
        response = self.client.put('/api/settings/mcp/tools/gallery_get_overview/prompt', json={
            'usage_prompt': 'description', 'task_prompt': 'legacy workflow', 'revision': 0})
        self.assertEqual(response.status_code, 422)
        self.assertFalse(prompt_store.path().exists())

    def test_migration_preserves_customizations_and_is_repeatable(self):
        original = self.legacy()
        before = {item['id']: skills.read(item['id'])['content'] for item in skills.catalog()['skills']}
        config = workflows.migrate()
        self.assertEqual(prompt_store.load()['tools'], original['tools'])
        self.assertNotIn('tasks', prompt_store.load())
        self.assertEqual(len(list((self.data / 'backups').glob('tool_prompts_before_skills_*.json'))), 1)
        for workflow, skill_id in config['imported_skills'].items():
            self.assertEqual(skills.require(skill_id)['body'], original['tasks'][workflow])
        for skill_id, content in before.items():
            self.assertEqual(skills.read(skill_id)['content'], content)
        self.assertEqual(workflows.snapshot('publication', {'job_id': 'test'})['prompt'], '自訂日期流程')
        self.assertEqual(workflows.migrate(), config)
        self.assertEqual(len(skills.catalog()['skills']), 7)

    def test_failed_migration_preserves_source_and_recovers_without_duplicate_imports(self):
        original = self.legacy()
        import_legacy = skills.import_legacy
        def fail_second(base, body):
            if base == workflows.WORKFLOWS['artist_identity']:
                raise OSError('injected import failure')
            return import_legacy(base, body)
        with patch.object(skills, 'import_legacy', side_effect=fail_second):
            listing = self.client.get('/api/agent-skills').json()
        self.assertIn('injected import failure', listing['migration_error'])
        self.assertEqual(prompt_store.load(), original)
        self.assertFalse(workflows.settings_path().exists())
        workflows.migrate()
        workflows.migrate()
        self.assertEqual(len(skills.catalog()['skills']), 7)

    def test_migration_recovers_after_bindings_commit_before_cleanup(self):
        self.legacy({'publication': 'custom\r\nworkflow'})
        write = workflows.atomic_json
        def fail_cleanup(path, value, **options):
            if path == prompt_store.path():
                raise OSError('cleanup failure')
            return write(path, value, **options)
        with patch.object(workflows, 'atomic_json', side_effect=fail_cleanup):
            with self.assertRaises(OSError):
                workflows.migrate()
        config = workflows.settings()
        imported = skills.read(config['skills']['publication'])
        skills.save(imported['id'], imported['content'] + '\nuser edit\n', imported['revision'])
        workflows.migrate()
        self.assertNotIn('tasks', prompt_store.load())
        self.assertIn('user edit', workflows.snapshot('publication', {})['prompt'])

    def test_migration_only_touches_current_gallery_and_preserves_unknown_workflows(self):
        original = self.legacy({'unknown_workflow': 'custom instructions'})
        with patch.object(database, 'DATA_DIR', self.root.parent / 'another-gallery-data'):
            self.assertEqual(workflows.migrate()['skills'], {})
        self.assertEqual(prompt_store.load(), original)
        with self.assertRaises(ValueError):
            workflows.migrate()
        self.assertEqual(prompt_store.load(), original)
        self.assertEqual(len(skills.catalog()['skills']), 4)

    def test_snapshots_keep_old_jobs_and_retries_but_new_jobs_read_latest_skill(self):
        first = agent_jobs.create('artist_identity', {'artist_name': 'first'})
        doc = skills.read(workflows.WORKFLOWS['artist_identity'])
        skills.save(doc['id'], doc['content'] + '\nnew workflow rule\n', doc['revision'])
        second = agent_jobs.create('artist_identity', {'artist_name': 'second'})
        self.assertNotEqual(first['prompt']['revision'], second['prompt']['revision'])
        self.assertEqual(agent_jobs.get(first['id'])['prompt'], first['prompt'])
        agent_jobs.control(first['id'], 'stop')
        skills.skill_path(doc['id']).unlink()
        resumed = agent_jobs.control(first['id'], 'resume')
        self.assertEqual(resumed['prompt']['revision'], first['prompt']['revision'])
        with self.assertRaises(ValueError):
            agent_jobs.create('artist_identity', {'artist_name': 'third'})

    def test_missing_skill_does_not_enable_publication_or_create_job(self):
        self.image('artist/a.png')
        database.scan_library()
        skills.skill_path(self.skill_id).unlink()
        response = self.client.post('/api/agent/jobs', json={'workflow': 'publication', 'folder': 'artist'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(agent_jobs.history(), [])
        self.assertFalse((self.data / 'publication_settings.json').exists())
