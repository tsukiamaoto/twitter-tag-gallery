# WebUI 改善任務

更新日期：2026-10-21

## 工作方式

- `todo/`：已確認需求、尚未開始。
- `doing/`：正在實作或驗證；未完成的驗收項目保留未勾選。
- `done/`：實作與驗收都完成，文件包含驗證結果與限制。
- 同一任務只保留一份文件，依實際進度移動目錄，保留編號。
- 發現阻礙時記在原任務中，不因為已寫程式就標成完成。

## 本次範圍

1. 實際檢查 WebUI 與原始碼，記錄資訊層級問題。
2. 設定中心與排程介面簡化。
3. 已選標籤的可見性與取消操作。
4. 每頁數量與方向鍵翻頁。
5. 圖片停留預覽。

狀態與驗收結果以各目錄中的任務文件為準。

## 目前進度

| 任務 | 狀態 | 紀錄 |
| --- | --- | --- |
| 001 現況分析 | done | [分析結果](done/001-webui-review.md) |
| 002 設定中心與排程 | done | [實作與驗收](done/002-settings-and-schedule.md) |
| 003 標籤取消操作 | done | [實作與驗收](done/003-active-tag-filters.md) |
| 004 數量與快捷分頁 | done | [實作與驗收](done/004-pagination-preferences.md) |
| 005 停留預覽 | done | [實作與驗收](done/005-image-hover-preview.md) |
| 006 主畫面與管理工具 | done（另一個聊天） | [整合與驗收](done/006-header-management-tools.md) |
| 007 現代 Windows 資料夾選取 | done | [實作與驗收](done/007-windows-folder-picker.md) |
| 008 改名預覽與連線池 | done（待後端重新載入） | [實作與驗收](done/008-rename-preview-pool.md) |
| 009 智慧標籤與模型設定 | done | [實作與驗收](done/009-smart-tag-ui.md) |
| 010 標籤、背景任務與縮圖效能 | done（已套用正式服務） | [實作與驗收](done/010-tag-filter-performance.md) |
| 011 資料夾搜尋與常用捷徑 | done（已套用正式服務） | [實作與驗收](done/011-folder-search-shortcuts.md) |
| 012 資料一致性、效能與 MCP 管理 | done（已備份、遷移並驗證 Antigravity MCP） | [實作與驗收](done/012-reliability-and-mcp.md) |

新需求從 `todo/` 開始；正在實作或驗證的任務放在 `doing/`，通過驗收後移至 `done/`。空目錄以 `.gitkeep` 保留。

## 驗證方式

這次四項功能的隔離測試：`node --test tests/webui.test.cjs`，共 10 項通過。另已在本機 WebUI 實際驗證設定切換、篩選、每頁數量保存、方向鍵及停留預覽。驗收沒有啟動真實排程搬移或改寫 Windows 排程。

瀏覽偏好儲存在瀏覽器 localStorage；排程沿用圖庫內 `.tag_gallery/scheduler_config.json`。

007 追加驗證：JavaScript 13 項、Python 原生選取器 5 項通過。使用者已確認現代 Windows 視窗的外觀，正式網頁的開啟／取消流程亦通過。
