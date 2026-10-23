"""Delete image files to the Windows Recycle Bin and remove database records."""
import os
import stat
from pathlib import Path

import database
from background_work import exclusive_job
from file_operations import recover
from file_safety import gallery_path, UnsafePath
from query_cache import remove_deleted_images_from_cache


def recycle_files(paths):
    targets = [Path(p).resolve() for p in paths]
    if not targets:
        return
    if os.name != "nt":
        for path in targets:
            path.unlink()
        return
    joined = "\0".join(str(path) for path in targets)
    flags = 0x0040 | 0x0010 | 0x0400 | 0x0004  # FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
    try:
        from win32com.shell import shell, shellcon
        code, aborted = shell.SHFileOperation((0, shellcon.FO_DELETE, joined, None, flags, None, None))
    except ImportError:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", wintypes.WORD),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", wintypes.LPVOID),
                ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        buf = ctypes.create_unicode_buffer(joined + "\0\0")
        op = SHFILEOPSTRUCTW(
            hwnd=None,
            wFunc=3,  # FO_DELETE
            pFrom=ctypes.cast(buf, wintypes.LPCWSTR),
            pTo=None,
            fFlags=flags,
            fAnyOperationsAborted=False,
            hNameMappings=None,
            lpszProgressTitle=None,
        )
        code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        aborted = bool(op.fAnyOperationsAborted)
    if code != 0 or aborted:
        raise OSError(code or 1, f"無法將檔案移至資源回收筒 (錯誤碼: {code})")


@exclusive_job("刪除圖片檔案")
def delete_images(image_ids):
    ids = list(dict.fromkeys(image_ids))
    if not ids or any(type(iid) is not int or iid <= 0 for iid in ids):
        raise ValueError("請選取有效的圖片 ID")
    if not database.GALLERY_ROOT.is_dir():
        raise OSError("圖庫根目錄無法存取，保留圖片紀錄")
    if (database.DATA_DIR / "file_journals" / "PENDING").exists():
        if recover(database.GALLERY_ROOT, database.DATA_DIR, database.get_db)["errors"]:
            raise RuntimeError("前次檔案操作尚未復原，已停止刪除")
    root_resolved = database.GALLERY_ROOT.resolve()
    files_to_recycle = []
    found_ids, missing_ids = set(), []
    conn = database.get_db()
    try:
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT id, rel_path, filename FROM images WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                found_ids.add(row["id"])
                source = gallery_path(database.GALLERY_ROOT, row["rel_path"])
                if ".tag_gallery" in {part.casefold() for part in source.relative_to(root_resolved).parts}:
                    raise UnsafePath("不可刪除圖庫系統檔案")
                try:
                    info = source.stat()
                except FileNotFoundError:
                    # Explicit deletion also removes stale records for files
                    # already deleted outside the gallery. Other I/O errors
                    # must keep the index so an unavailable file is not lost.
                    missing_ids.append(row["id"])
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise UnsafePath(f"圖片路徑不是一般檔案：{row['filename']}")
                files_to_recycle.append(source)
        if len(found_ids) != len(ids):
            if len(ids) == 1 and not found_ids:
                # A still-open viewer can refer to a record removed by another
                # request/tab. Treat repeating this single deletion as done.
                remove_deleted_images_from_cache(ids)
                return {
                    "deleted": 0,
                    "deleted_ids": ids,
                    "recycled": 0,
                    "missing_count": 0,
                    "already_deleted": True,
                }
            raise ValueError("包含不存在的圖片 ID；尚未刪除任何檔案")

        def _apply_delete(c):
            c.executemany("DELETE FROM images WHERE id=?", [(iid,) for iid in ids])
            database.refresh_tag_counts(c)
            if files_to_recycle:
                recycle_files(files_to_recycle)

        database.run_db_write_with_retry(conn, _apply_delete)
    finally:
        conn.close()

    database.log_operation(
        category="files",
        action="刪除圖片至系統資源回收筒",
        summary=f"已移除 {len(ids)} 筆圖片紀錄，{len(files_to_recycle)} 個檔案移至系統資源回收筒、{len(missing_ids)} 個檔案原已不存在",
        affected_count=len(ids),
        details={"image_ids": ids, "recycled_count": len(files_to_recycle), "missing_ids": missing_ids},
    )
    remove_deleted_images_from_cache(ids)
    return {
        "deleted": len(ids),
        "deleted_ids": ids,
        "recycled": len(files_to_recycle),
        "missing_count": len(missing_ids),
    }
