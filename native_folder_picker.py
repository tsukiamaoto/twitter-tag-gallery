"""Show the modern Windows Explorer folder picker without a console window."""

import base64
import json
import os
from pathlib import Path
import subprocess
import threading


_picker_lock = threading.Lock()
PICKER_SCRIPT = Path(__file__).with_suffix(".ps1")


def pick_folder(initial_dir: str = "") -> str:
    if os.name != "nt":
        raise RuntimeError("Windows 資料夾選取只能在 Windows 桌面環境使用。")
    if not _picker_lock.acquire(blocking=False):
        raise RuntimeError("資料夾選擇視窗已開啟，請先完成目前的選擇。")
    try:
        env = os.environ.copy()
        env.update({
            "TAG_GALLERY_PICKER_INITIAL": initial_dir or "",
            "TAG_GALLERY_PICKER_TITLE": "選擇來源資料夾",
            "TAG_GALLERY_PICKER_OK": "選擇資料夾",
        })
        # Paths travel through the environment, never through PowerShell source.
        windows_dir = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        powershell = windows_dir / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        try:
            script = PICKER_SCRIPT.read_text(encoding="ascii")
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-NonInteractive", "-STA", "-EncodedCommand",
                 base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=300,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("資料夾選取已逾時，請重新開啟選擇視窗。") from error
        except OSError as error:
            raise RuntimeError("無法開啟 Windows 資料夾選擇視窗。") from error
        if result.returncode != 0:
            raise RuntimeError("Windows 資料夾視窗啟動失敗，請確認圖庫在桌面工作階段執行。")
        try:
            selected = json.loads(result.stdout.lstrip("\ufeff"))["selected_path"]
            if not isinstance(selected, str):
                raise ValueError("Invalid folder selection")
        except (ValueError, KeyError, TypeError) as error:
            raise RuntimeError("無法讀取 Windows 資料夾選取結果，請重試。") from error
        return selected
    finally:
        _picker_lock.release()


if __name__ == "__main__":
    import sys

    print(pick_folder(sys.argv[1] if len(sys.argv) > 1 else ""))
