---
name: twitter-tag-correlation
description: >-
  透過 twitter-tag-gallery MCP Server 與 Gemini Flash 模型，分析 D:\圖檔\twitter
  圖庫中的標籤相關性 (Tag Correlation)、語意展開搜尋、自動補齊繁體中文標籤翻譯、
  建立高階主題叢集，以及批次將圖片歸檔至繪師資料夾。當使用者要求分析圖片標籤相關性、
  用 Gemini Flash 處理圖庫標籤或搜尋特定主題圖片時啟用此技能。
---

# Twitter / Pixiv 圖庫標籤相關性與 Gemini Flash 處理技能

本技能結合 `twitter-tag-gallery` MCP Server（位於 `H:\code\twitter-tag-gallery\mcp_server.py`）與 Antigravity 的 **Gemini Flash (`Model="flash"`)** 子代理，對 `D:\圖檔\twitter` 的 31,031 張圖片進行高速標籤相關性分析與語意歸納。

## 1. 可用的 MCP 工具 (`twitter-tag-gallery`)

1. `gallery_get_overview()`：取得圖庫統計、前 30 大畫師與前 40 大高頻標籤。
2. `gallery_find_tag_correlations(target_tag, min_cooccurrence=2, top_k=40)`：計算指定標籤或畫師與其他所有標籤的共現次數、條件機率 $P(B|A)$ 與 Jaccard 相似度。
3. `gallery_semantic_search(query)`：輸入自然語言或關鍵字，展開相關標籤並依符合標籤數排序回傳圖片。
4. `gallery_list_untranslated_tags(category='all', limit=100)`：列出尚未有繁體中文翻譯的高頻英文 Danbooru 標籤。
5. `gallery_apply_gemini_enrichments(translations, semantic_theme_groups, auto_cluster_builtin_themes)`：將 Gemini Flash 推理出的繁體中文翻譯與高階主題相關性群組（如 `主題:黑絲與美腿`、`主題:泳裝與夏日海灘`）批次寫入 SQLite。
6. `gallery_batch_tag_or_move(include_tags, add_artist_tag, add_general_tags, move_to_folder, only_root_files)`：依標籤交集批次新增標籤或自動編號搬移至繪師資料夾。

## 2. 標準執行流程 (使用 Gemini Flash 進行相關性處理)

當使用者要求「用 Gemini Flash 搜尋標籤相關性並處理」時：
1. 若主代理非 Flash 模型，可透過 `invoke_subagent` 啟動 `Model="flash"` 的子代理執行，或直接由當前代理呼叫 MCP 工具（或執行 `H:\code\twitter-tag-gallery\mcp_server.py` 內的函式）。
2. 先呼叫 `gallery_list_untranslated_tags(limit=150)` 與 `gallery_get_overview()` 取得目前圖庫中的標籤分佈。
3. 由 Gemini Flash 分析這些標籤之間的語意相關性：
   - 產生精準的繁體中文翻譯字典 (`translations`)
   - 歸納出高關聯性的主題標籤群組 (`semantic_theme_groups`)
4. 呼叫 `gallery_apply_gemini_enrichments(...)` 將結果寫回 `D:\圖檔\twitter\.tag_gallery\gallery.db`，讓 Web UI (`http://127.0.0.1:8765`) 立即生效！
