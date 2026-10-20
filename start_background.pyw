# -*- coding: utf-8 -*-
"""
Twitter & Pixiv AI 智慧標籤圖庫 — 無視窗背景啟動器 (Background Launcher)

功能：
1. 在背景完全隱藏執行（不跳出黑色 CMD 視窗）。
2. 自動檢查 http://127.0.0.1:8765 是否已在執行，避免重複啟動。
3. 若尚未啟動，於背景啟動 FastAPI / Uvicorn 伺服器，並將日誌寫入 D:\\圖檔\\twitter\\.tag_gallery\\server.log。
4. 等待伺服器就緒後，自動在預設瀏覽器開啟 http://127.0.0.1:8765。
"""

import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

PROJECT_DIR = Path(r"H:\code\twitter-tag-gallery")
LOG_DIR = Path(r"D:\圖檔\twitter\.tag_gallery")
LOG_FILE = LOG_DIR / "server.log"
PID_FILE = LOG_DIR / "server.pid"
PYTHONW_EXE = Path(r"C:\Users\Gary\AppData\Local\Programs\Python\Python310\pythonw.exe")
PYTHON_EXE = Path(r"C:\Users\Gary\AppData\Local\Programs\Python\Python310\python.exe")
GALLERY_URL = "http://127.0.0.1:8765"


def is_server_running(timeout: float = 1.0) -> bool:
    try:
        req = urllib.request.Request(f"{GALLERY_URL}/api/stats", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_server_in_background() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    exe = str(PYTHON_EXE if PYTHON_EXE.exists() else sys.executable)

    CREATE_NO_WINDOW = 0x08000000

    log_fp = open(str(LOG_FILE), "a", encoding="utf-8", errors="ignore")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    proc = subprocess.Popen(
        [exe, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8765"],
        cwd=str(PROJECT_DIR),
        stdout=log_fp,
        stderr=log_fp,
        stdin=subprocess.DEVNULL,
        env=env,
        creationflags=CREATE_NO_WINDOW,
        close_fds=False,
    )
    try:
        PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    except Exception:
        pass


def main() -> None:
    open_browser = "--no-browser" not in sys.argv

    if not is_server_running():
        start_server_in_background()
        # 等待伺服器啟動就緒 (最多等待 20 秒)
        for _ in range(50):
            if is_server_running(timeout=0.8):
                break
            time.sleep(0.4)

    if open_browser:
        webbrowser.open(GALLERY_URL)


if __name__ == "__main__":
    main()
