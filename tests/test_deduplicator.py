# -*- coding: utf-8 -*-
"""Tests for exact and visual duplicate detection, variation preservation, tag inheritance, and MCP tool."""
from pathlib import Path
import os
import struct
import zlib
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageCms, ImageDraw

import database
import deduplicator
import gallery_deletion
import tool_registry as registry
import tool_runtime as runtime
from test_reliability import GalleryCase


class DeduplicatorTests(GalleryCase):
    def test_png_metadata_does_not_decode_with_pillow_and_opencv_decodes_once(self):
        path = self.image("single-decode.png")
        import cv2
        from PIL import PngImagePlugin
        with patch.object(PngImagePlugin.PngImageFile, "load", side_effect=AssertionError("Pillow must not decode")), \
                patch.object(cv2, "imdecode", wraps=cv2.imdecode) as decode:
            entry = deduplicator._compute_pixel_fingerprint(path)
        self.assertIsNotNone(entry)
        self.assertEqual(decode.call_count, 1)

    def test_png_exif_after_pixel_data_is_preserved_without_raster_decode(self):
        first = self.image("plain.png")
        second = self.root / "rotated.png"
        exif = Image.Exif()
        exif[274] = 3
        data = exif.tobytes()[6:]
        chunk = b"eXIf" + data
        encoded = first.read_bytes()
        # Insert EXIF immediately before IEND, after IDAT.
        second.write_bytes(encoded[:-12] + struct.pack(">I", len(data)) + chunk
                           + struct.pack(">I", zlib.crc32(chunk) & 0xffffffff) + encoded[-12:])
        with patch.object(Image.Image, "load", side_effect=AssertionError("no raster decode")):
            self.assertNotEqual(deduplicator._image_header(first)[2], deduplicator._image_header(second)[2])
        with patch.object(deduplicator, "_load_full_image") as decode:
            matched, metrics = deduplicator.compare_visual_identity(first, second)
            self.assertFalse(matched)
            self.assertEqual(metrics["reason"], "display_metadata_mismatch")
            decode.assert_not_called()

    def test_unique_dimensions_and_verified_exact_copies_need_no_full_decode(self):
        image = self.image("master.png")
        (self.root / "copy.png").write_bytes(image.read_bytes())
        Image.new("RGB", (23, 29), "blue").save(self.root / "unique.png")
        database.scan_library()
        events = []
        with patch.object(deduplicator, "_load_full_image", side_effect=AssertionError("unnecessary decode")):
            preview = deduplicator.scan_duplicates(progress=lambda **value: events.append(value))
        self.assertEqual(preview["exact_duplicate_images"], 1)
        self.assertEqual(preview["visual_duplicate_images"], 0)
        self.assertTrue(any(e.get("skipped_images") == 2 for e in events))
        self.assertEqual(events[-1]["stage"], "比對完成")

    def test_dimensions_are_grouped_within_scope_before_decoding(self):
        for folder, color in (("artist-a", "red"), ("artist-b", "blue")):
            directory = self.root / folder
            directory.mkdir()
            Image.new("RGB", (32, 32), color).save(directory / "image.png")
        database.scan_library()
        with patch.object(deduplicator, "_load_full_image", side_effect=AssertionError("different folders")):
            result = deduplicator.scan_duplicates(scope="same_folder")
        self.assertEqual(result["total_duplicate_images"], 0)

    def test_new_peer_activates_a_previously_skipped_singleton(self):
        first = self.image("first.png")
        database.scan_library()
        with patch.object(deduplicator, "_compute_pixel_fingerprint") as compute:
            self.assertEqual(deduplicator.scan_duplicates()["total_duplicate_images"], 0)
            compute.assert_not_called()
        with Image.open(first) as image:
            image.save(self.root / "peer.webp", lossless=True)
        database.scan_library()
        self.assertEqual(deduplicator.scan_duplicates()["visual_duplicate_images"], 1)

    def test_unchanged_header_cache_skips_image_open_but_invalidates_on_edit(self):
        path = self.image("singleton.png")
        database.scan_library()
        deduplicator.scan_duplicates()
        with patch.object(deduplicator.Image, "open", side_effect=AssertionError("cached header")):
            self.assertEqual(deduplicator.scan_duplicates()["total_duplicate_images"], 0)
        Image.new("RGB", (11, 12), "blue").save(path)
        rows = self.rows("SELECT * FROM images")
        deduplicator._pixel_candidates(rows, "all")
        self.assertEqual((rows[0]["width"], rows[0]["height"]), (11, 12))

    def test_mismatched_dimensions_reject_before_decoding(self):
        first = self.image("first.png")
        second = self.root / "second.png"
        Image.new("RGB", (11, 10), "red").save(second)
        with patch.object(deduplicator, "_load_full_image") as decode:
            matched, metrics = deduplicator.compare_visual_identity(first, second)
        self.assertFalse(matched)
        self.assertEqual(metrics["reason"], "dimensions_mismatch")
        decode.assert_not_called()

    def test_one_low_contrast_pixel_is_preserved_at_original_resolution(self):
        original = self.root / "原圖.png"
        changed = self.root / "表情差分.png"
        image = Image.new("RGB", (1536, 1024), (120, 130, 140))
        image.save(original)
        image.putpixel((1535, 1023), (121, 130, 140))
        image.save(changed)
        # Even the loosest legacy settings cannot override exact equality.
        matched, metrics = deduplicator.compare_visual_identity(
            original, changed, max_mae=20.0, min_ssim=0.8, max_diff_frac=0.05)
        self.assertFalse(matched)
        self.assertEqual(metrics["changed_pixels"], 1)
        self.assertEqual(metrics["max_channel_diff"], 1)
        database.scan_library()
        self.assertEqual(deduplicator.scan_duplicates()["total_duplicate_images"], 0)

    def test_alpha_channel_differences_are_preserved(self):
        first, second = self.root / "first.png", self.root / "alpha.png"
        image = Image.new("RGBA", (64, 64), (120, 130, 140, 255))
        image.save(first)
        image.putpixel((30, 31), (120, 130, 140, 254))
        image.save(second)
        matched, metrics = deduplicator.compare_visual_identity(first, second)
        self.assertFalse(matched)
        self.assertEqual(metrics["changed_pixels"], 1)
        database.scan_library()
        self.assertEqual(deduplicator.scan_duplicates()["total_duplicate_images"], 0)

    def test_16bit_low_bit_differences_are_not_discarded(self):
        first, second = self.root / "16bit.png", self.root / "16bit-diff.png"
        pixels = np.full((64, 64), 32000, dtype=np.uint16)
        Image.fromarray(pixels).save(first)
        pixels[31, 30] += 1
        Image.fromarray(pixels).save(second)
        matched, metrics = deduplicator.compare_visual_identity(first, second)
        self.assertFalse(matched)
        self.assertEqual(metrics["max_channel_diff"], 1)

    def test_equivalent_opaque_rgb_rgba_and_grayscale_pixels_match(self):
        paths = [self.root / (mode + ".png") for mode in ("L", "RGB", "RGBA")]
        image = Image.new("L", (40, 60), 123)
        for path, mode in zip(paths, ("L", "RGB", "RGBA")):
            image.convert(mode).save(path)
        for path in paths[1:]:
            matched, metrics = deduplicator.compare_visual_identity(paths[0], path)
            self.assertTrue(matched)
            self.assertEqual(metrics["changed_pixels"], 0)
        database.scan_library()
        self.assertEqual(deduplicator.scan_duplicates(mode="visual")["total_duplicate_images"], 2)

    def test_resize_recompression_and_display_metadata_changes_are_preserved(self):
        original = self.root / "master.png"
        self._create_gradient_image(original)
        variants = [self.root / name for name in ("small.png", "compressed.jpg", "oriented.png", "profile.png")]
        with Image.open(original) as image:
            image.resize((200, 200)).save(variants[0])
            image.save(variants[1], quality=90)
            exif = image.getexif()
            exif[274] = 3
            image.save(variants[2], exif=exif)
            image.save(variants[3], icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
        for path in variants:
            with self.subTest(path=path.name):
                matched, _ = deduplicator.compare_visual_identity(original, path)
                self.assertFalse(matched)
        database.scan_library()
        self.assertEqual(deduplicator.scan_duplicates()["total_duplicate_images"], 0)

    def test_pixel_cache_invalidates_same_size_edit_and_decoder_version(self):
        first, second = self.root / "a.png", self.root / "b.png"
        image = Image.new("RGB", (64, 64), (120, 130, 140))
        image.save(first, compress_level=0)
        image.save(second, compress_level=9)
        database.scan_library()
        self.assertEqual(deduplicator.scan_duplicates(mode="visual")["total_duplicate_images"], 1)
        rows = self.rows("SELECT * FROM images")
        with patch.object(deduplicator, "_compute_pixel_fingerprint") as compute:
            deduplicator.ensure_pixel_hashes(rows)
            compute.assert_not_called()
        before = first.stat()
        image.putpixel((20, 30), (121, 130, 140))
        image.save(first, compress_level=0)
        self.assertEqual(first.stat().st_size, before.st_size)
        os.utime(first, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))
        self.assertEqual(deduplicator.scan_duplicates(mode="visual")["total_duplicate_images"], 0)
        conn = deduplicator._open_visual_hash_db()
        with conn:
            conn.execute("UPDATE pixel_hashes SET version='outdated'")
        conn.close()
        with patch.object(deduplicator, "_compute_pixel_fingerprint", wraps=deduplicator._compute_pixel_fingerprint) as compute:
            deduplicator.ensure_pixel_hashes(rows)
            self.assertEqual(compute.call_count, 2)

    def test_cached_fingerprint_collision_still_requires_direct_comparison(self):
        first, second = self.root / "a.png", self.root / "b.png"
        image = Image.new("RGB", (64, 64), "white")
        image.save(first)
        image.putpixel((10, 10), (254, 255, 255))
        image.save(second)
        database.scan_library()
        deduplicator.scan_duplicates(mode="visual")
        conn = deduplicator._open_visual_hash_db()
        with conn:
            conn.execute("UPDATE pixel_hashes SET pixel_hash='forced-collision'")
            ids = [row["id"] for row in self.rows("SELECT id FROM images ORDER BY id")]
            conn.execute("INSERT INTO visual_pair_cache VALUES(?,?,?,?,0,1,0,0)",
                         (*ids, first.stat().st_size, second.stat().st_size))
        conn.close()
        self.assertEqual(deduplicator.scan_duplicates()["total_duplicate_images"], 0)

    def test_cleanup_rejects_stale_preview_before_backup_or_tag_merge(self):
        first, second = self.root / "a.png", self.root / "b.png"
        image = Image.new("RGB", (64, 64), "white")
        image.save(first, compress_level=0)
        image.save(second, compress_level=9)
        database.scan_library()
        preview = deduplicator.scan_duplicates()
        self.assertEqual(preview["total_duplicate_images"], 1)
        image.putpixel((10, 10), (254, 255, 255))
        image.save(second)
        with patch("backup_service.backup") as backup, patch.object(gallery_deletion, "delete_images") as delete:
            with self.assertRaisesRegex(ValueError, "重新掃描"):
                deduplicator.execute_dedup_groups(preview["groups"])
            backup.assert_not_called()
            delete.assert_not_called()
            from fastapi import HTTPException
            from management_api import DedupExecuteRequest, dedup_execute
            with self.assertRaises(HTTPException) as response:
                dedup_execute(DedupExecuteRequest(groups=preview["groups"], selected_delete_ids=preview["all_delete_ids"]))
            self.assertEqual(response.exception.status_code, 400)
            backup.assert_not_called()
            delete.assert_not_called()
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())

    def test_exact_and_pixel_subgroups_share_one_kept_image(self):
        image = Image.new("RGB", (64, 64), "white")
        for name, level in (("a.png", 0), ("b.png", 9)):
            path = self.root / name
            image.save(path, compress_level=level)
            (self.root / ("copy-" + name)).write_bytes(path.read_bytes())
        database.scan_library()
        preview = deduplicator.scan_duplicates()
        self.assertEqual(preview["total_groups"], 1)
        self.assertEqual(preview["total_duplicate_images"], 3)
        self.assertEqual(preview["exact_duplicate_images"], 1)
        self.assertEqual(preview["visual_duplicate_images"], 2)
        self.assertNotIn(preview["groups"][0]["keep"]["id"], preview["all_delete_ids"])
        with patch.object(gallery_deletion, "recycle_files", side_effect=lambda paths: [Path(p).unlink() for p in paths]):
            result = deduplicator.execute_dedup_groups(preview["groups"])
        self.assertEqual(result["deleted_count"], 3)
        self.assertEqual(len(list(self.root.glob("*.png"))), 1)

    def test_scope_limits_pixel_equality_to_selected_folders(self):
        image = Image.new("RGB", (64, 64), "white")
        for folder, level in (("artist-a", 0), ("artist-b", 9)):
            directory = self.root / folder
            directory.mkdir()
            image.save(directory / "image.png", compress_level=level)
        database.scan_library()
        self.assertEqual(deduplicator.scan_duplicates(scope="same_folder")["total_duplicate_images"], 0)
        self.assertEqual(deduplicator.scan_duplicates(scope="all")["total_duplicate_images"], 1)
        self.assertEqual(deduplicator.scan_duplicates(scope="all", folder="artist-a")["total_duplicate_images"], 0)

    def test_exact_comparison_checks_middle_of_large_files(self):
        first, second = self.root / "large-a.bin", self.root / "large-b.bin"
        data = bytearray(9 * 1024 * 1024)
        first.write_bytes(data)
        second.write_bytes(data)
        self.assertTrue(deduplicator._files_identical(first, second))
        data[len(data) // 2] = 1
        second.write_bytes(data)
        self.assertFalse(deduplicator._files_identical(first, second))

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

        # 3. Lossless format conversion with all original pixels preserved.
        visual_dup_path = folder_dir / "20260101_0002_lossless.webp"
        with Image.open(master_path) as m_img:
            m_img.save(visual_dup_path, format="WEBP", lossless=True)

        # 4. Artist variation / differential image (same 600x600 base, but has a local change)
        variation_path = folder_dir / "20260101_0003_diff.png"
        self._create_gradient_image(variation_path, size=(600, 600), fmt="PNG", add_variation=True)

        database.scan_library()

        conn = database.get_db()
        try:
            rows = {r["filename"]: r["id"] for r in conn.execute("SELECT id, filename FROM images").fetchall()}
            master_id = rows["20260101_0001.png"]
            exact_id = rows["20260101_0001 (1).png"]
            visual_id = rows["20260101_0002_lossless.webp"]
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
                    self.assertIsNone(deduplicator._compute_pixel_fingerprint(path))
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
