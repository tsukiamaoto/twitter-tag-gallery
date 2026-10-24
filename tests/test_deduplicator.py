# -*- coding: utf-8 -*-
"""Tests for exact and visual duplicate detection, variation preservation, tag inheritance, and MCP tool."""
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

import database
import deduplicator
import gallery_deletion
import tool_registry as registry
import tool_runtime as runtime
from test_reliability import GalleryCase


class DeduplicatorTests(GalleryCase):
    def _create_gradient_image(self, path: Path, size=(400, 400), fmt="PNG", quality=95, add_variation=False):
        path.parent.mkdir(parents=True, exist_ok=True)
        w, h = size
        x = np.linspace(20, 230, w, dtype=np.float32)
        y = np.linspace(30, 220, h, dtype=np.float32)
        xx, yy = np.meshgrid(x, y)
        r = np.clip(xx, 0, 255).astype(np.uint8)
        g = np.clip(yy, 0, 255).astype(np.uint8)
        b = np.clip((xx + yy) * 0.5, 0, 255).astype(np.uint8)
        arr = np.stack([r, g, b], axis=2)
        img = Image.fromarray(arr, mode="RGB")
        draw = ImageDraw.Draw(img)
        draw.rectangle([w // 4, h // 4, w * 3 // 4, h * 3 // 4], outline=(255, 240, 180), width=6)
        if add_variation:
            # Simulate an artist differential (e.g. expression/dialogue/clothing patch > 0.5% area)
            draw.rectangle([w // 3, h // 3, w // 3 + w // 5, h // 3 + h // 5], fill=(20, 20, 240))
        if fmt.upper() == "JPEG":
            img.save(path, format="JPEG", quality=quality)
        else:
            img.save(path, format="PNG")

    def test_exact_and_visual_dedup_preserves_variations_and_inherits_tags(self):
        folder_dir = self.root / "sincos"
        # 1. Master high-res PNG (600x600)
        master_path = folder_dir / "20260101_0001.png"
        self._create_gradient_image(master_path, size=(600, 600), fmt="PNG")

        # 2. Exact byte-for-byte copy of master
        exact_dup_path = folder_dir / "20260101_0001 (1).png"
        exact_dup_path.write_bytes(master_path.read_bytes())

        # 3. Downscaled & JPEG-recompressed visual duplicate (300x300 JPG)
        visual_dup_path = folder_dir / "20260101_0002_small.jpg"
        with Image.open(master_path) as m_img:
            small = m_img.resize((300, 300), Image.Resampling.BICUBIC)
            small.save(visual_dup_path, format="JPEG", quality=92)

        # 4. Artist variation / differential image (same 600x600 base, but has a local change)
        variation_path = folder_dir / "20260101_0003_diff.png"
        self._create_gradient_image(variation_path, size=(600, 600), fmt="PNG", add_variation=True)

        database.scan_library()

        conn = database.get_db()
        try:
            rows = {r["filename"]: r["id"] for r in conn.execute("SELECT id, filename FROM images").fetchall()}
            master_id = rows["20260101_0001.png"]
            exact_id = rows["20260101_0001 (1).png"]
            visual_id = rows["20260101_0002_small.jpg"]
            variation_id = rows["20260101_0003_diff.png"]

            # Attach a character tag to exact_id and a manual tag to visual_id to verify tag inheritance
            cache = {}
            char_tag_id = database.ensure_tag(conn, cache, "hatsune_miku", "character")
            manual_tag_id = database.ensure_tag(conn, cache, "收藏精選", "general")
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, 1.0, 'danbooru')",
                    (exact_id, char_tag_id),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO image_tags (image_id, tag_id, confidence, source) VALUES (?, ?, 1.0, 'manual')",
                    (visual_id, manual_tag_id),
                )
        finally:
            conn.close()

        # Preview scan (execute_delete=False)
        preview = deduplicator.clean_duplicates(mode="both", scope="same_folder", folder="sincos", execute_delete=False)
        self.assertTrue(preview["preview"])
        self.assertEqual(preview["total_scanned"], 4)
        self.assertEqual(preview["exact_duplicate_images"], 1)
        self.assertEqual(preview["visual_duplicate_images"], 1)
        self.assertEqual(set(preview["all_delete_ids"]), {exact_id, visual_id})
        self.assertNotIn(master_id, preview["all_delete_ids"])
        self.assertNotIn(variation_id, preview["all_delete_ids"], "Artist variation must strictly be preserved")

        # Execute cleanup (stub recycle_files so test temp files are unlinked instead of sent to OS Recycle Bin)
        with patch.object(gallery_deletion, "recycle_files", side_effect=lambda paths: [Path(p).unlink() for p in paths]):
            result = deduplicator.clean_duplicates(mode="both", scope="same_folder", folder="sincos", execute_delete=True)

        self.assertFalse(result["preview"])
        exec_info = result["execution"]
        self.assertEqual(exec_info["deleted_count"], 2)
        self.assertEqual(set(exec_info["deleted_ids"]), {exact_id, visual_id})
        self.assertTrue(master_path.exists())
        self.assertTrue(variation_path.exists())
        self.assertFalse(exact_dup_path.exists())
        self.assertFalse(visual_dup_path.exists())

        # Verify inherited tags on master_id
        conn = database.get_db()
        try:
            kept_tags = {
                r["name"]: r["source"]
                for r in conn.execute(
                    "SELECT t.name, it.source FROM image_tags it JOIN tags t ON t.id = it.tag_id WHERE it.image_id = ?",
                    (master_id,),
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertIn("hatsune_miku", kept_tags)
        self.assertIn("收藏精選", kept_tags)

    def test_mcp_tool_gallery_clean_duplicates_registered_and_validates(self):
        self.assertIn("gallery_clean_duplicates", registry.CATALOG)
        tool = registry.CATALOG["gallery_clean_duplicates"]
        validated = tool.validate(tool.example)
        self.assertEqual(validated["mode"], "both")
        self.assertEqual(validated["execute_delete"], False)
        run_info = runtime.invoke("gallery_clean_duplicates", {"mode": "exact", "execute_delete": False})
        self.assertTrue(run_info["preview"])
        self.assertEqual(run_info["total_duplicate_images"], 0)

    def test_animated_formats_are_excluded_from_visual_comparison(self):
        for ext in ("gif", "webp", "png"):
            with self.subTest(ext=ext):
                first = Image.new("RGB", (48, 48), "white")
                paths = [self.root / f"animation-{color}.{ext}" for color in ("red", "blue")]
                for path, color in zip(paths, ("red", "blue")):
                    first.save(path, save_all=True, append_images=[Image.new("RGB", first.size, color)], duration=100, loop=0)
                    with Image.open(path) as image:
                        self.assertEqual(image.n_frames, 2)
                    self.assertIsNone(deduplicator._compute_single_visual_hash(path))
                matched, metrics = deduplicator.compare_visual_identity(*paths)
                self.assertFalse(matched)
                self.assertEqual(metrics["reason"], "animated_media")
                matched, _ = deduplicator.compare_visual_identity(paths[0], paths[0])
                self.assertFalse(matched, "Even identical animations must use exact mode, not first-frame similarity")

    def test_legacy_cached_animation_hashes_cannot_delete_different_animations(self):
        first = Image.new("RGB", (48, 48), "white")
        for name, color in (("a.gif", "red"), ("b.gif", "blue")):
            first.save(self.root / name, save_all=True, append_images=[Image.new("RGB", first.size, color)], duration=100, loop=0)
        (self.root / "a-copy.gif").write_bytes((self.root / "a.gif").read_bytes())
        database.scan_library()
        rows = self.rows("SELECT * FROM images")
        vconn = deduplicator._open_visual_hash_db()
        try:
            with vconn:
                # Reproduce cache entries generated by the old first-frame implementation.
                for row in rows:
                    vconn.execute("INSERT INTO visual_hashes(id,rel_path,size_bytes,width,height,dhash,phash) VALUES(?,?,?,48,48,?,?)",
                                  (row["id"], row["rel_path"], row["size_bytes"], "0000000000000000", "8000000000000000"))
        finally:
            vconn.close()
        preview = deduplicator.clean_duplicates(mode="both")
        self.assertEqual(preview["exact_duplicate_images"], 1)
        self.assertEqual(preview["visual_duplicate_images"], 0)
        blue_id = next(row["id"] for row in rows if row["filename"] == "b.gif")
        self.assertNotIn(blue_id, preview["all_delete_ids"])
        with patch.object(gallery_deletion, "recycle_files", side_effect=lambda paths: [Path(p).unlink() for p in paths]):
            result = deduplicator.clean_duplicates(mode="both", execute_delete=True)
        self.assertEqual(result["execution"]["deleted_count"], 1)
        self.assertTrue((self.root / "a.gif").exists())
        self.assertTrue((self.root / "b.gif").exists())

    def test_response_group_limit_does_not_cap_execution_scope(self):
        groups = [{"group_id": index} for index in range(10001)]
        def scan(*, max_groups, **options):
            return {"total_groups": len(groups), "groups": groups if max_groups is None else groups[:max_groups]}
        with patch.object(deduplicator, "scan_duplicates", side_effect=scan), patch.object(
                deduplicator, "execute_dedup_groups", side_effect=lambda selected: {"deleted_count": len(selected)}):
            result = deduplicator.clean_duplicates(execute_delete=True, max_groups=1)
        self.assertEqual(result["execution"]["deleted_count"], 10001)
        self.assertEqual(len(result["groups"]), 1)
        self.assertTrue(result["groups_truncated"])

    def test_tag_merge_preserves_and_inherits_manual_provenance(self):
        keep_path = self.image("keep.png")
        (self.root / "copy.png").write_bytes(keep_path.read_bytes())
        database.scan_library()
        ids = {row["filename"]: row["id"] for row in self.rows("SELECT id,filename FROM images")}
        conn = database.get_db()
        try:
            with conn:
                keep_manual = database.ensure_tag(conn, {}, "keep_manual_artist", "artist")
                inherit_manual = database.ensure_tag(conn, {}, "inherit_manual_character", "character")
                conn.executemany("INSERT INTO image_tags(image_id,tag_id,confidence,source) VALUES(?,?,?,?)", [
                    (ids["keep.png"], keep_manual, .2, "manual"),
                    (ids["copy.png"], keep_manual, 1.0, "danbooru"),
                    (ids["keep.png"], inherit_manual, 1.0, "ai"),
                    (ids["copy.png"], inherit_manual, .2, "manual"),
                ])
        finally:
            conn.close()
        with patch.object(gallery_deletion, "recycle_files", side_effect=lambda paths: [Path(p).unlink() for p in paths]):
            result = deduplicator.clean_duplicates(mode="exact", execute_delete=True)
        self.assertEqual(result["execution"]["deleted_count"], 1)
        merged = {row["tag_id"]: row for row in self.rows("SELECT tag_id,source,confidence FROM image_tags WHERE image_id=?", ids["keep.png"])}
        self.assertEqual(merged[keep_manual]["source"], "manual")
        self.assertAlmostEqual(merged[keep_manual]["confidence"], .2)
        self.assertEqual(merged[inherit_manual]["source"], "manual")
