# 012 資料一致性、效能與 MCP 管理

狀態：完成，已於 2026-10-21 取得使用者明確授權，完成正式資料庫備份、服務重啟與資料庫遷移；Antigravity 真實客戶端回報 READY 並載入 24 個工具。

使用者核准範圍：掃描防誤刪、可復原檔案操作、路徑防護、交易快取回滾、分開 AI 與來源查驗狀態、模型生命週期、查詢快取、跨程序任務、檔案變更／去重／快取清理、前端模組化，以及設定中的圖庫 MCP 管理。使用者於 2026-10-21 取消 Gemini API 直連，改由 Antigravity 作為 MCP 客戶端。

效能原則：改名使用一次計畫快照與追加式日誌；重試只撤銷本次快取變動；雜湊以新檔與變動檔為主；查詢快取有容量上限及跨程序失效；進度寫入節流。

## 驗收

- [x] 不完整掃描保留圖片索引與人工標籤；完整掃描只標記遺失。
- [x] 改名與搬移中斷可復原，禁止圖庫外路徑。
- [x] 重試不保留回滾交易的標籤 ID。
- [x] 圖片解碼及暫時性網路失敗可重試。
- [x] GPU 推論、載入與卸載共用生命週期鎖。
- [x] 搜尋與快取保持結果一致並量測效能。
- [x] UI 顯示跨程序任務進度與失敗，閒置輪詢降頻。
- [x] MCP 設定、權限、診斷與 Antigravity 匯出可用；模型登入由客戶端管理。
- [x] Settings 工具參數、執行、停止及結果紀錄與 MCP 共用驗證及作業鎖。
- [x] 隔離故障測試與既有測試通過；正式圖片不作測試素材。

## 實作與驗證

- 掃描採 `os.walk(onerror=...)`，列舉／stat／hash 失敗都保留未出現的索引。完整掃描只設 `missing_since`，不刪除圖片或人工標籤。重現圖片失聯、重新出現、原地修改及外部改名；另驗證硬連結在暫存失敗時不會被誤刪。
- 共用檔案操作服務先持久化計畫，追加事件並維持 pending 指標；SQLite 提交記錄可辨識「檔案完成、最後日誌尚未寫入」的中斷。失敗／取消會回復，開機會檢查未完成計畫。跨磁碟匯入驗證 SHA-256，提交前保留來源。
- 路徑檢查涵蓋來源與圖庫互相包含、系統資料夾、上層路徑、符號連結及 Windows 保留名稱。
- SQLite 重試只撤銷本次新增的 tag cache 項目。dirty-tag triggers 只重算受影響標籤，不逐次重算全庫。
- AI 與 Pixiv 查驗分開記錄狀態、錯誤、重試時間及版本。解碼失敗不完成；Pixiv 逾時／403 不寫已查驗標記；404／410 定期允許重查。
- 模型載入、推論、卸載由共用 gate 管理；DirectML 關閉 memory pattern 並使用 sequential execution。測試驗證推論與卸載不重疊，已用目前安裝的 ORT 確認設定可用，未載入真實 GPU 模型作壓測。
- 查詢採標籤 ID 集合、最近圖片優先查詢（完整處理時間相同的 ties，必要時回退聚合）、有容量上限的單次載入快取；DB/WAL 改變會跨程序失效。
- 任務存於獨立 jobs.sqlite3，不與圖庫的大量寫入競爭；進度每秒節流，Web／Windows 排程／MCP 共用。支援合作式停止及依原範圍重試未完成項目。
- 檔案指紋於新檔／變動檔建立，舊圖庫在閒置時分批補齊。重複匯入保留來源、不重複搬入已知指紋；不會自動刪除既有重複原圖。舊縮圖保留近期版本，只清理過期快取；語意規則重新套用會移除失效衍生標籤並記錄規則版本。
- MCP 相容 SDK 1.x FastMCP 與 2.x MCPServer；權限每次呼叫檢查，設定生效不依賴客戶端重新啟動。工具清單仍建議由客戶端重新整理。支援本機握手與 tools/list，完整提供 24 個工具，唯讀模式只回傳 10 個唯讀工具。
- 工具定義與 Pydantic 參數格式集中於 `tool_registry.py`；業務實作移至 `gallery_tools.py`、`gallery_workflow_tools.py`，協定留在 `mcp_server.py`。修正備援模式漏列單張反查與畫師歸檔兩個工具，各入口使用同一份 schema。
- 設定 → MCP 管理 →「工具操作與執行紀錄」提供選擇工具、說明與影響範圍、JSON schema、參數範例、執行、結果、失敗及紀錄查詢。寫入工具按鈕明確標示修改圖庫；合作式停止使用既有任務取消入口，不會強制終止檔案操作。
- 設定頁工具在背景執行；每個程序最多兩個唯讀工具，寫入工具共用全域作業鎖，停止要求不重取執行中的寫入鎖。`tool_runs.sqlite3` 獨立儲存最近 200 次紀錄、每次最多 128 KiB 結果，不儲存輸入參數；程序中止標成 interrupted，不自動重跑寫入。只有可見的工具操作區在執行中每秒讀取結果，關閉或隱藏會停止請求。
- 前端移除全域函式替換，改用明確註冊的擴充點；主要 JS 與 CSS 自 HTML 拆出。模型與背景工作取用同一 runtime；忙碌 1 秒、閒置 10 秒、隱藏分頁 30 秒輪詢。

## 測試

- Python：95 項通過（既有 42 項、可靠性 26 項、工具管理 13 項及圖庫流程 14 項）。
- Node：15 項通過。
- Playwright：gallery-shell、smart-tag-ui、integration-settings 三組通過；涵蓋 320–2507 px，瀏覽器錯誤為 0。
- 新設定測試涵蓋唯讀工具、儲存、移除 Gemini API 分頁、診斷成功／失敗、Antigravity 匯出、跨程序工作與失敗顯示。
- 工具管理測試涵蓋 Settings API 的 403／404／409／410／422、schema 與型別驗證、結果與失敗、背景執行、取消、作業鎖、完成時序、唯讀併發上限、紀錄容量及中斷。SDK 1.x、2.x 均實際透過 stdio 握手、tools/list、呼叫臨時圖庫總覽並驗證非法參數的錯誤回報。新增圖庫工具的 12 項管理與 14 項流程測試已驗證兩個 SDK；目前工具管理為 13 項，另包含退役 Gemini API 不連線的測試。瀏覽器亦驗證 JSON 錯誤不送出、停用寫入工具、掃描背景作業／停止要求、結果與失敗顯示及 320 px 操作區。
- 圖庫、GPU、付費生成及正式檔案搬移未作測試素材；資料庫效能使用一致備份。

## 效能

同一批 31,146 張圖片、約 120 萬筆關聯的備份，三次樣本（非 p95）：

| 項目 | 結果 |
| --- | --- |
| 舊關鍵字無命中計數 | 192–236 ms |
| 新關鍵字完整列表 API | 13–21 ms |
| 自然排序完整 API 冷／暖 | 122 ms／5.7–6.4 ms |
| 最近標籤完整 API 冷／暖 | 9.3 ms／1.3–1.4 ms |
| 無 dirty tags 的統計更新 | 0.00–0.03 ms |
| 500 個暫存檔舊兩階段改名 | 0.39 秒 |
| 500 個暫存檔可復原改名 | 1.43 秒 |

改名增加約 1.04 秒／500 檔，約 2 ms／檔；包含完整路徑與原檔驗證、持久計畫、事件、額外 DB 保護步驟。未逐張 fsync 日誌；跨磁碟複製另包含內容驗證成本。腳本：`python -B tests/benchmark_reliability.py --database <gallery.db> --files 500`，正式庫只讀，寫入僅發生於暫存副本。

工具管理另以臨時圖庫與空 handler 各量測 30 次：參數驗證、存取紀錄及結果持久化的額外成本，唯讀中位數 10.40 ms／p95 12.05 ms；含背景作業鎖與任務紀錄的寫入入口中位數 23.51 ms／p95 25.50 ms。數字不含真實查詢、網路、搬移或 GPU 工作。

## 使用與限制

- 更新後重新啟動 Web 後端及舊 MCP 客戶端。初次資料遷移保留 `backups/before_reliability_migration_*.db`。
- 已建立指紋／檔案識別後，才能可靠關聯外部改名；無法憑空追認首次建立指紋前已移走的圖片。失聯索引保留可供後續處理。
- 重試依原範圍重新選取未完成資料，不是恢復 GPU 執行中的記憶體；已提交標籤及檔案操作不依賴程序記憶體。
- 圖庫不直接呼叫 Gemini API，不接收或讀取 Gemini 金鑰。MCP 是本專案 stdio server 的管理，不會改寫使用者其他 MCP 客戶端設定檔。
- 工具操作會真實執行已填參數，不是寫入的模擬模式；需自行核對範例中的圖片 ID 與標籤。整合測試的網路查詢、GPU 及寫入 handler 使用替身，正式圖庫未作工具測試素材。
- 磁碟故障／外部程式修改造成無法安全辨識原檔時，保留計畫並停止新檔案操作，不覆蓋衝突檔。

## 圖庫 MCP 流程工具（2026-10-21）

提供搜尋、打標、歸檔、復原等實際圖庫工具，Settings 會自動列出並可逐一停用。2026-10-24 移除獨立手動掃描工具；圖庫索引仍在初次啟動與排程匯入後自動同步。

| 工具 | 用途 | MCP 回應 |
| --- | --- | --- |
| `gallery_search_images` | 關鍵字／標籤／資料夾／AI 狀態與分頁搜尋 | 圖片清單及 ID |
| `gallery_get_image_details` | 圖片詳細標籤、處理階段、錯誤及版本 | 詳情，標籤最多 500 筆 |
| `gallery_update_image_tags` | 明確指定圖片 ID 批次編輯核心模型標籤，保護人工標籤 | 更新數量 |
| `gallery_analyze_images` | 指定最多 100 張圖片本機 AI 打標 | 背景任務 ID |
| `gallery_move_images` | 指定圖片搬移，預設只預覽 | 背景任務 ID |
| `gallery_recover_file_operations` | 修復中斷的檔案操作，保留衝突日誌 | 背景任務 ID |
| `gallery_list_rename_batches` | 列出可還原的已完成改名批次 | 批次清單 |
| `gallery_undo_rename` | 依 batch_id 還原已完成改名 | 背景任務 ID |
| `gallery_get_jobs` | 跨程序作業、階段、進度與最近結果 | 任務清單，不新增輪詢紀錄 |
| `gallery_get_tool_run` | 依 run_id 讀取結果、錯誤、job_id | 執行詳情，不新增輪詢紀錄 |
| `gallery_cancel_job` | 依 job_id 合作式停止目前作業 | 停止要求已送出 |
| `gallery_cancel_agent_job` | 依 agent_job_id 停止桌面查證工作 | 已停止工作狀態，後續提交拒絕 |

建議順序：

1. `gallery_search_images` 傳 `{"q":"風景","page_size":20}`，或 `{"include_tags":["landscape"],"folder":"__ROOT__"}`。`include_tags` 使用原始標籤名稱；中文關鍵字使用 `q`。
2. `gallery_update_image_tags` 傳 `{"image_ids":[實際圖片ID],"add_tags":[{"name":"artist_name","category":"artist"}]}`；或使用 `gallery_analyze_images` 讓本機 AI 分析。模型目前只可新增畫師、角色、作品、來源，不能覆蓋或移除人工標籤，詳見 [018 分類限制](018-core-generated-tags.md)。相同模型與檔案版本已完成時預設略過；`skip_completed=false` 可重新分析。背景工具回傳 `run_id` 後，使用 `gallery_get_tool_run` 每 1–2 秒查詢，直到 `run.status` 不再是 `running`。
3. `gallery_move_images` 先傳 `{"image_ids":[實際圖片ID],"target_folder":"畫師名稱","execute_move":false}`；查看結果內 `entries`，確認後以相同條件傳 `execute_move=true`。執行會重新建立計畫並再次檢查檔案衝突。
4. 需要停止時，使用啟動回應的 `cancel_tool` 與 `cancel_arguments`。共用圖庫作業的非空 `job_id`（亦即 `gallery_get_jobs` 的 `active.id`）傳給 `gallery_cancel_job`；桌面查證工作的 `agent_job_id` 傳給 `gallery_cancel_agent_job`。兩者不可混用，`run_id` 僅用於讀取工具結果。`job_id=null` 的背景工具可能屬於桌面查證工作，不能因此推定沒有停止入口。

操作預設值與限制：`gallery_batch_tag_or_move.include_tags` 至少一個標籤，指定資料夾即實際搬移，需要預覽改用 `gallery_move_images`。批次反查的 `auto_move_verified`、畫師校正的 `move_to_artist_folder` 與 `purge_ai_artist_tags`、清理畫師的 `revert_unverified_ai_folders` 均預設 `false`。畫師查證證據使用具名的 `source_url`、`linked_url`；`confirmed=true` 必須附非空標準名稱、至少兩個平台帳號與證據。背景工具說明會附上共用執行契約，即使使用者自訂用途說明，仍會保留輪詢與停止方式。

長任務在啟動它的 MCP 程序中執行，客戶端應保持連線到任務結束；中途關閉客戶端造成程序中止時，已提交的資料與檔案日誌會保留，可在重新連線後查詢 interrupted 紀錄及執行復原。復原中斷操作與還原已完成改名是不同工具；目前沒有任意撤銷已完成搬移的工具。

搜尋自 Web 抽至 `gallery_query.py`，共享同一份條件、索引查詢與自然排序快取；MCP 查詢不建立縮圖工作。人工標籤自 Web 抽至 `gallery_tag_service.py`，共享全批 ID 檢查、交易重試、手動來源及增量統計。搬移預覽與正式執行共享 `gallery_service.plan_image_moves`，避免兩套命名邏輯。

修正背景工具完成回報早於作業鎖釋放的時序，確保查到完成時即可開始下一個寫入；新增專門測試。掃描每 100 個檔案更新進度並檢查取消，仍以每秒節流持久化。

## 客戶端模型與登入（2026-10-21）

使用者確認透過 Antigravity 操作，因此移除 Gemini API 設定分頁、金鑰儲存與圖庫直接請求 Gemini 的程式。舊 `/api/settings/gemini/test`、`/api/semantic/gemini_flash` 回傳 410 並指引使用 MCP，不送出網路請求；設定 API 拒收舊金鑰／模型欄位。舊設定檔中的 Gemini 欄位不會讀取或公開，下次儲存 MCP 設定時只保留有效 MCP 欄位；環境變數不作修改。

流程為「Antigravity 的代理→ 本專案 MCP → 圖庫」。登入、訂閱與雲端模型選擇在客戶端管理，本頁的「AI 模型」仍用於圖庫本機視覺模型。圖庫不會自動啟動客戶端；本次工作提供並驗證 MCP 工具，沒有另啟動 Gemini 執行正式分析。

MCP 管理提供 Antigravity 連線設定匯出，包含標準 command／args／cwd／env 及 disabled／disabledTools，使用圖庫實際的權限設定。依 [Antigravity MCP 文件](https://antigravity.google/docs/mcp) 產生設定，需合併至客戶端設定並重新整理工具。已移除其他 CLI 的設定匯出與畫師背景呼叫程式。

## 正式套用與 Antigravity 驗收（2026-10-21）

使用者明確要求「先備份資料庫，再重啟正式服務並套用更新與資料庫遷移，確認 Antigravity MCP 能正常使用」。正式圖庫為 `D:\圖檔\twitter`，服務為 `http://127.0.0.1:8765`。

- 已建立並驗證備份：`D:\圖檔\twitter\.tag_gallery\backups\before_update_20261021_020732.db`，147,152,896 bytes。
- 備份 SHA-256：`7b9711f3ffe8a6b238c58ae9e87af209e593bcbccf3406e9bdd6ef864e92d6f9`。
- 遷移版本 1、SQLite `quick_check=ok`、`foreign_key_check` 無錯誤。
- 遷移前後均為 31,147 張圖片、1,189,813 筆標籤關聯、4 筆人工標籤；人工標籤完整內容摘要一致。
- 正式服務已由專案現有背景啟動器，經 Windows 獨立背景程序啟動；驗收程序 PID 36188，並已驗證跨多次工具呼叫持續可用。PID 是本次紀錄，之後正常重啟會改變。
- 正式網頁以瀏覽器實測：Gemini API 分頁不存在、MCP 權限清單 24 個工具、Antigravity 匯出與握手成功，JavaScript 錯誤為 0。畫面：`docs/screenshots/012-live-mcp-desktop.png`。

實際發現 Antigravity 以 `mcp_<server>_<tool>` 組合模型工具名稱，原連線名稱 `twitter-tag-gallery` 使最長工具超過 64 字元並被拒收。已將匯出的連線名稱改為 `gallery`（最長組合 58 字元），並加入回歸測試。工具原始名稱與功能均保留。

在本次明確要求確認正常使用的範圍內，已先備份並修正 Antigravity 現有設定 `C:\Users\Gary\.gemini\config\mcp_config.json`；備份為同目錄的 `mcp_config.json.before-gallery-update-20261021_021037.bak`。只將本圖庫項目改成 `gallery` 並補齊 cwd／圖庫路徑；保留其他設定。透過 Antigravity 本機 MCP 重新整理介面載入後，其真實狀態為 `MCP_SERVER_STATUS_READY`、工具 24 個、非空工具錯誤 0 個。

另外依 Antigravity 的實際 stdio 設定，成功執行 initialize、tools/list、圖庫總覽、分頁圖片搜尋、圖片詳情及任務查詢。這些驗收使用唯讀工具，未啟動正式 AI 批次或搬移。驗收摘要保存在本工作區忽略的 `.tag_gallery/activation_20261021_020732.json`、`antigravity_verification.json` 及 `antigravity_client_state.json`，不包含登入權杖。
