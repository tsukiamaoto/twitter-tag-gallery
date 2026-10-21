"""Exercise the real reveal route without opening Explorer or starting gallery jobs."""
import ast
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from file_safety import gallery_path


class OpenExplorerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "test.db"
        with self.connect() as conn:
            conn.execute("CREATE TABLE images (id INTEGER PRIMARY KEY, rel_path TEXT)")
        self.launch = Mock()
        app = FastAPI()
        source = Path(__file__).resolve().parents[1] / "app.py"
        route = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                     if isinstance(node, ast.FunctionDef) and node.name == "api_open_explorer")
        namespace = {"app": app, "get_db": self.connect, "GALLERY_ROOT": self.root,
                     "gallery_path": gallery_path, "HTTPException": HTTPException,
                     "subprocess": SimpleNamespace(Popen=self.launch)}
        exec(compile(ast.Module(body=[route], type_ignores=[]), str(source), "exec"), namespace)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def connect(self):
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        return conn

    def add_image(self, relative):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"test image")
        conn = self.connect()
        try:
            with conn:
                conn.execute("INSERT INTO images VALUES (1, ?)", (relative,))
        finally:
            conn.close()
        return target.resolve()

    def test_selects_the_exact_file_with_spaces_unicode_and_punctuation(self):
        target = self.add_image("サインこす (sign cos)/作品 1,春&秋.png")
        response = self.client.post("/api/open_explorer/1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "opened", "path": str(target)})
        args = self.launch.call_args.args[0]
        # Explorer must receive the switch outside the quoted filename.
        self.assertEqual(subprocess.list2cmdline(args), f'explorer.exe /select, "{target}"')
        self.launch.assert_called_once()

    def test_root_image_is_selected_in_the_library_folder(self):
        target = self.add_image("root image.png")
        response = self.client.post("/api/open_explorer/1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.launch.call_args.args[0][-1], str(target))

    def test_unknown_or_missing_image_does_not_open_an_unrelated_folder(self):
        self.assertEqual(self.client.post("/api/open_explorer/999").status_code, 404)
        target = self.add_image("artist/missing.png")
        target.unlink()
        response = self.client.post("/api/open_explorer/1")
        self.assertEqual(response.status_code, 404)
        self.assertIn("圖片檔案不存在", response.json()["detail"])
        self.launch.assert_not_called()

    def test_explorer_launch_failure_is_reported(self):
        self.add_image("image.png")
        self.launch.side_effect = OSError("launch failed")
        response = self.client.post("/api/open_explorer/1")
        self.assertEqual(response.status_code, 503)
        self.assertIn("無法開啟檔案總管", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
