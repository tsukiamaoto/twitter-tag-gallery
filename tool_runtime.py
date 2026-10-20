"""Validated tool execution with shared write locks and bounded cross-process history."""
from contextlib import nullcontext
import json
import os
import re
import sqlite3
import threading
import time
import uuid

from background_work import FileLock, JobCancelled, acquire_job, data_dir
from tool_registry import get_tool

_read_slots = threading.BoundedSemaphore(2)
RESULT_BYTES = 128 * 1024
HISTORY_LIMIT = 200


class ToolBusy(RuntimeError):
    pass


def failed_result(result):
    return isinstance(result, dict) and (result.get("status") in ("cancelled", "failed", "error", "busy", "already_running")
                                        or result.get("ok") is False or bool(result.get("error")))


def _db():
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(directory / "tool_runs.sqlite3", timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS tool_runs(id TEXT PRIMARY KEY,name TEXT,source TEXT,pid INTEGER,"
                 "started_at REAL,finished_at REAL,status TEXT,duration_ms REAL,job_id TEXT,error TEXT,"
                 "result_json TEXT,truncated INTEGER DEFAULT 0)")
    return conn


def _lock(run_id):
    return FileLock(data_dir() / "locks" / f"tool-run-{run_id}.lock")


def _remove_lock(lock):
    try:
        lock.path.unlink(missing_ok=True)
    except OSError:
        pass  # A released OS lock is sufficient; antivirus may still hold the file.


def _validated(name, arguments):
    from integration_settings import require_tool
    tool = get_tool(name)
    require_tool(name)
    if len(json.dumps(arguments, ensure_ascii=False).encode("utf-8")) > 1024 * 1024:
        raise ValueError("工具參數超過 1 MiB")
    arguments = tool.validate(arguments)
    return tool, arguments


def _prepare(name, arguments, source):
    tool, arguments = _validated(name, arguments)
    slot = False
    lease = None
    if tool.writes and tool.exclusive:
        lease = acquire_job(f"MCP：{tool.label}")
    elif not tool.writes:
        slot = _read_slots.acquire(blocking=False)
        if not slot:
            raise ToolBusy("已有兩個唯讀工具執行中，請稍後重試")
    run_id = uuid.uuid4().hex
    lock = _lock(run_id)
    try:
        if not lock.acquire():
            raise ToolBusy("無法取得工具執行鎖")
        conn = _db()
        try:
            with conn:
                # Clean orphaned running rows even if nobody opened the history UI.
                for row in conn.execute("SELECT id FROM tool_runs WHERE status='running'").fetchall():
                    previous = _lock(row["id"])
                    if previous.acquire():
                        previous.release()
                        _remove_lock(previous)
                        conn.execute("UPDATE tool_runs SET status='interrupted',finished_at=?,error=? WHERE id=? AND status='running'",
                                     (time.time(), "工具執行程序已中止；請先檢查結果再決定是否重試", row["id"]))
                conn.execute("INSERT INTO tool_runs(id,name,source,pid,started_at,status,job_id) VALUES(?,?,?,?,?,'running',?)",
                             (run_id, name, source, os.getpid(), time.time(), lease.info["id"] if lease else None))
                conn.execute("DELETE FROM tool_runs WHERE status!='running' AND id NOT IN "
                             "(SELECT id FROM tool_runs ORDER BY started_at DESC LIMIT ?)", (HISTORY_LIMIT,))
        finally:
            conn.close()
    except BaseException as error:
        lock.release()
        _remove_lock(lock)
        if lease:
            lease.info.update(status="failed", last_error=str(error)[:1000])
            lease.release()
        if slot:
            _read_slots.release()
        raise
    return tool, arguments, run_id, lock, lease, slot


def _finish(run_id, started, status, result=None, error=""):
    serialized = json.dumps(result, ensure_ascii=False, default=str)
    truncated = len(serialized.encode("utf-8")) > RESULT_BYTES
    if truncated:
        # Keep a valid JSON envelope; cutting a JSON string would corrupt history.
        preview = serialized.encode("utf-8")[:RESULT_BYTES // 2 - 128].decode("utf-8", errors="ignore")
        serialized = json.dumps({"truncated": True, "preview": preview}, ensure_ascii=False)
    conn = _db()
    try:
        with conn:
            conn.execute("UPDATE tool_runs SET finished_at=?,status=?,duration_ms=?,error=?,result_json=?,truncated=? WHERE id=?",
                         (time.time(), status, round((time.monotonic()-started)*1000, 1), error[:1000], serialized, int(truncated), run_id))
    finally:
        conn.close()


def _execute(prepared):
    tool, arguments, run_id, lock, lease, slot = prepared
    started = time.monotonic()
    try:
        with lease.activate() if lease else nullcontext():
            # A policy may have changed since a Settings request reserved the worker.
            from integration_settings import require_tool, record_session
            require_tool(tool.name)
            record_session("tools/call", tool.name)
            result = tool.handler()(**arguments)
            status = result.get("status") if isinstance(result, dict) else None
            if failed_result(result):
                message = str(result.get("message") or result.get("error") or "工具未完成")
                if lease:
                    lease.info.update(status="cancelled" if status == "cancelled" else "failed", last_error=message)
                final_status = "cancelled" if status == "cancelled" else "failed"
            else:
                final_status, message = "succeeded", ""
        # Publish completion only after the job lease has released the global lock.
        _finish(run_id, started, final_status, result, message)
        return result
    except JobCancelled:
        _finish(run_id, started, "cancelled", error="已依使用者要求停止")
        raise ValueError("工具已依使用者要求停止") from None
    except Exception as error:
        _finish(run_id, started, "failed", error=str(error))
        raise
    finally:
        if slot:
            _read_slots.release()
        lock.release()
        _remove_lock(lock)


def invoke(name, arguments, source="mcp"):
    tool = get_tool(name)
    if not tool.audit:
        tool, arguments = _validated(name, arguments)
        if not _read_slots.acquire(blocking=False):
            raise ToolBusy("已有兩個唯讀工具執行中，請稍後重試")
        try:
            return tool.handler()(**arguments)
        finally:
            _read_slots.release()
    return _execute(_prepare(name, arguments, source))


def dispatch(name, arguments):
    """Long MCP operations return promptly; clients can query run_id and job_id."""
    if get_tool(name).background:
        return start(name, arguments, source="mcp")
    return invoke(name, arguments, source="mcp")


def start(name, arguments, source="settings"):
    prepared = _prepare(name, arguments, source)
    run_id, lock, lease, slot = prepared[2:]
    def work():
        try:
            _execute(prepared)
        except Exception:
            pass  # The persisted result is returned by get_run, never lost to a thread traceback.
    thread = threading.Thread(target=work, name=f"tool-{name}", daemon=True)
    try:
        thread.start()
    except BaseException as error:
        _finish(run_id, time.monotonic(), "failed", error=str(error))
        lock.release()
        _remove_lock(lock)
        if lease:
            lease.info.update(status="failed", last_error=str(error)[:1000])
            lease.release()
        if slot:
            _read_slots.release()
        raise
    return {"id": run_id, "run_id": run_id, "status": "running", "job_id": lease.info["id"] if lease else None,
            "message": "已啟動；可用 gallery_get_tool_run 查詢 run_id 的結果"}


def _public(row, include_result=False):
    value = dict(row)
    serialized = value.pop("result_json", None)
    lock = _lock(value["id"])
    if value["status"] == "running" or lock.path.exists():
        if lock.acquire():
            lock.release()
            _remove_lock(lock)
            if value["status"] == "running":
                conn = _db()
                try:
                    with conn:
                        conn.execute("UPDATE tool_runs SET status='interrupted',error=?,finished_at=? WHERE id=? AND status='running'",
                                     ("工具執行程序已中止；請先檢查結果再決定是否重試", time.time(), value["id"]))
                    # The worker may have finished between our original read and lock check.
                    value = dict(conn.execute("SELECT * FROM tool_runs WHERE id=?", (value["id"],)).fetchone())
                    serialized = value.pop("result_json", None)
                finally:
                    conn.close()
        else:
            # Results are ready only after final resource cleanup, including reader slots.
            value["status"] = "running"
    if include_result:
        value["result"] = json.loads(serialized) if serialized else None
    return value


def history(limit=30):
    if not (data_dir() / "tool_runs.sqlite3").exists():
        return []
    conn = _db()
    try:
        return [_public(row) for row in conn.execute("SELECT * FROM tool_runs ORDER BY started_at DESC LIMIT ?", (min(50, max(1, limit)),)).fetchall()]
    finally:
        conn.close()


def get_run(run_id):
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise KeyError("找不到工具執行紀錄")
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM tool_runs WHERE id=?", (run_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError("找不到工具執行紀錄")
    return _public(row, include_result=True)
