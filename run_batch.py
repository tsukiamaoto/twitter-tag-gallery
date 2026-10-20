import sys
import json
sys.path.insert(0, r"H:\code\twitter-tag-gallery")
import mcp_server

print("Running deep reverse search batch...")
res = mcp_server.tool_gallery_deep_reverse_search_batch(
    scope="recent_root",
    limit=15,
    min_similarity=70.0,
    include_google_lens_url=True,
    auto_move_verified=True,
)

with open(r"H:\code\twitter-tag-gallery\deep_search_results.json", "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=2)

print(f"Done! Processed: {res['total_processed']}, Verified: {res['verified_artists_count']}, Moved: {res['moved_to_correct_folder_count']}, Purged tags: {res['purged_wrong_ai_tags_total']}")
for i, item in enumerate(res["items"]):
    print(f"[{i+1}] ID:{item['image_id']} {item['filename']} Verified:{item['verified']} Method:{item['method']} Sim:{item.get('similarity', 0)} Artists:{item['artists']} Folder:{item['moved_to_folder'] or item['old_folder']}")
