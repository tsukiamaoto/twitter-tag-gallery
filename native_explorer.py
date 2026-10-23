"""Open Explorer, verify the selected file, and bring its folder into view."""
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import time


RPC_E_CHANGED_MODE = -2147417850


@contextmanager
def _shell_session():
    try:
        import pythoncom
        from win32com.client.dynamic import Dispatch
    except ImportError as error:
        raise OSError("缺少 Windows Shell 元件，請安裝 requirements.txt 中的 pywin32") from error
    initialized = False
    try:
        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
            initialized = True
        except pythoncom.com_error as error:
            if error.hresult != RPC_E_CHANGED_MODE:
                raise
        yield Dispatch("Shell.Application")
    finally:
        if initialized:
            pythoncom.CoUninitialize()


def _select_file(shell, path):
    parent = os.path.normcase(str(path.parent))
    for window in shell.Windows():
        try:
            view = window.Document
            if os.path.normcase(view.Folder.Self.Path.rstrip("\\/")) != parent.rstrip("\\/"):
                continue
            item = view.Folder.ParseName(path.name)
            if item is None:
                continue
            view.SelectItem(item, 1 | 4 | 8 | 16)  # select, deselect others, ensure visible, focus
            selected = list(view.SelectedItems())
            if len(selected) == 1 and os.path.normcase(selected[0].Path) == os.path.normcase(str(path)):
                return int(window.HWND)
        except Exception:
            # ShellWindows also includes non-folder views and windows that are closing.
            continue
    return None


def _show_window(hwnd):
    user32 = ctypes.WinDLL("user32")
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.ShowWindow.restype = ctypes.c_int
    user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    user32.SetForegroundWindow.restype = ctypes.c_int
    user32.ShowWindow(hwnd, 9)  # SW_RESTORE also restores a minimized folder.
    if not user32.SetForegroundWindow(hwnd):
        # A browser-initiated backend request may lack foreground activation rights.
        # Complete the minimize before restoring; posting both asynchronously can race.
        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)


def reveal_file(path):
    if os.name != "nt":
        raise OSError("開啟檔案位置需要 Windows 桌面工作階段")
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise FileNotFoundError(f"檔案不存在：{path}")

    try:
        with _shell_session() as shell:
            # Reuse a folder already open before requesting another window.
            hwnd = _select_file(shell, path)
            if hwnd is None:
                explorer = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe"
                # ShellExecute accepts raw parameters: only the filename is quoted.
                shell.ShellExecute(str(explorer), f'/select,"{path}"', str(path.parent), "open", 1)
                deadline = time.monotonic() + 12
                while hwnd is None and time.monotonic() < deadline:
                    time.sleep(0.1)
                    hwnd = _select_file(shell, path)
            if hwnd is None:
                raise OSError("檔案總管未能開啟圖片所在資料夾或選取圖片，請重試")
            _show_window(hwnd)
    except OSError:
        raise
    except Exception as error:
        raise OSError(f"Windows Shell 無法定位檔案：{error}") from error
