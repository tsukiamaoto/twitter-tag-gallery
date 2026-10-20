"""Consistent SQLite backups, kept separately from disposable media caches."""
import os
from pathlib import Path
import sqlite3
import time
import uuid
from background_work import exclusive_job


def snapshot(source,target):
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_suffix(".tmp")
    destination=sqlite3.connect(temporary)
    try:
        source.backup(destination,pages=256,sleep=0.01)
        if destination.execute("PRAGMA quick_check").fetchone()[0]!="ok":
            raise RuntimeError("備份完整性驗證失敗")
    finally:
        destination.close()
    os.replace(temporary,target)


@exclusive_job("資料庫備份")
def backup():
    import database
    directory=Path(database.DATA_DIR)/"backups"
    directory.mkdir(parents=True,exist_ok=True)
    target=directory/f"gallery_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.db"
    temporary=target.with_suffix(".tmp")
    source=database.get_db()
    try:
        snapshot(source,target)
    finally:
        source.close()
    for stale in sorted(directory.glob("gallery_*.db"),reverse=True)[10:]:
        if stale.resolve().parent==directory.resolve():
            stale.unlink()
    return {"ok":True,"filename":target.name,"size_bytes":target.stat().st_size,"message":"資料庫已備份並通過完整性檢查"}
