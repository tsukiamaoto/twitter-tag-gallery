# 020 — 左側工作區工具列與 AI 設定中心

狀態：done  
完成日期：2026-10-24

## 導覽與功能歸類

- 最左側新增首頁與 AI 設定工具列，桌面寬 56px、800px 以下寬 48px；重新載入預設回首頁。
- 首頁沿用原圖庫；AI 設定以完整頁面顯示 AI 模型、MCP 管理、Tool 設定、Skill 管理。
- AI 模型保留圖片標籤模型與 GGUF 操作。MCP 管理負責啟用、唯讀、連線測試及匯出。
- Tool 設定包含工具管理與畫師身分兩個頁內分頁；工具管理收納權限、工具執行、紀錄與既有 Prompt 編輯器。
- Skill 管理收納現有影像修補技能，保留建立、複製、編輯、儲存與套用。
- 一般設定保留瀏覽偏好、操作日誌與紀錄、背景任務與復原；既有桌面背景查證狀態歸入背景任務。匯入排程已依 [022](022-schedule-workspace.md) 移至左側獨立工作區。

## 整合方式

`workspace-navigation.js` 在各設定模組初始化後安裝導覽控制器，移動原面板、頁簽及技能操作列，不複製 DOM。`GalleryApp.runtime.navigation` 管理工作區、AI 分類、Tool 子頁及可見狀態，透過 `workspace:change` 通知各模組。

- 切換保留圖庫篩選、搜尋、排序、頁碼、跨頁選取、側欄分頁與捲動位置，不另行查詢圖片；同次瀏覽記住 AI 分類。
- MCP 儲存只提交 `mcp_enabled`、`mcp_read_only`；工具權限只提交 `disabled_tools`。共用已儲存的設定，不讓另一頁的草稿覆寫權限。
- 沿用原後端 API 與資料格式。舊 `openSettings`／`switchSettingsTab` 的 AI 分類轉入新頁面；另提供 `openAiSettings`，接受 `model`、`mcp`、`tools`、`artists`、`tool-catalog`、`skills`、`image-skills`。
- 智慧標籤跳轉模型頁後，可返回原工作區並重新開啟智慧標籤；保留表單與原始觸發按鈕的焦點。
- 載入回應不覆寫已編輯的技能或工具 Prompt。模型、工具及背景查證輪詢依頁面可見狀態啟停，離開頁面不停止正在執行的工作。
- AI 頁面停用圖庫翻頁、刪除與搜尋快捷鍵；一般設定視窗仍保留 Escape 與焦點限制。
- 手機使用共用側欄抽屜，選取 AI 分類後關閉並將焦點移到內容標題；技能操作列固定於內容下方。

## 驗證結果

- `npm test`：38 項通過。
- `npm run test:browser`：11 組通過，包含新工作區、畫師身分、圖庫、智慧標籤、MCP／工具管理、刪除、詳細操作、拖曳選取、側欄標籤、篩選保存及影像技能。
- `node tests/folder-browser.spec.cjs`：資料夾搜尋、釘選、排序、手機抽屜與錯誤重試通過。
- 新工作區測試涵蓋 1440、1064、768、390、320px；既有圖庫驗證涵蓋 2507px 至 320px 的 15 種寬度。
- 驗證圖庫 DOM 與狀態保留、載入中輸入的草稿、權限分開儲存、工具與模型輪詢停止、舊入口轉向、智慧標籤返回及手機操作列可見性。
- 已檢視桌面與手機截圖；手機抽屜收合後完全移出畫面，沒有遮住窄工具列。

所有瀏覽器驗證使用模擬 API 與中性圖片，不啟動真實模型、排程或圖庫檔案操作。截圖位於忽略於 Git 的 `.tag_gallery/ui-review/`，例如 `workspace-model-1440.png`、`workspace-tools-1440.png`、`workspace-mcp-390.png` 與 `workspace-image-skills-390.png`。
