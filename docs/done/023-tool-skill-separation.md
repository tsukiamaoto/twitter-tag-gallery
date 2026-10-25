# 023 Tool 與 Skill 解耦

更新日期：2026-10-24

## 行為

- Tool 只管理程式能力、參數、權限及可編輯的工具說明。工具目錄不再包含 `workflow` 或 `task_prompt`，工具儲存與 MCP 工具清單不讀寫 Skill。
- Skill 管理直接讀取專案 `.agents/skills/<id>/SKILL.md`。影像修補已整合為同一清單內的 Agent Skill，並可指定執行模型，詳見 [024](024-image-agent-skills.md)。
- 四個預設 Agent Skill：`gallery-deduplication`、`twitter-tag-correlation`、`gallery-artist-identity`、`gallery-publication-tracing`。
- 編輯視窗提供完整 Markdown、複製、重新載入、Ctrl/Cmd+S、草稿保留與錯誤提示。卡片的「檔案可讀」只表示檔案狀態，不宣稱外部客戶端已載入。
- 原有去重預覽及畫師待查佇列保留獨立操作入口。Tool 視窗不再依工作流程搬動操作面板或顯示完整任務 Prompt。

## API 與儲存

- `GET /api/agent-skills` 列出實際技能；`GET /api/agent-skills/{id}` 讀取全文；`PUT /api/agent-skills/{id}` 以 `content`、`revision` 儲存既有檔案。
- `revision` 為原始檔案的 SHA-256，過期版本回傳 409；非法 frontmatter、重新命名及空白正文回傳 422。損壞的 UTF-8 Markdown 可讀取並修正；無法解碼或檔案缺失則明確顯示錯誤。
- 驗證解析後路徑留在技能根目錄內，包含符號連結／Windows junction；寫入使用共用鎖、備份與原子替換。備份位於技能根目錄 `.storage/backups/<id>/<revision>.md`，不列入 Git。
- Tool 說明 API 保留版本控制與重設功能。傳入舊 `task_prompt` 欄位會回傳 422，不修改任何流程。
- 一般 Agent Skill 僅提供編輯既有文件；影像修補 Skill 可透過專用表單新增與複製。此介面不提供重新命名技能 ID。

## 背景任務與舊資料遷移

- 任務層選擇 Skill：`publication` 使用發布日期技能，`artist_identity` 使用畫師身分技能；Tool 不儲存技能關聯。
- 建立工作時凍結技能正文、技能 ID、內容版本及帶 JSON 工作資料的 `rendered_prompt`。既有任務與續跑沿用舊快照；建立新任務才讀取新文件。缺失或損壞的 Skill 阻止新任務，但不妨礙既有快照續跑。
- 開啟 Agent Skill 清單或建立新背景任務時，檢查目前圖庫的舊 `tool_prompts.json`。先備份至該圖庫資料目錄 `backups/tool_prompts_before_skills_<hash>.json`。
- 舊自訂流程匯入為 `<default-id>-imported-<hash>`，不覆寫四個預設技能。背景任務的匯入版本綁定及遷移紀錄存於該圖庫 `agent_workflows.json`；後續可在 Skill 管理編輯該匯入版本。
- 全部匯入與任務綁定完成後才移除舊 `tasks`。中斷可重跑，不重複匯入；未知流程或匯入內容衝突保留原資料並顯示錯誤。工具自訂說明與其他圖庫不受遷移影響。

## 驗證

- 本次新增 14 項隔離後端測試，涵蓋解耦、讀寫、版本衝突、鎖定、外部編輯競態、寫入失敗、路徑越界、遷移恢復、跨圖庫隔離及任務快照。
- 完整 Python 回歸：213 項通過；JavaScript 單元測試：38 項通過；瀏覽器回歸：14 組通過。
- 四份技能通過 skill-creator 格式驗證。Skill 編輯視窗已檢查桌面與手機截圖，並驗證 1440、768、390、320 px 寬度，沒有水平溢出且儲存按鈕可見。
- 所有寫入測試使用暫存圖庫與技能副本；瀏覽器以 mock API 驗證。未以正式圖片進行清理或搬移。
- Windows 沙箱會阻擋暫存檔原子替換及無頭 Edge 啟動，測試在核准的沙箱外執行。測試用 `httpx` 安裝於專案隔離的 `.tag_gallery/test-deps`，正式新增依賴為 `PyYAML`。

## 套用

重新啟動圖庫後端並重新整理頁面。已連線的 MCP 客戶端需更新工具清單；外部 AI 客戶端依自身機制重新載入技能。既有背景任務保持原流程快照。
