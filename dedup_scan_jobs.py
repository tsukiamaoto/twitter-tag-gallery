"""Shared, bounded preview scans with pollable progress and no file deletion."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import uuid

import database
from background_work import exclusive_job
from deduplicator import scan_duplicates

_lock = threading.RLock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dedup-preview")
_scans = {}


def _root():
    return str(database.DATA_DIR.resolve())


def _public(scan):
    value = {key: item for key, item in scan.items() if key not in ("root", "key")}
    value["elapsed_seconds"] = scan.get("duration_seconds", round(time.monotonic() - scan["clock"], 1))
    value.pop("clock", None)
    value["progress"] = dict(scan["progress"])
    return value


def _work(scan_id):
    def progress(**values):
        with _lock:
            scan = _scans[scan_id]
            if "stage" in values and values["stage"] != scan["progress"].get("stage"):
                scan["progress"].update(processed_count=0, total_target=0)
            scan["progress"].update(values)

    @exclusive_job("掃描重複圖片")
    def run(options):
        return scan_duplicates(**options, progress=progress)

    with _lock:
        scan = _scans[scan_id]
        scan["status"] = "running"
        options = dict(scan["options"])
    try:
        result = run(options)
    except BaseException as error:
        # JobCancelled inherits BaseException; polling still needs a terminal state.
        with _lock:
            scan.update(status="failed", error=str(error) or "掃描已停止，請重試", finished_at=time.time())
    else:
        with _lock:
            scan.update(status="completed", result=result, finished_at=time.time())
    finally:
        with _lock:
            scan["duration_seconds"] = round(time.monotonic() - scan["clock"], 1)


def start_scan(**options):
    root = _root()
    key = (root, tuple(sorted(options.items())))
    with _lock:
        for scan in _scans.values():
            if scan["key"] == key and scan["status"] in ("queued", "running"):
                return _public(scan)
        # Finished results are bounded; no expiry can discard a running scan.
        completed = [iid for iid, scan in _scans.items() if scan["status"] not in ("queued", "running")]
        for iid in completed[:-7]:
            del _scans[iid]
        if sum(s["status"] in ("queued", "running") for s in _scans.values()) >= 4:
            raise ValueError("已有多個範圍等待掃描，請稍後重試")
        scan_id = uuid.uuid4().hex
        scan = {"scan_id": scan_id, "root": root, "key": key, "options": options,
                "status": "queued", "clock": time.monotonic(), "started_at": time.time(),
                "progress": {"stage": "等待掃描", "processed_count": 0, "total_target": 0}}
        _scans[scan_id] = scan
        try:
            _executor.submit(_work, scan_id)
        except BaseException:
            del _scans[scan_id]
            raise
        return _public(scan)


def get_scan(scan_id):
    with _lock:
        scan = _scans.get(scan_id)
        if scan is None or scan["root"] != _root():
            raise KeyError(scan_id)
        return _public(scan)


def latest_scan():
    with _lock:
        scans = [scan for scan in _scans.values() if scan["root"] == _root()]
        return _public(scans[-1]) if scans else None
