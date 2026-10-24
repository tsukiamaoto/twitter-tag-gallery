"""Tools are exercised on disposable galleries; network/model work is stubbed."""
import io
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

import anyio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_reliability import GalleryCase
import background_work as work
import integration_settings as integrations
import tool_registry as registry
import tool_runtime as runtime
from management_api import router
from mcp_server import build_server, run_stdio_loop


class ToolManagementTests(GalleryCase):
    def client(self):
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def wait_run(self, run_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = runtime.get_run(run_id)
            if value["status"] != "running":
                return value
            time.sleep(0.01)
        self.fail("Tool did not complete")

    def test_catalog_sdk_and_fallback_have_identical_tools_and_schemas(self):
        self.assertIn("gallery_confirm_artist_identity", registry.CATALOG)
        async def sdk_tools():
            return [tool.model_dump(by_alias=True) for tool in await build_server().list_tools()]
        sdk = anyio.run(sdk_tools)
        output = io.StringIO()
        run_stdio_loop(io.StringIO(json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/list"})+"\n"), output)
        fallback = json.loads(output.getvalue())["result"]["tools"]
        self.assertEqual({tool["name"] for tool in sdk}, set(registry.CATALOG))
        for tool in fallback:
            self.assertEqual(tool["inputSchema"], registry.CATALOG[tool["name"]].schema())
            self.assertEqual(tool["inputSchema"], next(item for item in sdk if item["name"] == tool["name"])["inputSchema"])
        for tool in registry.CATALOG.values():
            tool.validate(tool.example)

    def test_schema_validation_rejects_invalid_values_without_echoing_input(self):
        cases = [("gallery_get_overview", {"api_key":"do-not-echo-this"}),
                 ("gallery_reverse_image_search", {"image_id":True}),
                 ("gallery_find_tag_correlations", {"target_tag":"a","top_k":100000}),
                 ("gallery_deep_reverse_search_batch", {"scope":"invalid"}),
                 ("gallery_calibrate_image_artist_and_tags", {"calibrations":[{"image_id":-1}]})]
        for name, arguments in cases:
            with self.subTest(name=name), self.assertRaises(ValueError) as error:
                registry.CATALOG[name].validate(arguments)
            self.assertNotIn("do-not-echo-this", str(error.exception))

    def test_settings_policy_and_validation_are_enforced_before_handler_runs(self):
        with self.client() as client, patch.object(registry.Tool, "handler") as handler:
            integrations.save({"mcp_read_only":True})
            response = client.post("/api/settings/mcp/tools/gallery_reverse_image_search/run",json={"arguments":{"image_id":1}})
            self.assertEqual(response.status_code,403)
            integrations.save({"disabled_tools":["gallery_get_overview"]})
            self.assertEqual(client.post("/api/settings/mcp/tools/gallery_get_overview/run",json={"arguments":{}}).status_code,403)
            self.assertEqual(client.post("/api/settings/mcp/tools/unknown/run",json={"arguments":{}}).status_code,404)
            self.assertEqual(client.post("/api/settings/mcp/tools/gallery_find_tag_correlations/run",json={"arguments":{"top_k":-1}}).status_code,422)
            handler.assert_not_called()

    def test_direct_gemini_endpoints_are_retired_without_network_requests(self):
        import app
        test_app=FastAPI();test_app.include_router(router)
        test_app.add_api_route('/api/semantic/gemini_flash',app.api_semantic_gemini_flash,methods=['POST'])
        with TestClient(test_app) as client,patch('requests.get') as get,patch('requests.post') as post:
            self.assertEqual(client.post('/api/settings/gemini/test').status_code,410)
            self.assertEqual(client.post('/api/semantic/gemini_flash',json={'prompt_task':'translate','api_key':'unused'}).status_code,410)
            self.assertEqual(client.post('/api/settings/integrations',json={'api_key':'unused'}).status_code,422)
            get.assert_not_called();post.assert_not_called()
            config=client.get('/api/settings/integrations').json()
            self.assertNotIn('gemini_model',config);self.assertNotIn('key_configured',config)

    def test_background_settings_execution_retains_result_and_does_not_log_arguments(self):
        entered, release = threading.Event(), threading.Event()
        def handler(**arguments):
            entered.set()
            release.wait(3)
            return {"ok":True, "matches":2}
        with self.client() as client, patch.object(registry.Tool,"handler",return_value=handler):
            response = client.post("/api/settings/mcp/tools/gallery_semantic_search/run",json={"arguments":{"query":"private-input"}})
            self.assertEqual(response.status_code,202)
            run_id = response.json()["id"]
            self.assertTrue(entered.wait(1))
            self.assertEqual(client.get("/api/settings/mcp/runs/"+run_id).json()["run"]["status"],"running")
            release.set()
            value = self.wait_run(run_id)
        self.assertEqual(value["result"],{"ok":True,"matches":2})
        self.assertEqual(value["source"],"settings")
        self.assertNotIn("private-input",json.dumps(value))
        self.assertNotIn("result",runtime.history()[0])

    def test_write_tools_share_job_lock_and_support_cooperative_stop(self):
        entered, release = threading.Event(), threading.Event()
        def handler(**arguments):
            entered.set()
            release.wait(3)
            work.update_job(stage="測試取消")
            return {"done":True}
        with self.client() as client, patch.object(registry.Tool,"handler",return_value=handler):
            started = runtime.start("gallery_purge_all_low_conf_ai_artists",{})
            self.assertTrue(entered.wait(1))
            self.assertEqual(work.background_status()["id"],started["job_id"])
            try:
                with self.assertRaises(work.BackgroundBusy):
                    runtime.start("gallery_auto_organize_artist_folders",{})
                response=client.post("/api/settings/mcp/tools/gallery_auto_organize_artist_folders/run",json={"arguments":{}})
                self.assertEqual(response.status_code,409)
                work.request_cancel(started["job_id"])
            finally:
                release.set()
            value = self.wait_run(started["id"])
        self.assertEqual(value["status"],"cancelled")
        self.assertEqual(work.background_status(),{})

    def test_read_concurrency_is_bounded_and_does_not_block_write_lock(self):
        release = threading.Event()
        with patch.object(registry.Tool,"handler",return_value=lambda **kwargs:release.wait(3)):
            runs = [runtime.start("gallery_get_overview",{}) for _ in range(2)]
            try:
                with self.assertRaises(runtime.ToolBusy):runtime.start("gallery_get_overview",{})
                lease=work.acquire_job("unrelated write");lease.release()
            finally:release.set()
            for run in runs:self.assertEqual(self.wait_run(run["id"])["status"],"succeeded")

    def test_completed_state_waits_for_global_lock_cleanup(self):
        entered, release = threading.Event(), threading.Event()
        original=work.JobLease.release
        def slow_release(lease):
            entered.set();release.wait(3);return original(lease)
        with patch.object(work.JobLease,"release",slow_release),patch.object(registry.Tool,"handler",return_value=lambda **kwargs:{}):
            run=runtime.start("gallery_purge_all_low_conf_ai_artists",{})
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(runtime.get_run(run["id"])["status"],"running")
                self.assertEqual(work.background_status()["id"],run["job_id"])
            finally:release.set()
            self.assertEqual(self.wait_run(run["id"])["status"],"succeeded")
            lease=work.acquire_job("next operation");lease.release()

    def test_sdk_and_fallback_calls_use_same_policy_locks_and_audit(self):
        async def call():
            return await build_server().call_tool("gallery_get_overview",{})
        with patch.object(registry.Tool,"handler",return_value=lambda **kwargs:{"count":3}):
            result=anyio.run(call)
            content=result.content if hasattr(result,"content") else result
            self.assertIn('"count": 3',content[0].text)
            self.assertEqual(runtime.history()[0]["source"],"mcp")
            integrations.save({"mcp_enabled":False})
            try:
                result=anyio.run(call)
            except Exception as error:
                self.assertIn("停用",str(error))
            else:
                self.assertTrue(result.is_error)
            output=io.StringIO()
            run_stdio_loop(io.StringIO(json.dumps({"id":2,"method":"tools/call","params":{"name":"gallery_get_overview","arguments":{}}})+"\n"),output)
            self.assertTrue(json.loads(output.getvalue())["result"]["isError"])

    def test_stdio_protocol_executes_a_read_tool_and_returns_validation_errors(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env={**os.environ,"TWITTER_GALLERY_ROOT":str(self.root),"TWITTER_GALLERY_MCP_DIAGNOSTIC":"1","PYTHONIOENCODING":"utf-8"}
        parameters=StdioServerParameters(command=sys.executable,args=["-B",str(Path(__file__).resolve().parents[1]/"mcp_server.py")],env=env)
        async def protocol():
            with anyio.fail_after(15):
                async with stdio_client(parameters) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        listed=await session.list_tools()
                        self.assertEqual(len(listed.tools),len(registry.CATALOG))
                        result=await session.call_tool("gallery_get_overview",{})
                        self.assertEqual(json.loads(result.content[0].text)["total_images"],0)
                        invalid=await session.call_tool("gallery_get_overview",{"unexpected":True})
                        error_flag=getattr(invalid,"is_error",getattr(invalid,"isError",False))
                        self.assertTrue(error_flag)
        anyio.run(protocol)

    def test_actual_overview_call_reads_disposable_gallery(self):
        value=runtime.invoke("gallery_get_overview",{})
        self.assertEqual(value["total_images"],0)
        self.assertEqual(runtime.history()[0]["status"],"succeeded")

    def test_failed_run_result_can_be_read_without_triggering_api_error(self):
        with self.client() as client, patch.object(registry.Tool,"handler",return_value=lambda **kwargs:1/0):
            run=runtime.start("gallery_get_overview",{})
            value=self.wait_run(run["id"])
            self.assertEqual(value["status"],"failed")
            response=client.get("/api/settings/mcp/runs/"+run["id"])
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()["run"]["error"],"division by zero")
        with patch.object(registry.Tool,"handler",return_value=lambda **kwargs:{"found":False,"error":"Image not found"}):
            run=runtime.start("gallery_reverse_image_search",{"image_id":1})
            value=self.wait_run(run["id"])
            self.assertEqual(value["status"],"failed")
            self.assertEqual(value["error"],"Image not found")

    def test_results_and_history_are_bounded_and_interrupted_work_is_identified(self):
        with patch.object(registry.Tool,"handler",return_value=lambda **kwargs:{"text":'"\\中'*200000}):
            runtime.invoke("gallery_get_overview",{})
        value=runtime.get_run(runtime.history()[0]["id"])
        self.assertTrue(value["truncated"])
        self.assertLessEqual(len(json.dumps(value["result"],ensure_ascii=False).encode("utf-8")),runtime.RESULT_BYTES)
        prepared=runtime._prepare("gallery_get_overview",{},"settings")
        prepared[3].release();runtime._read_slots.release()
        self.assertEqual(runtime.get_run(prepared[2])["status"],"interrupted")
        with patch.object(runtime,"HISTORY_LIMIT",3),patch.object(registry.Tool,"handler",return_value=lambda **kwargs:{}):
            for _ in range(4):runtime.invoke("gallery_get_overview",{})
        self.assertEqual(len(runtime.history(50)),3)
