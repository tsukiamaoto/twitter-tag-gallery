"""Preview scan progress, shared execution and read-only HTTP lifecycle."""
import threading
import time
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import dedup_scan_jobs as jobs
from management_api import router
from test_reliability import GalleryCase


class DedupScanJobTests(GalleryCase):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def finished(self, scan_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = jobs.get_scan(scan_id)
            if value["status"] not in ("queued", "running"):
                return value
            time.sleep(.01)
        self.fail("Scan did not finish")

    def test_same_scan_is_shared_and_progress_can_be_polled_while_running(self):
        entered, release = threading.Event(), threading.Event()
        def scan(**options):
            options["progress"](stage="檢查尺寸與快取", processed_count=12, total_target=100)
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timed out")
            return {"total_groups": 0, "groups": []}
        with patch.object(jobs, "scan_duplicates", side_effect=scan) as compute:
            first = self.client.post("/api/dedup/scans", json={"mode": "both"})
            self.assertEqual(first.status_code, 202)
            self.assertTrue(entered.wait(2))
            scan_id = first.json()["scan_id"]
            try:
                duplicate = self.client.post("/api/dedup/scans", json={"mode": "both"}).json()
                self.assertEqual(duplicate["scan_id"], scan_id)
                active = self.client.get("/api/dedup/scans/" + scan_id).json()
                self.assertEqual(active["status"], "running")
                self.assertEqual(active["progress"]["processed_count"], 12)
                self.assertNotIn("result", active)
            finally:
                release.set()
            done = self.finished(scan_id)
            self.assertEqual(done["status"], "completed")
            self.assertEqual(compute.call_count, 1)
            self.assertEqual(self.client.get("/api/dedup/scans/latest").json()["scan"]["scan_id"], scan_id)

    def test_failure_is_terminal_and_same_options_can_retry(self):
        with patch.object(jobs, "scan_duplicates", side_effect=ValueError("cannot decode")):
            first = self.client.post("/api/dedup/scans", json={}).json()
            failed = self.finished(first["scan_id"])
            self.assertEqual(failed["status"], "failed")
            self.assertIn("cannot decode", failed["error"])
        with patch.object(jobs, "scan_duplicates", return_value={"groups": []}):
            retry = self.client.post("/api/dedup/scans", json={}).json()
            self.assertNotEqual(retry["scan_id"], first["scan_id"])
            self.assertEqual(self.finished(retry["scan_id"])["status"], "completed")

    def test_settings_status_does_not_start_a_scan_and_input_is_validated(self):
        with patch.object(jobs, "scan_duplicates") as scan:
            self.assertEqual(self.client.get("/api/dedup/scans/latest").json(), {"scan": None})
            self.assertEqual(self.client.get("/api/dedup/scans/missing").status_code, 404)
            self.assertEqual(self.client.post("/api/dedup/scans", json={"mode": "similar"}).status_code, 422)
            self.assertEqual(self.client.post("/api/dedup/scans", json={"execute_delete": True}).status_code, 422)
            scan.assert_not_called()

    def test_real_preview_publishes_counts_and_never_deletes_files(self):
        image = self.image("original.png")
        duplicate = self.root / "copy.png"
        duplicate.write_bytes(image.read_bytes())
        import database
        database.scan_library()
        scan = self.client.post("/api/dedup/scans", json={}).json()
        done = self.finished(scan["scan_id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["result"]["total_duplicate_images"], 1)
        self.assertEqual(done["progress"]["processed_count"], 2)
        self.assertTrue(image.exists())
        self.assertTrue(duplicate.exists())
