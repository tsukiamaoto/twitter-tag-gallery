@echo off
chcp 65001 >nul
echo 正在停止背景執行的 AI 智慧標籤圖庫伺服器 (Port 8765)...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8765" ^| findstr "LISTENING"') do (
    echo 終止程序 PID: %%a
    taskkill /F /PID %%a >nul 2>&1
)
echo ✅ 背景伺服器已停止。
pause
