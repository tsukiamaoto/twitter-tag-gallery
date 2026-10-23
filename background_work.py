"""Cross-process gallery job ownership and bounded media I/O.

Locks are released by the OS on process exit. Nested pipeline stages on the
owning thread share the lease; unrelated threads/processes cannot join it.
"""
import contextlib
import datetime
import functools
import json
import os
from pathlib import Path
import threading
import time
import uuid
import sqlite3
import contextvars

_registry_lock = threading.Lock()
_held_paths = set()
_local = threading.local()
_lease_context = contextvars.ContextVar("gallery_job_lease",default=None)
_last_interaction = 0.0


def data_dir():
    from database import DATA_DIR
    return Path(DATA_DIR)


class FileLock:
    def __init__(self, path):
        self.path = Path(path)
        self.key = str(self.path.resolve()).casefold()
        self.file = None

    def acquire(self):
        with _registry_lock:
            if self.key in _held_paths:
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            f = self.path.open("a+b")
            try:
                f.seek(0, 2)
                if f.tell() == 0:
                    f.write(b"\0")
                    f.flush()
                f.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                f.close()
                return False
            self.file = f
            _held_paths.add(self.key)
            return True

    def release(self):
        with _registry_lock:
            if self.file is not None:
                # Closing the descriptor also releases the OS lock.
                self.file.close()
                self.file = None
                _held_paths.discard(self.key)


def atomic_json(path, value, durable=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False)
            if durable:
                output.flush()
                os.fsync(output.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows may briefly deny replacement while a status reader
                # or antivirus has the previous file open.
                if attempt == 9:
                    raise
                time.sleep(0.01)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class BackgroundBusy(RuntimeError):
    def __init__(self):
        self.owner = background_status()
        name = self.owner.get("name", "圖庫處理")
        super().__init__(f"背景任務「{name}」正在執行，請待完成後重試")

    def response(self):
        return {"status": "already_running", "message": str(self), "background_job": self.owner}


class JobCancelled(BaseException):
    pass


def _check_cancel():
    lease=_lease_context.get()
    if lease is not None and (data_dir()/"cancel"/f"{lease.info['id']}.json").exists():
        lease.info.update(status="cancelled",stage="已停止",last_error="使用者要求停止")
        raise JobCancelled("使用者要求停止")


def request_cancel(job_id):
    active=background_status()
    if active.get("id")!=job_id:
        raise ValueError("此任務已結束")
    atomic_json(data_dir()/"cancel"/f"{job_id}.json",{"requested_at":time.time()})
    return {"status":"stopping","message":"已要求停止，正在完成目前的安全步驟"}


class JobLease:
    def __init__(self, lock, name):
        self.lock = lock
        self.info = {"id": uuid.uuid4().hex, "name": name, "pid": os.getpid(), "started_at": time.time(),
                     "updated_at": time.time(), "status": "running", "stage": "準備中", "processed_count": 0,
                     "total_target": 0, "last_error": "", "checkpoint": {}}
        self._finished = False
        self._last_publish = 0.0
        self._info_lock = threading.Lock()
        self._heartbeat_stop = threading.Event()
        try:
            now = time.time()
            self.info["updated_at"] = now
            conn = _jobs_db()
            try:
                with conn:
                    conn.execute("UPDATE jobs SET status='interrupted',updated_at=? WHERE status='running'", (now,))
                    conn.execute(
                        "INSERT INTO jobs(id,status,updated_at,payload) VALUES(?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,payload=excluded.payload",
                        (self.info["id"], self.info["status"], now, json.dumps(self.info, ensure_ascii=False)),
                    )
                    conn.execute("DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY updated_at DESC LIMIT 1000)")
            finally:
                conn.close()
            atomic_json(data_dir() / "background-job.json", self.info)
            self._last_publish = time.monotonic()
        except BaseException:
            lock.release()
            raise
        self._heartbeat = threading.Thread(target=self._pulse, name="job-heartbeat", daemon=True)
        self._heartbeat.start()

    def publish(self, force=False, **values):
        with self._info_lock:
            self.info.update(values)
            if not force and time.monotonic() - self._last_publish < 1.0:
                return
            self.info["updated_at"] = time.time()
            conn = _jobs_db()
            try:
                with conn:
                    conn.execute("INSERT INTO jobs(id,status,updated_at,payload) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,payload=excluded.payload",
                                 (self.info["id"],self.info["status"],self.info["updated_at"],json.dumps(self.info,ensure_ascii=False)))
            finally:
                conn.close()
            atomic_json(data_dir() / "background-job.json", self.info)
            self._last_publish = time.monotonic()

    def _pulse(self):
        while not self._heartbeat_stop.wait(1.0):
            try:
                self.publish(force=True)
            except OSError:
                pass

    def release(self):
        if self._finished:
            return
        self._finished = True
        self._heartbeat_stop.set()
        self._heartbeat.join(timeout=2)
        try:
            status = self.info["status"] if self.info["status"] != "running" else "succeeded"
            self.publish(force=True,status=status)
        finally:
            self.lock.release()
            (data_dir()/"cancel"/f"{self.info['id']}.json").unlink(missing_ok=True)

    @contextlib.contextmanager
    def activate(self):
        token = _lease_context.set(self)
        try:
            yield self
        except BaseException as error:
            self.info.update(status="cancelled" if isinstance(error,JobCancelled) else "failed",last_error=str(error)[:1000])
            raise
        finally:
            _lease_context.reset(token)
            self.release()


def _jobs_db():
    path = data_dir() / "jobs.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,status TEXT,updated_at REAL,payload TEXT)")
    return conn


def update_job(**values):
    _check_cancel()
    lease = _lease_context.get()
    if lease is not None:
        lease.publish(**values)


def job_history(limit=30):
    if not (data_dir() / "jobs.sqlite3").exists():
        return []
    conn = _jobs_db()
    try:
        rows = conn.execute("SELECT status,payload FROM jobs ORDER BY updated_at DESC LIMIT ?", (min(100,max(1,limit)),)).fetchall()
    finally:
        conn.close()
    active = background_status()
    jobs = []
    for status, payload in rows:
        value = json.loads(payload)
        value["status"] = status
        if status == "running" and value["id"] != active.get("id"):
            value.update(status="interrupted",last_error="程序已中止，可重試未完成項目")
        jobs.append(value)
    return jobs


def acquire_job(name):
    lock = FileLock(data_dir() / "locks" / "background-job.lock")
    if not lock.acquire():
        raise BackgroundBusy()
    return JobLease(lock, name)


def background_status():
    lock = FileLock(data_dir() / "locks" / "background-job.lock")
    if lock.acquire():
        lock.release()
        return {}
    result=_read_json(data_dir() / "background-job.json") or {"name": "圖庫處理"}
    if result.get("id"):
        result["cancel_requested"]=(data_dir()/"cancel"/f"{result['id']}.json").exists()
    return result


def exclusive_job(name):
    """Also guards direct CLI/private worker calls, not only the Web start API."""
    def decorate(func):
        @functools.wraps(func)
        def run(*args, **kwargs):
            lease = kwargs.pop("_job_lease", None)
            if _lease_context.get() is not None and lease is None:
                return func(*args, **kwargs)
            lease = lease or acquire_job(name)
            try:
                with lease.activate():
                    return func(*args, **kwargs)
            except JobCancelled:
                return {"status":"cancelled","message":"已停止處理"}
        return run
    return decorate


def start_job_thread(thread, lease):
    """The caller reserves ownership before returning 'started' to the UI."""
    try:
        thread.start()
    except BaseException:
        lease.release()
        raise


def claim_schedule_slot(slot):
    """Called while holding the job lease; both timer and Windows CLI use it."""
    path = data_dir() / "claimed-schedule-slots.json"
    slots = _read_json(path).get("slots", [])
    if slot in slots:
        return False
    atomic_json(path, {"slots": (slots + [slot])[-64:]})
    return True


def latest_schedule_slot(times, now=None):
    now = now or datetime.datetime.now()
    candidates = []
    for value in times:
        try:
            hour, minute = map(int, value.split(":"))
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (ValueError, AttributeError):
            continue
        if candidate > now:
            candidate -= datetime.timedelta(days=1)
        candidates.append(candidate)
    return max(candidates).strftime("%Y-%m-%d %H:%M") if candidates else now.strftime("%Y-%m-%d %H:%M")


def mark_interactive():
    global _last_interaction
    now = time.monotonic()
    if now - _last_interaction < 0.25:
        return
    _last_interaction = now
    marker = data_dir() / "interactive.marker"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


def interactive_recently():
    try:
        return time.time() - (data_dir() / "interactive.marker").stat().st_mtime < 2.0
    except OSError:
        return False


def background_checkpoint():
    _check_cancel()
    # Small bounded work units leave headroom without starving background jobs.
    time.sleep(0.10 if interactive_recently() else 0.01)


@contextlib.contextmanager
def media_io(background=False):
    """At most two media readers across processes; background can use only one.

    Slot zero is reserved for interactive thumbnail generation. No DB transaction
    should be held while waiting here.
    """
    if background:
        background_checkpoint()
    slots = (1,) if background else (0, 1)
    locks = [FileLock(data_dir() / "locks" / f"media-{slot}.lock") for slot in slots]
    held = None
    try:
        while held is None:
            for lock in locks:
                if lock.acquire():
                    held = lock
                    break
            if held is None:
                time.sleep(0.02)
        yield
    finally:
        if held is not None:
            held.release()
