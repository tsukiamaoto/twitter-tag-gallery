# 007 — 來源資料夾直接使用 Windows 選取

狀態：done  
開始日期：2026-10-20  
完成日期：2026-10-20

## 需求

使用者希望點擊來源資料夾的選擇入口時，直接開啟 Windows 原生資料夾選擇視窗，減少網頁內逐層瀏覽的操作。

使用者後續確認：採用現代 Windows 檔案總管樣式的系統視窗，取代舊式樹狀選擇器。使用 Common Item Dialog 的 `FOS_PICKFOLDERS` 模式。

## 驗收

- [x] 「瀏覽資料夾」直接呼叫 Windows 原生選取，不先開網頁資料夾瀏覽器。
- [x] 從最後一個來源資料夾開始瀏覽，可重複選取加入多個來源。
- [x] 取消保留既有來源；重複選取不增加重複路徑。
- [x] 等待視窗時有明確回饋，避免重複開啟。
- [x] 選取錯誤有可理解的提示，仍可手動貼上路徑。
- [x] 完成回歸測試，記錄實際 Windows 視窗驗證結果。

## 驗證結果

- 使用者實際看到新視窗後確認：「有看到，已經是我想要的樣式」。系統視窗的外觀驗收依據為使用者確認；驗證工具未擷取到系統視窗本身。
- 實際從正式 WebUI 點「瀏覽資料夾」，取消後回報「已取消選擇」，按鈕恢復可用，既有來源清單不變；網頁自製資料夾瀏覽器沒有開啟。
- JavaScript 回歸測試 13 項通過，包含來源起始位置、取消、重入鎖定、中文路徑、Windows 路徑去重及失敗重試。
- Python 原生選取器測試 5 項通過，包含 UTF-8 JSON 回傳、取消與啟動錯誤區分、逾時與鎖釋放。現代選取器的 C# 介面亦通過編譯。
- 已確認服務閒置後重新載入後端，並確認網站恢復連線、新版選取流程生效。
- 驗收沒有送出排程儲存或立即執行請求；選擇資料夾只更新表單，仍需使用者儲存才生效。

![來源資料夾操作區](../screenshots/windows-folder-picker.png)

## 實作

- 主按鈕直接使用系統選取，手動貼上路徑留在收合區。
- `scheduler.py` 委派至 `native_folder_picker.py`，後者啟動 STA PowerShell 執行 `native_folder_picker.ps1` 中的現代 Common Item Dialog。未安裝額外套件。
- 使用 [IFileDialog](https://learn.microsoft.com/en-us/windows/win32/api/shobjidl_core/nn-shobjidl_core-ifiledialog) 與 [FOS_PICKFOLDERS](https://learn.microsoft.com/zh-tw/windows/win32/api/shobjidl_core/ne-shobjidl_core-_fileopendialogoptions)，不使用舊式 FolderBrowserDialog。
- 路徑以環境變數傳遞；JSON 使用 UTF-8。啟動錯誤會回傳 503，避免誤報成使用者取消。
