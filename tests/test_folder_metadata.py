"""Exercise the shipped folder query with an isolated SQLite database."""
import ast
import sqlite3
import unittest
from pathlib import Path


class FolderMetadataTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("CREATE TABLE images (folder TEXT, mtime REAL)")
        self.connection.executemany("INSERT INTO images VALUES (?, ?)", [
            ("青井", 100), ("青井", 300), ("春野", 200), ("", 900), ("不明", None),
        ])
        actual_connection = self.connection

        class Connection:
            def execute(self, *args):
                return actual_connection.execute(*args)

            def close(self):
                pass

        tree = ast.parse((Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8"))
        query = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "api_folders")
        query.decorator_list = []
        namespace = {"get_db": Connection}
        exec(compile(ast.Module(body=[query], type_ignores=[]), "app.py", "exec"), namespace)
        self.query = namespace["api_folders"]

    def tearDown(self):
        self.connection.close()

    def test_latest_image_time_is_returned_without_changing_counts(self):
        folders = {row["name"]: row for row in self.query()}
        self.assertNotIn("", folders)
        self.assertEqual(folders["青井"], {"name": "青井", "count": 2, "last_updated": 300})
        self.assertEqual(folders["春野"]["last_updated"], 200)
        self.assertEqual(folders["不明"]["last_updated"], 0)

    def test_newest_image_changes_metadata_and_count(self):
        self.connection.execute("INSERT INTO images VALUES ('春野', 500)")
        spring = next(row for row in self.query() if row["name"] == "春野")
        self.assertEqual(spring["last_updated"], 500)
        self.assertEqual(spring["count"], 2)


if __name__ == "__main__":
    unittest.main()
