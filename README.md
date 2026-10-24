# Twitter & Pixiv AI 智慧標籤圖庫系統 (Twitter Tag Gallery)

目前新增的可靠性與設定功能請見 [012 驗收紀錄](docs/done/012-reliability-and-mcp.md)。

- 設定 → **MCP 管理**：圖庫 MCP 開關、唯讀模式、工具權限、本機握手診斷、Antigravity 設定匯出；「工具操作與執行紀錄」可查看各工具的參數格式、填入 JSON、實際執行並查看結果。
- 設定 → **畫師身分**：智慧標籤、Pixiv 反查與自動歸檔先查 Danbooru 的畫師 ID、別名與平台網址；查不到則交由 Antigravity 等 MCP 客戶端補查作者頁面連結，確認後以工具保存別名與共用資料夾。圖庫不啟動模型 CLI、不需要 API 金鑰；未確認者暫緩自動歸檔。此頁提供補查佇列與可複製的客戶端操作指令，見 [畫師跨平台身分](docs/done/019-artist-identity.md)。
- MCP 連線設定使用 **Antigravity**；登入與模型選擇在該客戶端管理，圖庫不使用 Gemini API 金鑰或直接呼叫模型 API。
- 模型與自動流程只寫入**畫師、角色、作品、來源**；人工標籤保留。模型不能透過 MCP 寫入一般描述、分級、年份或主題，更新後需重連 MCP，見 [018 驗收紀錄](docs/done/018-core-generated-tags.md)。
- 批次改名 → **追查原始發布日期**：按下後由既有 Antigravity 桌面版的 Gemini 3.8 Flash 在背景處理，以 Danbooru 協助找作者原貼文；確認後改為 `YYYYMMDD_0001.ext`，未知保留原名。AI → **Tool 設定**可搜尋全部工具、查看及編輯使用說明和模型任務 Prompt，見 [發布日期與 Prompt 管理](docs/done/021-publication-agent.md)。
- 設定 → **背景任務與復原**：跨程序進度、未完成項目重試、檔案操作復原與資料庫備份。
- 圖庫根目錄可由 `TWITTER_GALLERY_ROOT` 環境變數設定；預設仍為 `D:\圖檔\twitter`。
- 圖片詳情可單檔刪除，勾選圖片可跨頁批次刪除，支援 **Del** 快捷鍵與確認。檔案移到圖庫回收區，可「復原剛才刪除」並還原標籤；回收區尚未釋放磁碟空間。
- 支援 **Shift 點選範圍多選**，或按住滑鼠左鍵拖過一排圖片連續勾選；從已勾選的圖片開始拖曳可連續取消。
- 圖片卡片只顯示一位畫師；角色、作品與一般標籤在詳情查看。Web 列表使用精簡查詢，完整標籤仍可搜尋與篩選，見 [015 驗收紀錄](docs/done/015-compact-gallery-cards.md)。
- 左側標籤先載入 40 筆，接近清單底部時再追加下一批；搜尋仍涵蓋全部標籤，載入失敗可原地重試，見 [016 驗收紀錄](docs/done/016-sidebar-tag-pagination.md)。
- 已選標籤與左側標籤分類會自動儲存在此瀏覽器，重新整理或重開分頁後恢復；清除標籤與重設篩選也會記住清除結果，見 [017 驗收紀錄](docs/done/017-tag-filter-persistence.md)。
- 前端分成 `gallery-core.js`（請求／擴充介面）、`gallery-app.js`（圖庫）、`gallery-shell.js`（畫面控制）及 `integration-settings.js`（整合設定），CSS 另存於 `gallery-base.css`、`gallery-shell.css`。

工具定義與參數集中於 `tool_registry.py`，共用執行、作業鎖及紀錄在 `tool_runtime.py`，業務實作在 `gallery_tools.py`、`gallery_workflow_tools.py`；`mcp_server.py` 只處理協定。Web 與 MCP 共用 `gallery_query.py` 搜尋及 `gallery_tag_service.py` 標籤寫入。設定頁與 MCP 都會驗證已儲存的權限；寫入工具共用背景作業鎖，停止要求可在忙碌時送出，唯讀工具每個程序最多同時執行兩個。執行紀錄保留最近 200 次，每次最多 128 KiB 結果，不保存輸入參數。這個操作區管理本專案內建工具。

新增掃描、圖片搜尋／詳情、核心標籤編輯、本機 AI 打標、搬移預覽／執行、檔案復原、改名還原與任務查詢／停止工具。掃描、AI、搬移及復原會先回傳 `run_id`、`job_id`，使用 `gallery_get_tool_run` 查詢結果、`gallery_cancel_job` 送出停止要求；客戶端需保持 MCP 連線至任務結束。使用順序與完整工具參數見 [圖庫 MCP 流程](docs/done/012-reliability-and-mcp.md#圖庫-mcp-流程工具2026-10-21)。

背景工具的啟動回應另提供 `cancel_tool` 與 `cancel_arguments`，可直接用於停止。`run_id` 只查工具結果；非空的 `job_id` 控制共用圖庫作業；`agent_job_id` 控制桌面查證工作，使用 `gallery_cancel_agent_job`。桌面查證工具輸入的舊欄位 `job_id` 仍代表桌面工作，不能混用共用作業 ID。批次標籤工具禁止空篩選；反查與畫師校正預設不搬移，畫師校正也不預設清除 AI 畫師標籤。去重的視覺比對排除動畫，細微差分仍需檢視；動畫只參與位元組完全相同的比對。

開發驗證：先執行 `python -m pip install -r requirements-dev.txt` 與 `npm install`，再執行 `python -B -m unittest discover -s tests -p "test_*.py"`、`npm test`、`npm run test:browser`。瀏覽器測試使用本機 Microsoft Edge 與模擬 API。首次啟動新版會先備份既有資料庫再套用相容性遷移，舊 MCP 客戶端也需要重新連線。

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688.svg)](https://fastapi.tiangolo.com)
[![DirectML / CUDA](https://img.shields.io/badge/Hardware%20Accel-DirectML%20%7C%20CUDA-purple.svg)](https://onnxruntime.ai/)
[![MCP Ready](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-green.svg)](https://modelcontextprotocol.io/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

專為本機數萬張 Twitter (X)、Pixiv 與二次元動漫插圖打造的 **高效能 AI 智慧圖庫與標籤管理系統**。

本系統結合了 **FastAPI SPA 現代化 Web 介面**、**本機 AI 視覺標籤打標 (Camie Tagger v2)**、**多來源以圖搜圖與標籤校正 (Danbooru MD5 / IQDB / SauceNAO)**、**語意搜尋與繁體中文在地化**、**安全重命名與智慧歸檔**、**二次元去馬賽克修復**，以及支援 LLM Agent 協作的 **Model Context Protocol (MCP) 伺服器**。

---

## 📑 目錄

- [核心功能特色](#-核心功能特色)
- [系統架構](#-系統架構)
- [專案結構](#-專案結構)
- [快速上手](#-快速上手)
  - [1. 安裝環境與依賴](#1-安裝環境與依賴)
  - [2. 配置圖庫目錄](#2-配置圖庫目錄)
  - [3. 啟動 Web 服務](#3-啟動-web-服務)
- [進階模組說明](#-進階模組說明)
  - [以圖搜圖與標籤校正引擎](#-以圖搜圖與標籤校正引擎-reverse_searchpy)
  - [本機 AI 視覺打標與硬體加速](#-本機-ai-視覺打標與硬體加速-ai_taggerpy)
  - [智慧重命名與一鍵還原](#-智慧重命名與一鍵還原-renamerpy)
  - [MCP Server (AI Agent 整合)](#-mcp-server-ai-agent-整合-mcp_serverpy)
- [授權與免責聲明](#-授權與免責聲明)

---

## ✨ 核心功能特色

### 🖼️ 1. 極速現代化 Web 圖庫介面
- **高效能瀑布流**：針對 30,000+ 張插圖進行虛擬滾動與縮圖快取優化，秒開秒刷。
- **複合標籤檢索**：點擊加入／移除標籤，支援多標籤交叉檢索。
- **分類導航**：將標籤分為「畫師 (Artist)」、「角色 (Character)」、「原作作品 (Copyright)」、「外貌/服裝/屬性 (General)」、「主題 (Theme)」等分類。
- **鍵盤快捷瀏覽**：支援左右箭頭換頁、空格鍵縮放、Esc 退出與快捷標籤操作。
- **一鍵畫師歸檔**：點擊畫師標籤即可建立專屬資料夾並將圖片自動移入歸檔。

### 🔍 2. 多重以圖搜圖與標籤校正 (Reverse Search & Calibration)
- **Danbooru MD5 精準比對**：無需上傳圖片，計算本地檔案 MD5 直接命中 Danbooru 資料庫，秒級獲取 100% 官方認證之畫師、Twitter `@帳號`、Pixiv ID、角色與完整標籤。
- **IQDB 視覺比對**：對於未完全命中 MD5 的插圖，自動透過 IQDB 多圖庫 (Danbooru / Yande.re / Gelbooru) 比對相似圖。
- **SauceNAO 整合**：精準反查推特推文狀態 URL 與 Fanbox / Fantia 作者。
- **標籤自動校正 (Tag Calibration)**：確認真實畫師後，自動清除本機 AI 低信心度猜錯的畫師與錯誤標籤。

### 🧠 3. 本機 AI 視覺辨識引擎 (AI Vision Tagger)
- **Camie Tagger v2 / WD14 ONNX 模型**：內建 70,527+ 標籤與 7,007+ 位二次元繪師風格辨識能力。
- **硬體加速支援**：原生支援 Windows DirectML (AMD Radeon RX 系列顯卡) 與 NVIDIA CUDA 加速。
- **Pixiv 原生解析**：針對檔名包含 Pixiv ID 的圖片，直接解析作品並取得原作者資訊。
- **高效批次打標**：提供平行加速多執行緒打標指令碼 (`fast_bulk_artist_tagger.py`)。

### 🌐 4. 語意搜尋與繁體中文在地化
- **雙語字典映射**：內建 Danbooru 英文標籤轉繁體中文對照字典 (`zh_dict.py`)。
- **語意概念搜尋**：輸入「黑絲」、「泳裝海灘」等自然語言概念，系統自動展開關聯標籤進行多維度圖片推薦。
- **共現相關性矩陣**：計算標籤之間的 Jaccard 相似度與條件機率 $P(B|A)$。

### 📁 5. 智慧重命名與交易安全還原 (Safe Renamer & Undo)
- **Windows 自然排序編號**：支援將混亂的隨機檔名或推特截圖標準化為 `YYYYMMDD_001.ext`。
- **完整交易日誌**：每次重新命名操作均記錄於 JSON 日誌中，支援一鍵批次還原 (Undo)。

### 🎨 6. 二次元去馬賽克修復引擎 (Decensor Engine)
- **YOLOv11-seg + YOLOv8 像素級遮罩偵測**：多尺度鎖定二次元馬賽克與黑條部位。
- **本機 GGUF 模型局部重繪**：透過本機 GGUF 擴散修復模型 (FLUX.2 klein 4B / Qwen-Image-2.1) 與主題修復技能進行細緻影像修復，支援顯卡記憶體一鍵釋放。

### 🤖 7. Model Context Protocol (MCP) 伺服器
- 內建標準 MCP 伺服器 (`mcp_server.py`)，可無縫連接 Claude Desktop、Gemini Flash 或 Antigravity Agent。
- 提供 10+ 個 AI 工具，支援 Agent 自動分析圖庫標籤相關性、翻譯未翻譯標籤與執行深度校正。

---

## 🏛️ 系統架構

<p align="center">
  <img src="docs/architecture.svg" alt="Twitter & Pixiv AI 智慧標籤圖庫系統架構圖" width="100%">
</p>

### 1. 分層架構詳細流程 (Layered Architecture Flow)

```mermaid
flowchart TD
    %% Clients Layer
    subgraph Layer1 ["01 / 介面與外部整合層 (Presentation & Clients)"]
        WebUI["SPA 響應式前端<br/>(static/index.html)<br/>• 虛擬化瀑布流<br/>• 複合標籤篩選（加入／移除）<br/>• 快捷鍵與縮放檢視"]
        MCPClient["LLM Agent 介面<br/>(mcp_server.py Stdio)<br/>• Claude Desktop<br/>• Gemini Flash 3.8<br/>• Antigravity Subagents"]
    end

    %% Gateway & Core Layer
    subgraph Layer2 ["02 / 核心服務與排程調度層 (API Gateway & Scheduler)"]
        FastAPI["FastAPI 應用伺服器 (app.py:8765)<br/>• /api/images (分頁、篩選、排序)<br/>• /api/tags & /api/reverse-search<br/>• /api/rename & /api/move"]
        Scheduler["背景守護排程器 (scheduler.py)<br/>• 增量目錄掃描監控<br/>• 縮圖非同步快取佇列"]
    end

    %% Processing Layer
    subgraph Layer3 ["03 / 核心業務與分析引擎 (Core Processing Engines)"]
        SemanticEng["語意檢索引擎<br/>(semantic_engine.py)<br/>• Jaccard 共現矩陣<br/>• 自然語言標籤展開"]
        ZhDict["繁中字典在地化<br/>(zh_dict.py)<br/>• 10,000+ 標籤對照<br/>• 畫師資料夾標準化"]
        Renamer["安全重命名系統<br/>(renamer.py)<br/>• Windows 自然排序<br/>• 交易日誌與 1 鍵還原"]
        Decensor["去馬賽克修復引擎<br/>(decensor_engine.py)<br/>• YOLOv11-seg 遮罩偵測<br/>• GGUF 模型局部重繪"]
    end

    %% AI & Reverse Search Layer
    subgraph Layer4 ["04 / AI 視覺打標與多重反查校正 (AI Vision & Reverse Search)"]
        direction TB
        AIEngine["本機 AI 視覺打標 (ai_tagger.py)<br/>• Camie Tagger v2 (ViT 70,527 標籤)<br/>• 7,007 位畫師風格識別<br/>• DirectML / CUDA GPU 硬體加速"]
        
        subgraph RevSearchChain ["以圖搜圖與標籤校正鏈 (reverse_search.py)"]
            MD5Lookup["1. Danbooru MD5 零頻寬雜湊比對<br/>(100% 官方認證畫師 / Twitter @ / Pixiv ID)"]
            IQDBLookup["2. IQDB 視覺特徵比對<br/>(Danbooru / Yande.re / Gelbooru)"]
            SauceLookup["3. SauceNAO API 反查<br/>(推文狀態 URL / Fanbox 作者)"]
            Calibrate["4. 標籤自動校正 (Tag Calibration)<br/>(權威覆蓋並自動清除 AI 猜錯之標籤)"]
        end
    end

    %% Storage Layer
    subgraph Layer5 ["05 / 資料持久化與儲存層 (Storage & Persistence)"]
        LocalFiles[("原始圖片庫 (D:\\圖檔\\twitter)<br/>31,000+ 圖片檔案")]
        DB[("SQLite 資料庫 (gallery.db)<br/>WAL 模式 · 關聯式正規化")]
        ThumbDir[("縮圖快取目錄 (.tag_gallery/thumbs)<br/>高品質 WebP / JPEG")]
        HistLog[("交易歷史日誌 (rename_history.json)<br/>重命名歷程備份")]
    end

    %% Inter-layer connections
    WebUI <-->|HTTP REST / JSON| FastAPI
    MCPClient <-->|Stdio JSON-RPC| FastAPI
    
    FastAPI <--> Scheduler
    FastAPI --> SemanticEng
    FastAPI --> ZhDict
    FastAPI --> Renamer
    FastAPI --> Decensor
    
    FastAPI <--> AIEngine
    FastAPI <--> RevSearchChain
    
    Scheduler --> LocalFiles
    Scheduler --> DB
    Scheduler --> ThumbDir
    
    Renamer <--> LocalFiles
    Renamer --> HistLog
    
    AIEngine --> DB
    RevSearchChain --> DB
    FastAPI <--> DB
```

### 2. 以圖搜圖與標籤自動校正流程 (Reverse Search & Calibration Flow)

```mermaid
flowchart TD
    Start(["選定圖片進行反查 / 校正"]) --> Step1["計算本地檔案 MD5 雜湊值"]
    
    Step1 --> CheckMD5{"查詢 Danbooru MD5 API<br/>是否命中？"}
    
    CheckMD5 -- "是 (100% 精準命中)" --> FetchMeta["取得官方認證完整中繼資料：<br/>• 認證畫師名 & 漢字別名<br/>• 原作者 Twitter @帳號 / Pixiv ID<br/>• 角色 (Character) & 原作 (Copyright)<br/>• 官方 Danbooru 標籤集"]
    
    CheckMD5 -- "否 (無 MD5 紀錄)" --> Step2["產生低解析搜尋縮圖並提交 IQDB 視覺比對"]
    
    Step2 --> CheckIQDB{"IQDB 是否找到<br/>相似度 &gt; 80% 插圖？"}
    
    CheckIQDB -- "是" --> FetchBooru["串接 Yande.re / Gelbooru / Danbooru Post API<br/>擷取結構化標籤與作者"]
    
    CheckIQDB -- "否" --> Step3["調用 SauceNAO API<br/>進行特徵指紋反查"]
    
    Step3 --> CheckSauce{"SauceNAO 是否命中？"}
    CheckSauce -- "是" --> ExtractSauce["解析推文 URL、Pixiv 繪師或 Fanbox 原作者"]
    CheckSauce -- "否" --> FallbackAI["保留本機 Camie Tagger v2 AI 預測標籤<br/>(標記為待人工或 Agent 複審)"]
    
    FetchMeta --> CalibrateProc["執行標籤自動校正 (Tag Calibration)：<br/>1. 清除先前本機 AI 低信心度猜錯的畫師<br/>2. 寫入 100% 認證畫師與推特帳號標籤<br/>3. 自動補齊繁中翻譯對照"]
    FetchBooru --> CalibrateProc
    ExtractSauce --> CalibrateProc
    
    CalibrateProc --> MovePrompt{"是否啟用自動歸檔？"}
    MovePrompt -- "是" --> AutoMove["自動依畫師名稱建立/移至專屬資料夾<br/>並套用 Windows 自然序號重命名"]
    MovePrompt -- "否" --> UpdateDB[("更新 SQLite 資料庫 (gallery.db)")]
    AutoMove --> UpdateDB
    FallbackAI --> UpdateDB
    UpdateDB --> Done(["完成！Web UI 與 MCP 即時同步更新"])
```

---

## 📂 專案結構

```text
twitter-tag-gallery/
├── app.py                         # FastAPI 主要 Web 服務與 API 路由
├── database.py                    # SQLite 資料庫連線、Schema 與底層掃描邏輯
├── ai_tagger.py                   # 本機 ONNX 視覺打標引擎 (Camie Tagger v2)
├── reverse_search.py              # 以圖搜圖 (Danbooru MD5, IQDB, SauceNAO)
├── fast_bulk_artist_tagger.py     # 高速批次畫師分類與平行打標工具
├── parallel_artist_tagger.py      # 多執行緒畫師標籤處理工具
├── semantic_engine.py             # 標籤相關性 (Jaccard) 計算與語意搜尋展開
├── zh_dict.py                     # Danbooru 標籤繁體中文翻譯字典與資料夾映射
├── build_full_enrichments.py      # 標籤繁中字典與高階主題叢集建置腳本
├── renamer.py                     # 安全檔案批次重命名與 1 鍵還原系統
├── decensor_engine.py             # YOLOv11-seg 像素級遮罩偵測 + GGUF 模型局部修復
├── scheduler.py                   # 背景監控與增量掃描排程器
├── mcp_server.py                  # Model Context Protocol (MCP) 伺服器
├── requirements.txt               # Python 依賴清單
├── static/
│   └── index.html                 # 現代化單頁 Web 圖庫前端 (SPA)
├── run_gallery.bat                # 啟動 Web 服務捷徑腳本
├── stop_gallery.bat               # 停止背景服務腳本
├── start_background.pyw           # 背景無終端機視窗靜默啟動器
└── start_silent.vbs               # VBScript 靜默啟動包裝
```

---

## 🚀 快速上手

### 1. 安裝環境與依賴

確保本機已安裝 **Python 3.10 或以上版本**：

```bash
git clone https://github.com/tsukiamaoto/twitter-tag-gallery.git
cd twitter-tag-gallery

# 建立並啟用虛擬環境 (建議)
python -m venv venv
venv\Scripts\activate

# 安裝核心依賴
pip install -r requirements.txt
```

> **DirectML 硬體加速（AMD GPU 專用）**：
> 若使用 AMD 顯示卡進行 AI 模型推論加速，請安裝：
> ```bash
> pip install onnxruntime-directml
> ```

### 2. 配置圖庫目錄

在 [`database.py`](file:///h:/code/twitter-tag-gallery/database.py#L12) 中設定您的圖片庫所在路徑：

```python
# database.py
GALLERY_ROOT = Path(r"D:\圖檔\twitter")  # 請替換為您的本地圖片目錄
```

系統啟動時會在該目錄下自動建立 `.tag_gallery/` 資料夾，儲存 SQLite 資料庫 (`gallery.db`)、縮圖快取與 AI 模型。

### 3. 啟動 Web 服務

直接執行啟動腳本或透過 Python 執行：

```bash
# 方式 A：直接執行 Python
python app.py

# 方式 B：使用批次檔背景啟動 (Windows)
run_gallery.bat
```

開啟瀏覽器造訪：**[http://127.0.0.1:8765](http://127.0.0.1:8765)**

---

## 🛠️ 進階模組說明

### 🔍 以圖搜圖與標籤校正引擎 (`reverse_search.py`)
- 當圖庫中部分圖片由本機 AI 猜測的信心度較低時，可使用以圖搜圖工具：
  - 計算本地圖片 MD5 直接向 Danbooru 請求查詢，無須耗費頻寬上傳完整圖檔。
  - 自動抽取 Pixiv Artwork ID、Twitter 貼文 ID、Fanbox 作者與原作角色資訊。
  - 一旦取得權威資料，自動清除誤判標籤並更新至資料庫。

### 🧠 本機 AI 視覺打標與硬體加速 (`ai_tagger.py`)
- 首次掃描或新增大量圖片時，可調用批次打標腳本：
  ```bash
  python fast_bulk_artist_tagger.py
  ```
- 支援動態切換 CPU / DirectML / CUDA 執行提供者，兼具辨識精準度與記憶體釋放控制。

### 📁 智慧重命名與一鍵還原 (`renamer.py`)
- 將特定繪師資料夾內的檔案依照自然日期與序號規範化：
  - 格式範例：`20240501_001.png`、`20240501_002.jpg`。
  - 執行前可透過 API 或前端預覽變更計畫，每次重命名皆保留交易 Log，支援錯誤操作時一鍵 Undo 復原。

### 🤖 MCP Server (AI Agent 整合) (`mcp_server.py`)
Twitter Tag Gallery 實作了 **Model Context Protocol (MCP)**，可供 Claude Desktop 或 Antigravity 代理呼叫。

在 Claude Desktop 或 Antigravity 設定檔中加入：

```json
{
  "mcpServers": {
    "gallery": {
      "command": "python",
      "args": ["H:\\code\\twitter-tag-gallery\\mcp_server.py"]
    }
  }
}
```

**支援的 MCP 工具**：
- `gallery_get_overview`：圖庫總量、畫師分佈與標籤統計。
- `gallery_find_tag_correlations`：查詢標籤共現性與 Jaccard 關聯度。
- `gallery_semantic_search`：自然語言展開之語意搜尋。
- `gallery_list_untranslated_tags`：列出待翻譯標籤供 LLM 批次處理。
- `gallery_apply_gemini_enrichments`：更新既有標籤的繁中翻譯；自動主題群組已停用。
- `gallery_batch_tag_or_move`：依標籤篩選、批次加標籤及搬移。
- `gallery_list_candidates_for_artist_calibration`：列出待驗證的畫師圖片。
- `gallery_reverse_image_search`：單張圖片來源反查與標籤校正。
- `gallery_deep_reverse_search_batch`：深度以圖搜圖與標籤校正。
- `gallery_calibrate_image_artist_and_tags`：寫入已驗證畫師、角色與作品標籤。
- `gallery_purge_all_low_conf_ai_artists`：清除低信心度 AI 畫師及修正誤建資料夾。
- `gallery_auto_organize_artist_folders`：預覽或執行畫師歸檔。

---

## 📄 授權與免責聲明

- 本專案採用 **[MIT License](LICENSE)** 授權。
- 本軟體僅供個人本地圖片整理與學術研究用途。請遵守相關網站（Twitter / Pixiv / Danbooru / SauceNAO）之使用條款與版權規範。
