# 017 繪師名稱顯示與別名合併

狀態：done（已套用正式服務）  
完成日期：2026-10-21

## 問題與修正

掃描資料夾與外部打標會分別建立繪師名稱、Danbooru 名稱及別名；原本標籤清單逐筆顯示，既有翻譯還帶有「現有繪師：」，使同一位繪師重複出現且名稱容易被截斷。

- `zh_dict.py` 統一繪師顯示名稱，同時處理已儲存的舊前綴、大小寫及既有資料夾的底線名稱。
- `gallery_artist_service.py` 依明確的繪師名稱對照與相同顯示名稱合併別名；僅合併 artist 類別。
- 清單排序、搜尋與分頁之前完成合併；「最近」以整組別名的最新圖片時間排序。
- 多別名繪師以不同 image_id 計數。同一張圖片同時帶多個別名只計算一次；查詢使用 tag_id 索引並快取結果。
- Web 與 MCP 的圖片篩選共用別名展開：同一繪師內取聯集，多個條件仍取交集，排除條件涵蓋全部別名。
- 側欄繪師只顯示名稱，省略原始英文副標；卡片點選與既有別名選取會統一成同一個可取消條件。
- 原始標籤、來源與信心度保留，詳情仍能管理個別標籤。資料庫不需遷移。

## 驗證

- `python -B -m unittest discover -s tests -p 'test_*.py'`：126 項通過，包含新增的 7 項繪師名稱、去重計數、搜尋、交集／聯集、排除、最近排序／完整回退及快取失效測試。
- `npm test`：34 項通過，包含側欄名稱與別名選取／取消。
- `node tests/gallery-shell.spec.cjs`：15 種視窗寬度通過。
- 正式 API 完整讀取 3,837 筆繪師列，顯示名稱沒有重複或舊前綴。Hara 的三個別名皆篩選出 1,111 張，Coro 的兩個別名皆為 1,153 張，タカハル為 791 張，加瀬大輝為 641 張，與清單計數一致。
- 正式瀏覽器確認首批 40 筆繪師沒有英文副標或重複列，點選 Hara 顯示一個條件與 1,111 張結果，無 JavaScript 錯誤。

驗證紀錄：`.tag_gallery/ui-review/artist-name-verification.json`、`artist-names-live-browser.json`、`artist-regression-python-final.log`。正式側欄截圖：`.tag_gallery/ui-review/artist-names-live-desktop.png`。
