"""Artist aliases share one sidebar row and one union of image matches."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import database
from gallery_artist_service import sidebar_tag_rows, recent_sidebar_tag_rows
from gallery_query import search_images
from query_cache import invalidate_gallery_queries
from zh_dict import get_tag_display_name, get_tag_zh


class ArtistTagsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, value in {"GALLERY_ROOT": root, "DATA_DIR": root / ".tag_gallery",
                            "THUMB_DIR": root / "thumbs", "MODEL_DIR": root / "models",
                            "DB_PATH": root / "gallery.db"}.items():
            patcher = patch.object(database, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        invalidate_gallery_queries()
        self.addCleanup(invalidate_gallery_queries)
        database.init_db()
        conn = database.get_db()
        with conn:
            conn.executemany("INSERT INTO images(id,rel_path,filename,ext,mtime) VALUES(?,?,?,?,?)",
                             [(i, f"{i}.png", f"{i}.png", ".png", i * 100) for i in range(1, 7)])
            conn.executemany("INSERT INTO tags(id,name,category,zh_name) VALUES(?,?,?,?)", [
                (1, "hara", "artist", "現有繪師：Hara"),
                (2, "hara_(harayutaka)", "artist", "現有繪師：Hara"),
                (3, "タカハル", "artist", ""),
                (4, "takaharu", "artist", "現有繪師：タカハル"),
                (5, "blue", "general", "藍色"),
                (6, "same-label", "general", "Hara"),
                (7, "Hara", "artist", ""),
                (8, "coro_tret_coro_fae", "artist", ""),
                (9, "coro_fae", "artist", "現有繪師：Coro Tret Coro Fae"),
            ])
            conn.executemany("INSERT INTO image_tags(image_id,tag_id) VALUES(?,?)", [
                (1, 1), (2, 1), (2, 2), (3, 2), (4, 2),
                (4, 3), (5, 3), (4, 4),
                (1, 5), (3, 5), (6, 6), (5, 8), (6, 9),
            ])
            database.refresh_tag_counts(conn)
        conn.close()

    def rows(self, category="artist", q="", limit=250, recent=False):
        conn = database.get_db()
        try:
            if recent:
                return recent_sidebar_tag_rows(conn, q, limit)
            return sidebar_tag_rows(conn, category, q, limit)
        finally:
            conn.close()

    def ids(self, **kwargs):
        return sorted(item["id"] for item in search_images(sort="mtime_desc", page_size=100, **kwargs)["items"])

    def test_dictionary_and_persisted_prefixes_show_only_artist_name(self):
        self.assertEqual(get_tag_zh("hara", "artist"), "Hara")
        self.assertEqual(get_tag_zh("coro_tret_coro_fae", "artist"), "Coro Tret Coro Fae")
        self.assertEqual(get_tag_display_name("unknown", "artist", "現有繪師：自訂繪師"), "自訂繪師")
        self.assertEqual(get_tag_display_name("hara", "artist", "現有繪師：Hara"), "Hara")
        self.assertEqual(get_tag_display_name("blue", "general", "藍色"), "藍色")

    def test_aliases_merge_before_limit_and_count_distinct_images(self):
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        hara = next(row for row in rows if row["zh_name"] == "Hara")
        self.assertEqual(hara["post_count"], 4)
        self.assertEqual(set(hara["aliases"]), {"hara", "hara_(harayutaka)", "Hara"})
        self.assertEqual(self.rows(limit=1)[0]["zh_name"], "Hara")
        self.assertEqual(len(self.rows(category="all")), 5)
        self.assertEqual(self.rows(category="general", q="Hara")[0]["name"], "same-label")

    def test_search_by_any_alias_or_display_name_returns_complete_group(self):
        for query in ("HARA", "harayutaka"):
            self.assertEqual([row["zh_name"] for row in self.rows(q=query)], ["Hara"])
        coro = self.rows(q="coro_fae", limit=1)[0]
        self.assertEqual(coro["zh_name"], "Coro Tret Coro Fae")
        self.assertEqual(coro["post_count"], 2)
        self.assertEqual(set(coro["aliases"]), {"coro_fae", "coro_tret_coro_fae"})

    def test_alias_filtering_is_union_within_artist_and_intersection_across_conditions(self):
        for alias in ("hara", "hara_(harayutaka)", "Hara"):
            self.assertEqual(self.ids(include_tags=alias), [1, 2, 3, 4])
        self.assertEqual(self.ids(include_tags="hara,blue"), [1, 3])
        self.assertEqual(self.ids(include_tags="hara,takaharu"), [4])
        self.assertEqual(self.ids(include_tags="hara,hara_(harayutaka)"), [1, 2, 3, 4])
        self.assertEqual(self.ids(exclude_tags="Hara"), [5, 6])
        self.assertEqual(self.ids(include_tags="hara", exclude_tags="hara_(harayutaka)"), [])
        self.assertEqual(self.ids(include_tags="same-label"), [6])
        self.assertEqual(self.ids(include_tags="unknown"), [])

    def test_recent_groups_use_latest_alias_time_and_searches_keep_all_aliases(self):
        rows = self.rows(recent=True, limit=3)
        self.assertEqual([row["zh_name"] for row in rows], ["Hara", "Coro Tret Coro Fae", "タカハル"])
        hara = self.rows(recent=True, q="harayutaka", limit=1)[0]
        self.assertEqual(hara["post_count"], 4)
        self.assertEqual(hara["latest_mtime"], 400)
        self.assertEqual(len(hara["aliases"]), 3)

    def test_recent_fallback_groups_before_applying_limit(self):
        conn = database.get_db()
        with conn:
            conn.executemany("INSERT INTO images(id,rel_path,filename,ext,mtime) VALUES(?,?,?,?,?)",
                             [(i, f"{i}.png", f"{i}.png", ".png", 1000 + i) for i in range(7, 530)])
            conn.executemany("INSERT INTO image_tags(image_id,tag_id) VALUES(?,1)", [(i,) for i in range(7, 530)])
            database.refresh_tag_counts(conn)
        conn.close()
        invalidate_gallery_queries()
        rows = self.rows(recent=True, limit=4)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["zh_name"], "Hara")
        self.assertEqual(rows[0]["post_count"], 527)
        self.assertEqual(len({row["id"] for row in rows}), 4)

    def test_manual_edit_invalidation_refreshes_group_count_and_filter_names(self):
        self.assertEqual(self.rows(q="Hara")[0]["post_count"], 4)
        conn = database.get_db()
        with conn:
            conn.execute("DELETE FROM image_tags WHERE image_id=4 AND tag_id=2")
            database.refresh_tag_counts(conn)
        conn.close()
        invalidate_gallery_queries()
        self.assertEqual(self.rows(q="Hara")[0]["post_count"], 3)
        self.assertEqual(self.ids(include_tags="Hara"), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
