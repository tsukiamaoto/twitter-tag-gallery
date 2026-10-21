"""Exercise real MCP workflows on temporary images; AI inference is stubbed."""
import io
import json
from pathlib import Path
import threading
import time
from unittest.mock import patch

import anyio
import database
import background_work as work
import integration_settings as integrations
from processing_state import record
from test_reliability import GalleryCase
import tool_registry as registry
import tool_runtime as runtime
import gallery_workflow_tools as workflows
from mcp_server import build_server, run_stdio_loop


class WorkflowToolTests(GalleryCase):
    def wait_run(self, run_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = runtime.get_run(run_id)
            if value["status"] != "running":
                return value
            time.sleep(0.01)
        self.fail("Workflow did not complete")

    def scanned(self, *names):
        for name in names:self.image(name)
        database.scan_library()
        return {row["rel_path"]:row["id"] for row in self.rows("SELECT id,rel_path FROM images")}

    def test_scan_dispatch_and_result_polling_do_not_pollute_history(self):
        self.image("風景.png")
        started = runtime.dispatch("gallery_scan_library",{})
        self.assertEqual(started["run_id"],started["id"])
        value=self.wait_run(started["run_id"])
        self.assertEqual(value["status"],"succeeded")
        self.assertEqual(value["result"]["new_added"],1)
        for _ in range(3):
            result=runtime.dispatch("gallery_get_tool_run",{"run_id":started["id"]})
            self.assertEqual(result["run"]["result"]["total_images"],1)
            runtime.dispatch("gallery_get_jobs",{})
        self.assertEqual(len(runtime.history()),1)
        self.assertEqual(runtime.history()[0]["source"],"mcp")

    def test_search_uses_shared_query_with_tags_sort_pagination_and_missing_filter(self):
        ids=self.scanned("風景_10.png","風景_2.png","artist/other.png")
        runtime.invoke("gallery_update_image_tags",{"image_ids":[ids["風景_2.png"],ids["風景_10.png"]],"add_tags":[{"name":"landscape","category":"copyright"}]})
        result=runtime.invoke("gallery_search_images",{"include_tags":["landscape"],"q":"風景","sort":"natural_asc","page_size":1})
        self.assertEqual(result["total"],2)
        self.assertEqual(result["items"][0]["filename"],"風景_2.png")
        second=runtime.invoke("gallery_search_images",{"include_tags":["landscape"],"sort":"natural_asc","page":2,"page_size":1})
        self.assertEqual(second["items"][0]["filename"],"風景_10.png")
        conn=database.get_db()
        try:
            with conn:conn.execute("UPDATE images SET missing_since=1 WHERE id=?",(ids["風景_2.png"],))
        finally:conn.close()
        self.assertEqual(runtime.invoke("gallery_search_images",{"include_tags":["landscape"]})["total"],1)
        self.assertEqual(runtime.invoke("gallery_search_images",{"exclude_tags":["landscape"],"folder":"artist"})["total"],1)

    def test_search_and_web_results_are_identical(self):
        self.scanned("a_10.png","a_2.png")
        import app
        options=dict(include_tags="",exclude_tags="",folder="__ALL__",media_type="all",ai_status="all",q="a_",sort="natural_asc",page=1,page_size=10)
        with patch.object(app.thumbnail_cache,"prefetch"):
            web=app.api_images(**options)
        self.assertEqual(web,workflows.tool_gallery_search_images(**options))

    def test_model_tag_edit_is_atomic_preserves_manual_tags_and_recounts(self):
        ids=self.scanned("a.png","b.png");a,b=ids["a.png"],ids["b.png"]
        self.manual(a)
        result=runtime.invoke("gallery_update_image_tags",{"image_ids":[a,a,b],"add_tags":[{"name":"new_tag","category":"character"}]})
        self.assertEqual(result["updated"],2)
        self.assertEqual(self.rows("SELECT post_count FROM tags WHERE name='new_tag'")[0]["post_count"],2)
        with self.assertRaises(ValueError):
            runtime.invoke("gallery_update_image_tags",{"image_ids":[a,999999],"add_tags":[{"name":"must_not_exist","category":"character"}]})
        self.assertEqual(self.rows("SELECT id FROM tags WHERE name='must_not_exist'"),[])
        runtime.invoke("gallery_update_image_tags",{"image_ids":[a],"remove_tag_names":["new_tag"]})
        self.assertEqual(self.rows("SELECT post_count FROM tags WHERE name='new_tag'")[0]["post_count"],1)
        self.assertEqual(len(self.rows("SELECT * FROM image_tags it JOIN tags t ON t.id=it.tag_id WHERE it.image_id=? AND t.name='keep-manual'",a)),1)

    def test_detail_returns_processing_state_and_bounded_tags(self):
        iid=self.scanned("a.png")["a.png"]
        runtime.invoke("gallery_update_image_tags",{"image_ids":[iid],"add_tags":[{"name":"one","category":"character"},{"name":"two","category":"copyright"}]})
        conn=database.get_db()
        try:
            with conn:record(conn,iid,"ai","failed",error="decode error",retry_at=123)
        finally:conn.close()
        detail=runtime.invoke("gallery_get_image_details",{"image_id":iid,"tag_limit":1})
        self.assertTrue(detail["tags_truncated"])
        self.assertEqual(len(detail["tags"]),1)
        self.assertEqual(detail["processing"][0]["last_error"],"decode error")
        self.assertEqual(Path(detail["full_path"]),self.root/"a.png")

    def test_move_preview_and_execution_share_safe_plan_and_preserve_manual_tags(self):
        iid=self.scanned("a.png")["a.png"];self.manual(iid)
        request={"image_ids":[iid],"target_folder":"新畫師","auto_rename":False}
        preview=self.wait_run(runtime.dispatch("gallery_move_images",request)["id"])
        self.assertTrue(preview["result"]["preview"])
        self.assertTrue((self.root/"a.png").exists())
        self.assertFalse((self.root/"新畫師").exists())
        actual=self.wait_run(runtime.dispatch("gallery_move_images",{**request,"execute_move":True})["id"])
        self.assertEqual(actual["result"]["moved"],1)
        self.assertTrue((self.root/"新畫師/a.png").exists())
        self.assertEqual(self.rows("SELECT rel_path FROM images WHERE id=?",iid)[0]["rel_path"],"新畫師/a.png")
        self.assertEqual(len(self.rows("SELECT * FROM image_tags WHERE image_id=? AND source='manual'",iid)),1)

    def test_move_rejects_parent_paths_and_missing_ids(self):
        iid=self.scanned("a.png")["a.png"]
        for request in ({"image_ids":[iid],"target_folder":".."},{"image_ids":[iid,999999],"target_folder":"artist"}):
            value=self.wait_run(runtime.dispatch("gallery_move_images",{**request,"execute_move":True})["id"])
            self.assertEqual(value["status"],"failed")
            self.assertTrue((self.root/"a.png").exists())

    def test_ai_runs_once_and_skip_uses_file_and_model_versions(self):
        iid=self.scanned("a.png")["a.png"];self.manual(iid)
        import ai_tagger
        with patch.object(ai_tagger,"preprocess_image_fast",return_value=object()),patch.object(ai_tagger.tagger_engine,"predict_single",return_value=([("sky","general",0.9)],None)) as predict:
            first=self.wait_run(runtime.dispatch("gallery_analyze_images",{"image_ids":[iid]})["id"])
            self.assertEqual(first["result"]["processed"],1)
            second=self.wait_run(runtime.dispatch("gallery_analyze_images",{"image_ids":[iid]})["id"])
            self.assertEqual(second["result"]["skipped"],[iid])
            self.assertEqual(predict.call_count,1)
            self.wait_run(runtime.dispatch("gallery_analyze_images",{"image_ids":[iid],"skip_completed":False})["id"])
            self.assertEqual(predict.call_count,2)
        self.assertEqual(len(self.rows("SELECT * FROM image_tags WHERE image_id=? AND source='manual'",iid)),1)
        self.assertEqual(self.rows("SELECT ai_tagged FROM images WHERE id=?",iid)[0]["ai_tagged"],1)

    def test_ai_decode_failure_is_visible_and_not_marked_complete(self):
        iid=self.scanned("a.png")["a.png"]
        import ai_tagger
        with patch.object(ai_tagger,"preprocess_image_fast",return_value=None):
            value=self.wait_run(runtime.dispatch("gallery_analyze_images",{"image_ids":[iid]})["id"])
        self.assertEqual(value["status"],"failed")
        self.assertEqual(value["result"]["errors"][0]["image_id"],iid)
        self.assertEqual(self.rows("SELECT ai_tagged FROM images WHERE id=?",iid)[0]["ai_tagged"],0)

    def test_cancel_tool_can_stop_an_owned_write_job_without_acquiring_it_again(self):
        entered,release=threading.Event(),threading.Event()
        def scan():
            entered.set();release.wait(3);work.update_job(stage="stop checkpoint");return {}
        with patch.object(workflows,"tool_gallery_scan_library",side_effect=scan):
            run=runtime.dispatch("gallery_scan_library",{})
            self.assertTrue(entered.wait(1))
            try:
                result=runtime.dispatch("gallery_cancel_job",{"job_id":run["job_id"]})
                self.assertEqual(result["status"],"stopping")
                self.assertEqual(runtime.dispatch("gallery_get_jobs",{})["active"]["id"],run["job_id"])
            finally:release.set()
            self.assertEqual(self.wait_run(run["id"])["status"],"cancelled")

    def test_recovery_reports_conflicts_and_success_without_silent_completion(self):
        value=self.wait_run(runtime.dispatch("gallery_recover_file_operations",{})["id"])
        self.assertEqual(value["result"]["errors"],[])
        with patch("file_operations.recover",return_value={"recovered":[],"errors":[{"error":"conflict"}]}):
            value=self.wait_run(runtime.dispatch("gallery_recover_file_operations",{})["id"])
        self.assertEqual(value["status"],"failed")

    def test_rename_undo_uses_existing_journal_and_list(self):
        iid=self.scanned("a.png")["a.png"]
        import renamer
        self.stack.enter_context(patch.object(renamer,"log_operation",return_value=0))
        for name,value in {"GALLERY_ROOT":self.root,"DATA_DIR":self.data,"RENAME_LOG_DIR":self.data/"rename_logs"}.items():
            self.stack.enter_context(patch.object(renamer,name,value))
        (self.data/"rename_logs").mkdir(exist_ok=True)
        from file_operations import execute
        result=execute(self.root,self.data,database.get_db,[{"id":iid,"old_rel_path":"a.png","new_rel_path":"b.png"}],kind="rename")
        listed=runtime.invoke("gallery_list_rename_batches",{})
        self.assertIn(result["operation_id"],[batch["batch_id"] for batch in listed["batches"]])
        undo=self.wait_run(runtime.dispatch("gallery_undo_rename",{"batch_id":result["operation_id"]})["id"])
        self.assertEqual(undo["result"]["restored"],1)
        self.assertTrue((self.root/"a.png").exists())

    def test_readonly_policy_blocks_new_writes_and_keeps_queries(self):
        integrations.save({"mcp_read_only":True})
        for name in ("gallery_scan_library","gallery_update_image_tags","gallery_analyze_images","gallery_move_images","gallery_recover_file_operations","gallery_undo_rename","gallery_cancel_job"):
            with self.subTest(name=name),self.assertRaises(PermissionError):runtime.dispatch(name,registry.CATALOG[name].example)
        self.assertEqual(runtime.dispatch("gallery_search_images",{})["total"],0)
        self.assertEqual(runtime.dispatch("gallery_get_jobs",{})["active"],{})

    def test_sdk_and_fallback_return_run_ids_for_long_tools(self):
        async def sdk():return await build_server().call_tool("gallery_scan_library",{})
        result=anyio.run(sdk)
        content=result.content if hasattr(result,"content") else result
        started=json.loads(content[0].text)
        self.assertEqual(self.wait_run(started["run_id"])["status"],"succeeded")
        output=io.StringIO()
        run_stdio_loop(io.StringIO(json.dumps({"id":1,"method":"tools/call","params":{"name":"gallery_scan_library","arguments":{}}})+"\n"),output)
        started=json.loads(json.loads(output.getvalue())["result"]["content"][0]["text"])
        self.assertEqual(self.wait_run(started["run_id"])["status"],"succeeded")
