---
name: twitter-tag-correlation
description: >-
  透過 twitter-tag-gallery MCP 分析本機圖庫的標籤相關性、語意搜尋、翻譯既有標籤，
  或由模型校正畫師、角色、作品及來源並協助畫師歸檔。適用於 Antigravity
  等模型操作圖庫的需求；去重請使用 gallery-deduplication，一般程式碼維護不需要執行此流程。
---

# 本機圖庫標籤搜尋與模型校正

本專案 MCP 位於 `H:\code\twitter-tag-gallery\mcp_server.py`，預設圖庫為
`D:\圖檔\twitter`，Web UI 為 `http://127.0.0.1:8765`。以工具回傳的圖片 ID、
標籤與目前狀態為準，不假設圖庫數量或模型版本。

## 模型標籤範圍

使用者選擇只保留畫師 (`artist`)、角色 (`character`)、作品 (`copyright`)、來源 (`source`)
及既有人工標籤。模型重新產生、校正與批次處理標籤時，僅提交前述四個分類。

- 不產生或寫入外觀、服裝、姿勢、場景等一般描述，也不建立分級、年份或語意主題標籤。
- 無法確認畫師、角色或作品時略過該項，不以猜測湊足標籤，不將一般描述改列為核心分類。
- 網頁人工編輯可以使用其他分類；模型的 MCP 編輯一律記為模型來源，不得偽裝人工來源，
  也不得覆蓋或移除人工標籤。
- 使用 MCP 完成寫入；不可透過直接執行 SQL 或改寫資料庫繞過分類限制。工具拒絕某項標籤
  時，修正提交內容，不改用其他寫入入口繞過限制。

## 選擇工具

- `gallery_get_overview` 可取得圖庫概況與目前的 `tag_policy`。
- `gallery_search_images`、`gallery_get_image_details` 取得圖片 ID、標籤及處理狀態。
- `gallery_find_tag_correlations`、`gallery_semantic_search` 可讀取既有標籤進行分析與搜尋；
  讀取歷史一般標籤不代表應重新產生這類標籤。
- `gallery_list_untranslated_tags` 列出待翻譯標籤；`gallery_apply_gemini_enrichments`
  僅更新既有翻譯。省略 `semantic_theme_groups` 及 `auto_cluster_builtin_themes`，不要求主題聚合。
- `gallery_calibrate_image_artist_and_tags` 提交有依據的畫師、角色、作品及繁中名稱。
  `add_general_tags` 已停用，請省略。預設不搬移、不清除 AI 畫師標籤；只有使用者授權對應操作時，
  才傳 `move_to_artist_folder=true` 或 `purge_ai_artist_tags=true`。
  提交角色或作品會清除對應分類信心度低於 0.70 的 AI 標籤，人工標籤保留。
- `gallery_update_image_tags` 只接受上述四種核心分類，必須明確指定 `category`。
- `gallery_batch_tag_or_move` 依至少一個既有標籤篩選並新增畫師；禁止空的 `include_tags`。
  指定 `move_to_folder` 即實際搬移並自動改名，只在使用者要求歸檔時使用；需要預覽則用 `gallery_move_images`。
- `gallery_deep_reverse_search_batch` 預設只反查及校正標籤；明確傳 `auto_move_verified=true` 才搬移。
- 去重工作讀取 [gallery-deduplication](../gallery-deduplication/SKILL.md)，由該技能定義比對限制與刪除流程。

## 工作方式

先讀取相關圖片與標籤，依使用者指定的範圍分析或校正。使用當前模型即可；只有使用者
指定模型或委派時才採用相應方式。對待校正圖片取得可信依據後，再提交核心標籤。
需要更新翻譯時，只翻譯目前保留的核心標籤或使用者指定的人工標籤。

長任務的啟動回應不是完成結果。以 `gallery_get_tool_run(run_id)` 查詢，直到 `run.status`
不再是 `running`，讀取 `run.result` 或 `run.error`。回報實際更新與略過項目，不把遭拒或推測的標籤當成已寫入。

識別碼與停止方式：

- `run_id` 是工具執行紀錄，只用來查詢結果。
- 啟動回應的 `job_id` 是共用圖庫作業 ID，可能為 `null`；僅非空值可傳 `gallery_cancel_job`。
- 桌面橋接器提供的 `job_id` 是桌面查證工作 ID，在啟動回應另列為 `agent_job_id`；
  用 `gallery_cancel_agent_job(agent_job_id)` 停止，不能傳給 `gallery_cancel_job`。
- 優先照啟動回應的 `cancel_tool` 與 `cancel_arguments` 送出停止要求；兩者為 `null` 時沒有停止入口。
  停止是合作式處理，已開始的網路請求或安全檔案步驟可能仍須完成。保持 MCP 連線至工具結束。

既有標籤的大量清理、原始圖片刪除及搬移是各自獨立的工作，不能由「重新產生標籤」
推定使用者已授權。清理既有資料前先提供範圍與數量，經使用者同意後建立可還原備份。
