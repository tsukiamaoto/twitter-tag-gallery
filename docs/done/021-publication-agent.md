# Antigravity 發布日期追查與 Prompt 管理

改名視窗預設為「追查原始發布日期」，按「開始背景追查與改名」建立持久化工作。瀏覽器關閉不會取消工作。Antigravity 桌面版需保持運作；離線或無法確認 Flash 實際版本為 Gemini 3.8 時，工作不會取得寫入資格。

## 執行與資料

- `agent_jobs.sqlite3` 保存工作、圖片範圍、Prompt 快照、對話 ID、實際模型與逐圖狀態。取消後重試產生新工作 ID，舊對話無法繼續提交。
- 官方 Sidecar `gallery-publication` 綁定目前圖庫的 Antigravity 專案，透過桌面本機原生入口建立專用對話，從即時模型目錄選擇 Gemini 3.8 Flash Medium；`agentapi` 僅讀取對話中繼資料。不複製登入資料。啟動前以實際 modelUsage 與圖庫總覽 MCP 成功結果驗證，不接受模型自述、要求選用的模型或 Prompt 文字作證。此原生介面屬桌面版本相容層，介面變動時停止領取工作並顯示錯誤。
- 模型協調與圖庫寫入鎖分開；來源查詢不佔用全域檔案作業鎖，改名沿用既有兩階段日誌與還原功能。
- `publication_files` 保存原始檔名、SHA256、MD5 與檔案版本；`publication_sources` 保存原作品網址、UTC 發布時間、作者及匹配依據。圖片內容變更會使舊證據失效。
- 原日期自動查證支援 X、Pixiv、Fanbox 原作品頁，Danbooru 是來源索引。MD5 匹配還須核對來源帳號與作品畫師；Pixiv ID 僅作候選。來源解析的作品頁發布時間用於命名，CDN 修訂日期、Danbooru 收錄時間、本機時間不作發布日期。
- 模型補查須提交原貼文、該作品的圖片 URL、作者頁及逐圖核對理由；後端重新解析原作品日期，不接受自由填寫日期。
- 各資料夾依最早已確認發布時間重排為 `YYYYMMDD_0001.ext`，日期換算台灣時間。未知圖片保留原名；衝突整夾回報，不使用 `_dup`。流水號預設四碼，必要時擴位。

## 介面與 MCP

AI 設定 → Tool 設定 → 工具管理提供全部工具的說明／Prompt 編輯。直接程式工具只有使用說明；模型流程另有共用任務 Prompt。儲存後下次 `tools/list` 與新工作生效；執行中的工作及續跑保留原 Prompt 版本。背景任務與復原顯示工作與完整 Prompt 快照。

新增 MCP 工具：`gallery_get_agent_job`、`gallery_trace_publication_dates`、`gallery_submit_publication_sources`、`gallery_complete_agent_batch`、`gallery_submit_artist_identity`。長任務仍回傳 `run_id`，以 `gallery_get_tool_run` 輪詢完成後再接續。每批最多 50 張。

停止桌面查證工作使用 `gallery_cancel_agent_job(agent_job_id)`。這個 ID 是橋接器提供給查證工具輸入的 `job_id`，啟動回應會另列為 `agent_job_id`；不同於共用圖庫作業的 `job_id`。啟動回應會附上 `cancel_tool` 與 `cancel_arguments`，包括沒有共用作業鎖、`job_id=null` 的查證工具。停止是合作式處理，會拒絕後續提交並在工作檢查點停止，不會強制中止已開始的網路或安全檔案步驟。

HTTP：`POST/GET /api/agent/jobs`、`GET /api/agent/jobs/{id}`、`POST /api/agent/jobs/{id}/stop|resume`、`PUT /api/settings/mcp/tools/{name}/prompt`。內部 Sidecar 入口驗證獨立橋接憑證，憑證不放入模型 Prompt。

安裝：先備份並遷移圖庫，再執行 `python antigravity_setup.py --project-id <目前圖庫 Antigravity 專案 ID>`。安裝器備份原設定，加入本 Sidecar、固定圖庫根目錄的背景 MCP server 及原圖庫 MCP 版本環境欄位。每個圖庫以不同背景 server 名稱隔離，實際 Prompt 快照固定指定該名稱。對話只授予本工作所需 MCP 與來源讀取權限，禁止命令及直接寫檔，並保留本機既有 Ask／Deny 規則。可由 MCP 管理與背景任務確認連線、版本及錯誤。未確認桌面版本時不可將其他模型視為通過。

## 驗證

離線暫存圖庫測試涵蓋日期、頁碼、來源核對、重排、衝突、還原、取消／續跑、內容變更、Prompt 版本、權限與設定保留。瀏覽器測試使用實際 UI 靜態資源及模擬 API，驗證桌面與手機的列表、編輯、背景按鈕及關閉視窗。


2026-10-24 驗收：17 項新後端測試、56 項既有後端回歸與桌面／390px 手機瀏覽器測試通過。既有 Antigravity 桌面實際以 Gemini 3.8 Flash Medium 完成圖庫唯讀 MCP 測試；獨立單圖測試圖庫的背景工作 `926ac43497c64cbdac0988ebaa023e57` 完成讀取工作、來源追查與批次完成，結果為未知 1 張、改名 0 張、標籤 0，保留 `synthetic-unknown.png`。暫時 Sidecar／MCP 已移除，證據保留於 `.tag_gallery/desktop-smoke/result.json`。正式圖庫尚未啟動整批追查或改名，由使用者按下按鈕啟動。
