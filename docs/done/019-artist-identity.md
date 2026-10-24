# 畫師跨平台身分與 Antigravity MCP 補查

智慧標籤、單張 AI、Pixiv 作者反查、搜圖及自動歸檔共用持久化畫師身分。先查本機已確認關係，再查 Danbooru 的畫師 ID、別名及平台網址；查不到時加入 `pending_model` 佇列，由 Antigravity 的模型透過 MCP 補查。模型由 Antigravity 對話提供，圖庫不啟動背景模型，也不需要 API 金鑰。

## 設定與操作

1. 重新啟動圖庫服務，並在 Antigravity 重新整理或重連圖庫 MCP，以取得新的畫師身分確認工具。
2. 設定 → **MCP 管理** 可產生 Antigravity 的 `mcpServers.gallery` 連線設定。新版 Antigravity 全域設定在 `~/.gemini/config/mcp_config.json`，也可使用專案的 `.agents/mcp_config.json`；合併時保留其他伺服器設定。
3. 設定 → **畫師身分** 查看待補查佇列，複製補查指令到已連線的 Antigravity 對話。模型登入、模型選擇及搜尋能力由 Antigravity 管理。
4. 模型讀取 `gallery_get_artist_identity_status`，搜尋並閱讀作者首頁，再呼叫 `gallery_confirm_artist_identity` 保存已確認的帳號關係；沒有依據者保留待查。
5. 後續智慧標籤和歸檔自動重用已記住的別名及資料夾。身分確認只保存對照，不直接搬移既有圖片。

可貼到 Antigravity 的指令：

> 請使用圖庫 MCP 的 gallery_get_artist_identity_status 讀取待補查畫師。搜尋並閱讀作者首頁，確認各平台帳號之間的明確連結；確認後呼叫 gallery_confirm_artist_identity，提交 request_key、canonical_name、aliases、profile_urls 及 evidence（source_url、linked_url）。沒有足夠依據就保留待查，不以名稱或畫風猜測，不啟動其他 CLI，也不要搬移圖片。完成後回報確認與略過數量。

## 資料及歸檔規則

- 以 Pixiv 使用者 ID、Twitter/X 帳號或 ID、Fanbox creator 帳號辨認；作品與貼文 ID 不當成作者 ID，新舊網址會正規化。
- Danbooru 多筆符合結果不自動選第一筆；同名畫師可有不同身分與資料夾。不同既有身分的帳號衝突時不自動合併。
- 模型確認必須對應既有待查 `request_key`、涵蓋原始查詢，至少兩個不同平台帳號，並有作者頁面的 `source_url → linked_url` 依據連接全部帳號。名稱、畫風、轉載或搜尋摘要不足以確認。網頁依據由模型查證，伺服器驗證帳號與依據結構。
- 優先沿用確切別名對應的現有資料夾；有多個時選現存圖片最多者，再依名稱穩定排序。確認後固定使用此資料夾。
- 未確認的圖片暫緩自動歸檔，不建立猜測資料夾。既有重複資料夾不會整批搬移；搬移沿用可復原日誌、不覆寫同名檔案。人工標籤保留。
- 側欄及標籤篩選共用已保存的身分與別名。一般標籤不因畫師別名而被替換。

## 工具介面

- `gallery_resolve_artist_identity(artist_name, profile_urls, force_refresh)`：查本機／Danbooru，背景回傳 `run_id`。未確認時回傳 `pending_model`、`request_key` 與下一步指引。
- `gallery_get_artist_identity_status(limit)`：唯讀回傳成功身分數、待補查數量、名稱、平台網址及 `request_key`。
- `gallery_confirm_artist_identity(request_key, canonical_name, aliases, profile_urls, evidence)`：保存模型查證、標記待查完成，遵守 MCP 唯讀／工具權限與共用作業鎖。
- `GET /api/settings/artist-identity`：設定頁讀取 MCP 模式與佇列；舊背景模型呼叫程式、CLI 設定檔與設定寫入介面均已移除。

## MCP 補查與驗證

使用者在 Antigravity 對話中操作圖庫 MCP，模型登入與選擇由 Antigravity 管理。連線設定見 [Antigravity MCP 文件](https://www.antigravity.google/docs/mcp)。

測試涵蓋：Danbooru 未命中佇列、MCP 查證回寫、依據／原始查詢驗證、唯讀權限、跨程序持久化、別名資料夾重用、人工標籤保留，以及確認流程不會啟動模型子程序、舊模型設定寫入介面已移除。真實公開作者資料的煙霧測試使用暫存圖庫與標準 MCP 客戶端，並非代替使用者在 Antigravity 內執行模型。

真實資料煙霧測試已通過：`米山舞 / yoneyamai` 在 Danbooru 未命中；Pixiv 使用者 `1554775` 的公開資料直接連到 `twitter.com/yoneyamai`。透過 MCP 保存後，`@yoneyamai` 重用「米山舞」資料夾，僅搬移暫存測試圖片，待查佇列清空、身分總數保持 1。紀錄：`.tag_gallery/identity-mcp-smoke-911e8320/verification.json`。

清理後的前端單元測試 38 項通過；獨立的 `tests/artist-identity-settings.spec.cjs` 驗證 Antigravity 設定匯出、MCP 佇列、補查指令、無 CLI 控制項及手機排版。Python 全套執行 174 項，唯一錯誤為仍預期舊匯出格式的測試斷言；已改為驗證 Antigravity 的 disabled／disabledTools，補跑畫師與 MCP 整合測試 25 項均通過。完整設定頁操作台測試先前仍有執行按鈕停用的逾時，未列為本次通過項目。

本機清理已移除 CLI 專用的登入憑證、使用者設定、狀態、信任目錄、專案與會話紀錄，以及圖庫的舊模型設定檔；保留 Antigravity 目錄、共用記憶檔及 MCP 設定，MCP 設定的 SHA-256 未變。已在閒置時重新載入圖庫服務，驗證畫師狀態僅提供 MCP 模式、舊設定寫入路由消失、Antigravity 匯出正常。啟用紀錄：`.tag_gallery/mcp_only_activation_20261024_025104.json`。
