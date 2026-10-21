"""Fault injection uses temporary galleries and never loads GPU models or calls APIs."""
from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
import database
import background_work as work
import file_operations
import integration_settings as integrations
from file_safety import validate_source


class GalleryCase(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)/"library";self.root.mkdir()
        self.data=self.root/".tag_gallery"
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        for name,value in {"GALLERY_ROOT":self.root,"DATA_DIR":self.data,"THUMB_DIR":self.data/"thumbs","MODEL_DIR":self.data/"models","DB_PATH":self.data/"gallery.db"}.items():
            self.stack.enter_context(patch.object(database,name,value))
        self.stack.enter_context(patch.object(database,"log_operation",return_value=0))
        self.stack.enter_context(patch.object(database,"background_checkpoint"))
        database.init_db()

    def image(self,name="image.png"):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
        Image.new("RGB",(10,10),"red").save(path)
        return path

    def rows(self,sql,*args):
        conn=database.get_db()
        try:return [dict(row) for row in conn.execute(sql,args)]
        finally:conn.close()

    def manual(self,iid):
        conn=database.get_db()
        try:
            with conn:
                tid=database.ensure_tag(conn,{},"keep-manual","general")
                conn.execute("INSERT INTO image_tags(image_id,tag_id,source) VALUES(?,?,'manual')",(iid,tid))
                database.refresh_tag_counts(conn)
        finally:conn.close()


class ScanSafetyTests(GalleryCase):
    def test_incomplete_walk_does_not_mark_or_remove_images_and_manual_tags(self):
        path=self.image("artist/image.png");database.scan_library();self.manual(1)
        def broken(root,onerror=None):
            onerror(PermissionError(13,"denied",str(path.parent)))
            yield str(self.root),["artist"],[]
        with patch.object(database.os,"walk",side_effect=broken):result=database.scan_library()
        self.assertFalse(result["scan_complete"])
        self.assertEqual(result["missing_marked"],0)
        self.assertIsNone(self.rows("SELECT missing_since FROM images")[0]["missing_since"])
        self.assertEqual(len(self.rows("SELECT * FROM image_tags WHERE source='manual'")),1)

    def test_full_scan_marks_missing_and_return_preserves_id_and_manual_tags(self):
        path=self.image();database.scan_library();self.manual(1);saved=path.read_bytes();path.unlink()
        result=database.scan_library();self.assertEqual(result["removed"],0);self.assertEqual(result["missing_marked"],1)
        path.write_bytes(saved);database.scan_library()
        self.assertEqual(self.rows("SELECT id,missing_since FROM images"),[{"id":1,"missing_since":None}])
        self.assertEqual(len(self.rows("SELECT * FROM image_tags WHERE source='manual'")),1)

    def test_external_edit_resets_only_derived_analysis_and_updates_stat(self):
        path=self.image();database.scan_library();self.manual(1)
        conn=database.get_db()
        with conn:
            tid=database.ensure_tag(conn,{},"old-ai","general")
            conn.execute("INSERT INTO image_tags(image_id,tag_id,source) VALUES(1,?,'ai')",(tid,))
            conn.execute("UPDATE images SET ai_tagged=1")
        conn.close()
        Image.new("RGB",(20,20),"blue").save(path)
        result=database.scan_library();self.assertEqual(result["modified"],1)
        row=self.rows("SELECT size_bytes,mtime_ns,ai_tagged FROM images")[0]
        self.assertEqual(row["size_bytes"],path.stat().st_size);self.assertEqual(row["mtime_ns"],path.stat().st_mtime_ns)
        self.assertEqual(row["ai_tagged"],0);self.assertEqual(len(self.rows("SELECT * FROM image_tags WHERE source='manual'")),1)
        self.assertFalse(self.rows("SELECT * FROM image_tags WHERE source='ai'"))

    def test_external_rename_preserves_identity_and_manual_tags(self):
        path=self.image();database.scan_library();self.manual(1);path.rename(self.root/"renamed.png")
        result=database.scan_library();self.assertEqual(result["relocated"],1)
        self.assertEqual(self.rows("SELECT id,rel_path FROM images"),[{"id":1,"rel_path":"renamed.png"}])
        self.assertEqual(len(self.rows("SELECT * FROM image_tags WHERE source='manual'")),1)

    def test_root_failure_and_source_overlap_are_rejected(self):
        with patch.object(database,"GALLERY_ROOT",self.root/"unavailable"):
            with self.assertRaises(OSError):database.scan_library()
        for source in (self.root,self.root.parent,self.root/"artist"):
            with self.assertRaises(ValueError):validate_source(self.root,source)
        from gallery_service import artist_directory
        for folder in ("..",".",".tag_gallery"):
            with self.assertRaises(ValueError):artist_directory(self.root,folder)


class TransactionTests(GalleryCase):
    def test_retry_discards_only_rolled_back_cache_entries(self):
        self.image();database.scan_library();conn=database.get_db();cache={"already-committed":999};attempt=[0]
        def write(c):
            attempt[0]+=1;tid=database.ensure_tag(c,cache,"retry-tag","general")
            if attempt[0]==1:raise sqlite3.OperationalError("database is locked")
            c.execute("INSERT INTO image_tags(image_id,tag_id) VALUES(1,?)",(tid,))
        try:
            with patch("database.time.sleep"):database.run_db_write_with_retry(conn,write,max_retries=2)
            self.assertEqual(attempt[0],2);self.assertEqual(cache["already-committed"],999)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_tags WHERE tag_id=?",(cache["retry-tag"],)).fetchone()[0],1)
        finally:conn.close()

    def test_incremental_counts_do_not_touch_unrelated_tags(self):
        self.image();database.scan_library();conn=database.get_db()
        with conn:
            conn.execute("INSERT INTO tags(name,post_count) VALUES('unrelated',123)")
            tid=database.ensure_tag(conn,{},"changed","general")
            conn.execute("INSERT INTO image_tags(image_id,tag_id) VALUES(1,?)",(tid,))
            database.refresh_tag_counts(conn)
        self.assertEqual(conn.execute("SELECT post_count FROM tags WHERE name='unrelated'").fetchone()[0],123)
        self.assertEqual(conn.execute("SELECT post_count FROM tags WHERE id=?",(tid,)).fetchone()[0],1);conn.close()

    def test_recent_tags_match_full_aggregate_including_timestamp_ties(self):
        from query_cache import newest_tag_rows
        conn=database.get_db()
        with conn:
            for iid in range(1,701):
                conn.execute("INSERT INTO images(id,rel_path,filename,ext,mtime) VALUES(?,?,?,'.png',?)",(iid,f'{iid}.png',f'{iid}.png',100 if iid<600 else 200))
            for tid in range(1,81):
                conn.execute("INSERT INTO tags(id,name,post_count) VALUES(?,?,1)",(tid,f'tag-{tid}'))
                conn.executemany("INSERT INTO image_tags(image_id,tag_id) VALUES(?,?)",[((tid*7+i)%700+1,tid) for i in range(4)])
        baseline=conn.execute("SELECT t.id,MAX(i.mtime) AS latest_mtime FROM tags t JOIN image_tags it ON t.id=it.tag_id JOIN images i ON i.id=it.image_id GROUP BY t.id ORDER BY latest_mtime DESC,t.id ASC LIMIT 30").fetchall()
        actual=newest_tag_rows(conn,30)
        self.assertEqual([(r['id'],r['latest_mtime']) for r in actual],[(r['id'],r['latest_mtime']) for r in baseline]);conn.close()

    def test_query_cache_invalidates_after_another_connection_commits(self):
        from query_cache import QueryCache
        cache=QueryCache(capacity=2);reads=[]
        def read():reads.append(1);return len(self.rows('SELECT id FROM images'))
        self.assertEqual(cache.get('count',read),0);self.assertEqual(cache.get('count',read),0)
        conn=database.get_db()
        with conn:conn.execute("INSERT INTO images(rel_path,filename,ext) VALUES('one.png','one.png','.png')")
        conn.close();self.assertEqual(cache.get('count',read),1);self.assertEqual(len(reads),2)

    def test_explicit_invalidation_does_not_join_or_republish_an_old_load(self):
        from query_cache import QueryCache
        cache = QueryCache()
        started, release = threading.Event(), threading.Event()
        old = []
        def slow():
            started.set()
            if not release.wait(3):
                raise RuntimeError("test load timed out")
            return "old"
        thread = threading.Thread(target=lambda: old.append(cache.get("list", slow)))
        with patch("query_cache.database_version", return_value="unchanged"):
            thread.start()
            self.assertTrue(started.wait(2))
            try:
                cache.clear()
                self.assertEqual(cache.get("list", lambda: "fresh"), "fresh")
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(old, ["old"])
            self.assertEqual(cache.get("list", lambda: "unexpected"), "fresh")


class FileRecoveryTests(GalleryCase):
    def prepare(self):
        self.image("a.png");self.image("b.png");database.scan_library();self.manual(1)
        return [{"id":1,"old_rel_path":"a.png","new_rel_path":"new_a.png"},{"id":2,"old_rel_path":"b.png","new_rel_path":"new_b.png"}]

    def test_phase_two_failure_restores_files_database_and_tags(self):
        entries=self.prepare();rename=os.rename
        def fail(source,dest):
            if Path(dest).name=="new_b.png":raise OSError("simulated phase 2 failure")
            return rename(source,dest)
        with patch.object(file_operations.os,"rename",side_effect=fail):
            with self.assertRaises(OSError):file_operations.execute(self.root,self.data,database.get_db,entries)
        self.assertTrue((self.root/"a.png").exists());self.assertTrue((self.root/"b.png").exists())
        self.assertFalse((self.root/"new_a.png").exists())
        self.assertEqual([r["rel_path"] for r in self.rows("SELECT rel_path FROM images ORDER BY id")],["a.png","b.png"])
        self.assertEqual(len(self.rows("SELECT * FROM image_tags WHERE source='manual'")),1)
        journal=json.loads(next((self.data/"file_journals").glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(journal["status"],"rolled_back")

    def test_database_failure_rolls_back_completed_filesystem_batch(self):
        entries=self.prepare()
        with patch.object(database,"run_db_write_with_retry",side_effect=sqlite3.OperationalError("simulated write failure")):
            with self.assertRaises(sqlite3.OperationalError):file_operations.execute(self.root,self.data,database.get_db,entries)
        self.assertTrue((self.root/"a.png").exists());self.assertFalse((self.root/"new_a.png").exists())

    def test_process_crash_recovery_uses_manifest_even_without_events(self):
        entries=self.prepare();entry=entries[0];entry["identity"]=file_operations._signature(self.root/"a.png");entry["temp_rel_path"]="__staged.png"
        (self.root/"a.png").rename(self.root/entry["temp_rel_path"])
        manifest=self.data/"file_journals"/"crash.json"
        work.atomic_json(manifest,{"id":"crash","kind":"rename","status":"prepared","entries":[entry]})
        result=file_operations.recover(self.root,self.data,database.get_db)
        self.assertEqual(result["errors"],[]);self.assertTrue((self.root/"a.png").exists())

    def test_swapping_names_preserves_database_identity(self):
        entries=self.prepare();entries[0]["new_rel_path"]="b.png";entries[1]["new_rel_path"]="a.png"
        file_operations.execute(self.root,self.data,database.get_db,entries)
        self.assertEqual([r["rel_path"] for r in self.rows("SELECT rel_path FROM images ORDER BY id")],["b.png","a.png"])

    def test_cross_volume_import_keeps_source_until_database_commit(self):
        source=self.root.parent/"downloads";source.mkdir();original=source/"source.png";original.write_bytes(b'original-content')
        real_stat=Path.stat
        class DeviceStat:
            def __init__(self,value):self.value=value;self.st_dev=value.st_dev+1
            def __getattr__(self,name):return getattr(self.value,name)
        def stat(path,*args,**kwargs):
            value=real_stat(path,*args,**kwargs)
            return DeviceStat(value) if path==self.root else value
        entries=[{"old_rel_path":"source.png","new_rel_path":"imported.png"}]
        from database import run_db_write_with_retry as write
        def inspect_commit(conn,callback,*args,**kwargs):
            self.assertTrue(list(source.glob('__gallery_*')),"Source remains staged until DB commit")
            return write(conn,callback,*args,**kwargs)
        with patch.object(Path,'stat',stat),patch.object(database,'run_db_write_with_retry',side_effect=inspect_commit):
            file_operations.execute(self.root,self.data,database.get_db,entries,kind='import',source_root=source)
        self.assertEqual((self.root/'imported.png').read_bytes(),b'original-content')
        self.assertEqual(list(source.iterdir()),[])

    def test_cancel_during_file_batch_rolls_back(self):
        entries=self.prepare();original_update=file_operations.update_job
        def cancel(**values):
            work.request_cancel(work.background_status()['id'])
            return original_update(**values)
        with patch.object(file_operations,'update_job',side_effect=cancel):
            result=file_operations.execute(self.root,self.data,database.get_db,entries)
        self.assertEqual(result['status'],'cancelled')
        self.assertTrue((self.root/'a.png').exists());self.assertTrue((self.root/'b.png').exists())

    def test_rollback_does_not_delete_another_hard_link(self):
        first=self.image('a.png')
        try:os.link(first,self.root/'b.png')
        except OSError:self.skipTest('Hard links unavailable')
        database.scan_library()
        entries=[{'id':1,'old_rel_path':'a.png','new_rel_path':'b.png'},{'id':2,'old_rel_path':'b.png','new_rel_path':'a.png'}]
        rename=os.rename
        def fail(source,dest):
            if Path(source).name=='b.png':raise OSError('fail second staging operation')
            return rename(source,dest)
        with patch.object(file_operations.os,'rename',side_effect=fail):
            with self.assertRaises(OSError):file_operations.execute(self.root,self.data,database.get_db,entries)
        self.assertTrue((self.root/'a.png').is_file());self.assertTrue((self.root/'b.png').is_file())


class IntegrationTests(GalleryCase):
    def test_policy_is_live_and_export_does_not_include_secrets(self):
        self.assertTrue(integrations.allowed("gallery_batch_tag_or_move"))
        integrations.save({"mcp_read_only":True})
        self.assertFalse(integrations.allowed("gallery_batch_tag_or_move"));self.assertTrue(integrations.allowed("gallery_get_overview"))
        config=integrations.client_config()["mcpServers"]["gallery"]
        self.assertFalse(config["trust"]);self.assertNotIn("gallery_batch_tag_or_move",config["includeTools"])
        with patch.dict(os.environ,{"GEMINI_API_KEY":"not-for-public-output"}):
            self.assertNotIn("not-for-public-output",json.dumps(integrations.public_config()))
        integrations.save({"mcp_enabled":False})
        with self.assertRaises(PermissionError):integrations.require_tool("gallery_get_overview")

    def test_legacy_api_credentials_are_ignored_and_cannot_be_saved(self):
        integrations.config_path().write_text(json.dumps({"mcp_read_only":True,"gemini_key_protected":"retired-secret","gemini_model":"old-model"}))
        self.assertTrue(integrations.load()["mcp_read_only"])
        self.assertNotIn("retired-secret",json.dumps(integrations.load()))
        self.assertNotIn("gemini_model",integrations.public_config())
        with self.assertRaises(ValueError):integrations.save({"api_key":"do-not-save"})
        integrations.save({"mcp_enabled":True})
        self.assertNotIn("retired-secret",integrations.config_path().read_text())

    def test_antigravity_export_uses_native_policy_fields(self):
        integrations.save({"mcp_read_only":True})
        config=integrations.client_config("antigravity")["mcpServers"]["gallery"]
        self.assertIn("gallery_scan_library",config["disabledTools"])
        self.assertNotIn("gallery_get_overview",config["disabledTools"])
        self.assertNotIn("trust",config);self.assertNotIn("includeTools",config)
        for client in ("antigravity","gemini_cli"):
            exported=integrations.client_config(client)["mcpServers"]
            for server_name in exported:
                self.assertLessEqual(max(len(f"mcp_{server_name}_{name}") for name in integrations.TOOLS),64)
        integrations.save({"mcp_enabled":False})
        self.assertTrue(integrations.client_config("antigravity")["mcpServers"]["gallery"]["disabled"])

    def test_mcp_handshake_respects_policy(self):
        integrations.save({"mcp_read_only":True})
        result=integrations.diagnose_mcp();self.assertTrue(result["ok"],result)
        self.assertEqual(result["tool_count"],10)

    def test_backup_can_be_read_and_has_original_manual_tags(self):
        self.image();database.scan_library();self.manual(1)
        from backup_service import backup
        result=backup();conn=sqlite3.connect(self.data/'backups'/result['filename'])
        try:
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_tags WHERE source='manual'").fetchone()[0],1)
        finally:conn.close()


class ModelGateTests(unittest.TestCase):
    def test_inference_and_unload_never_overlap(self):
        from model_control import operation,snapshot
        entered=threading.Event();release=threading.Event();unloaded=threading.Event()
        @operation("inference")
        def inference():entered.set();release.wait(2)
        @operation("unloading")
        def unload():unloaded.set()
        one=threading.Thread(target=inference);two=threading.Thread(target=unload)
        one.start();self.assertTrue(entered.wait(1));two.start();self.assertFalse(unloaded.wait(0.05))
        self.assertTrue(snapshot()["busy"]);release.set();one.join(2);two.join(2)
        self.assertTrue(unloaded.is_set());self.assertFalse(snapshot()["busy"])


class ProcessingStateTests(GalleryCase):
    def ai_module(self):
        module=importlib.import_module("ai_tagger")
        self.stack.enter_context(patch.object(module,"GALLERY_ROOT",self.root))
        self.stack.enter_context(patch.object(module,"MODEL_DIR",self.data/"models"))
        self.stack.enter_context(patch.object(module,"log_operation",return_value=0))
        return module

    def test_pixiv_timeout_remains_retryable_without_checked_marker(self):
        self.image("12345678_p0.png");database.scan_library();module=self.ai_module()
        import requests
        with patch("reverse_search.search_danbooru_by_pixiv_ids_batch",return_value={}),patch("reverse_search.search_danbooru_by_md5_batch",return_value={}),patch("reverse_search.save_persistent_artist_cache"),patch.object(requests,"get",side_effect=requests.Timeout("timeout")) as network:
            module.lookup_pixiv_artists_batch(enable_google_web_fallback=False)
            first=network.call_count
            module.lookup_pixiv_artists_batch(enable_google_web_fallback=False)
            self.assertEqual(network.call_count,first,"backoff prevents immediate repeated requests")
        row=self.rows("SELECT status,retry_at FROM image_processing WHERE stage='pixiv'")[0]
        self.assertEqual(row["status"],"failed");self.assertGreater(row["retry_at"],time.time())
        self.assertFalse(self.rows("SELECT * FROM image_tags WHERE source='pixiv_checked'"))
        self.assertEqual(self.rows("SELECT ai_tagged FROM images")[0]["ai_tagged"],0)

    def test_decode_failure_does_not_mark_visual_analysis_complete(self):
        self.image();database.scan_library();module=self.ai_module();engine=module.AITaggerEngine()
        with patch.object(engine,"ensure_model_loaded"),patch.object(module,"preprocess_image_fast",return_value=None):
            engine._batch_worker("all_untagged",0,0.38,0.65,run_reverse_search_calibrate=False,run_semantic_themes=False,auto_unload=False)
        row=self.rows("SELECT status,last_error FROM image_processing WHERE stage='ai'")[0]
        self.assertEqual(row["status"],"failed");self.assertTrue(row["last_error"])
        self.assertEqual(self.rows("SELECT ai_tagged FROM images")[0]["ai_tagged"],0)

    def test_job_progress_and_terminal_state_are_persistent(self):
        lease=work.acquire_job("test durable task")
        with lease.activate():
            lease.publish(force=True,stage="network",processed_count=3,total_target=10,recipe={"kind":"ai","options":{}})
            self.assertEqual(work.background_status()["processed_count"],3)
            self.assertEqual(work.job_history()[0]["stage"],"network")
        self.assertEqual(work.job_history()[0]["status"],"succeeded")
        self.assertFalse(work.background_status())

    def test_cooperative_stop_preserves_cancelled_status(self):
        @work.exclusive_job("cancel test")
        def run():
            active=work.background_status()
            work.request_cancel(active["id"])
            work.update_job(stage="must stop before publishing")
            self.fail("cancelled job continued")
        self.assertEqual(run()["status"],"cancelled")
        self.assertEqual(work.job_history()[0]["status"],"cancelled")


if __name__=="__main__":unittest.main()
