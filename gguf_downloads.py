"""Pinned, resumable model downloads. No gallery files are read or written."""
import argparse
import concurrent.futures as futures
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import quote
import zipfile

import requests
from background_work import FileLock, atomic_json

CATALOG = json.loads(Path(__file__).with_name("gguf_catalog.json").read_text(encoding="utf-8"))
CHUNK = 16 * 1024 * 1024


def model_root():
    return Path(os.environ.get("TWITTER_GGUF_ROOT", Path(__file__).parent / "models" / "gguf")).resolve()


def inside(root, relative):
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError("模型路徑超出 GGUF 資料夾")
    return result


def sha256(path):
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


class DownloadCancelled(Exception):
    pass


def download(profiles, workers=16):
    root = model_root()
    root.mkdir(parents=True, exist_ok=True)
    lock = FileLock(root / ".downloads.lock")
    if not lock.acquire():
        raise ValueError("已有模型下載正在執行")
    stop = root / ".download.cancel"
    stop.unlink(missing_ok=True)
    state = {"phase": "downloading", "profiles": profiles, "pid": os.getpid(), "completed": [],
             "downloaded_bytes": 0, "total_bytes": 0, "error": "", "started_at": time.time()}
    jobs, plans = [], []
    terminal_error = threading.Event()

    def check_cancel():
        if stop.exists() or terminal_error.is_set():
            raise DownloadCancelled()

    def publish():
        state["updated_at"] = time.time()
        atomic_json(root / "download_status.json", state, durable=True)

    def transfer(url, path, first, last, total, whole=False):
        for attempt in range(6):
            check_cancel()
            existing = path.stat().st_size if path.exists() else 0
            expected = last - first + 1
            if existing == expected:
                return
            if existing > expected:
                raise ValueError("下載暫存檔大小錯誤")
            try:
                if whole:
                    # Small runtime archives use a plain request; ranged weights use exact ranges.
                    offset = existing
                    headers = {"Range": f"bytes={offset}-{last}"} if offset else {}
                    request_url = url
                else:
                    offset = first + existing
                    headers = {"Range": f"bytes={offset}-{last}", "Accept-Encoding": "identity"}
                    request_url = url + f"?download=true&part={first}&offset={existing}"
                with requests.get(request_url, headers=headers, stream=True, timeout=(30, 30)) as response:
                    if response.status_code == 429:
                        time.sleep(min(30, int(response.headers.get("Retry-After", "10"))))
                        continue
                    response.raise_for_status()
                    if not (whole and not existing and response.status_code == 200):
                        if response.status_code != 206 or response.headers.get("Content-Range") != f"bytes {offset}-{last}/{total}":
                            raise ValueError("下載伺服器未正確回應檔案範圍")
                    with path.open("ab") as output:
                        for block in response.iter_content(1024 * 1024):
                            check_cancel()
                            output.write(block)
                        output.flush()
                        os.fsync(output.fileno())
                if path.stat().st_size != expected:
                    raise IOError("檔案尚未下載完整")
                return
            except DownloadCancelled:
                raise
            except (requests.RequestException, IOError, ValueError):
                if attempt == 5:
                    terminal_error.set()
                    raise
                time.sleep(min(10, 2 ** attempt))

    try:
        assets = [CATALOG["runtime"]]
        for name in profiles:
            assets.extend(CATALOG["profiles"][name]["assets"].values())
        state["total_bytes"] = sum(a["bytes"] for a in assets)
        publish()
        with futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for asset in assets:
                destination = inside(root, asset["path"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists() and destination.stat().st_size == asset["bytes"] and sha256(destination) == asset["sha256"]:
                    state["completed"].append(asset["path"])
                    continue
                if "url" in asset:
                    url = asset["url"]
                else:
                    url = f"https://huggingface.co/{asset['repo']}/resolve/{asset['revision']}/{quote(asset['file'], safe='/')}"
                part_dir = root / ".downloads" / asset["sha256"]
                part_dir.mkdir(parents=True, exist_ok=True)
                parts = []
                chunk_size = asset["bytes"] if "url" in asset else CHUNK
                for first in range(0, asset["bytes"], chunk_size):
                    last = min(asset["bytes"], first + chunk_size) - 1
                    part = part_dir / f"{first:012d}.part"
                    parts.append(part)
                    jobs.append(pool.submit(transfer, url, part, first, last, asset["bytes"], "url" in asset))
                plans.append((asset, destination, parts))
            pending = set(jobs)
            while pending:
                done, pending = futures.wait(pending, timeout=2, return_when=futures.FIRST_EXCEPTION)
                for job in done:
                    job.result()
                state["downloaded_bytes"] = sum(a["bytes"] for a in assets if a["path"] in state["completed"]) + sum(p.stat().st_size for _, _, parts in plans for p in parts if p.exists())
                publish()
            state["phase"] = "verifying"
            publish()
        for asset, destination, parts in plans:
            check_cancel()
            temporary = destination.with_name(destination.name + ".partial")
            result = hashlib.sha256()
            with temporary.open("wb") as output:
                for part in parts:
                    with part.open("rb") as source:
                        for block in iter(lambda: source.read(8*1024*1024), b""):
                            check_cancel()
                            output.write(block)
                            result.update(block)
                output.flush()
                os.fsync(output.fileno())
            if temporary.stat().st_size != asset["bytes"] or result.hexdigest() != asset["sha256"]:
                for part in parts:
                    part.unlink(missing_ok=True)
                temporary.unlink(missing_ok=True)
                raise ValueError(f"{destination.name} 校驗失敗，已清除失效分段；請續傳重試")
            temporary.replace(destination)
            for part in parts:
                part.unlink()
            state["completed"].append(asset["path"])
            publish()
        archive = root / CATALOG["runtime"]["path"]
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                check_cancel()
                target = inside(root / "runtime" / "bin", member.filename)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(target.name + ".extract.partial")
                    digest = hashlib.sha256()
                    with package.open(member) as source, temporary.open("wb") as output:
                        for block in iter(lambda: source.read(1024*1024), b""):
                            check_cancel()
                            output.write(block)
                            digest.update(block)
                        output.flush()
                        os.fsync(output.fileno())
                    # A running Windows executable cannot be replaced; verified identical files can be reused.
                    if target.is_file() and sha256(target) == digest.hexdigest():
                        temporary.unlink()
                    else:
                        temporary.replace(target)
        state.update(phase="completed", downloaded_bytes=state["total_bytes"])
    except (DownloadCancelled, KeyboardInterrupt):
        state["phase"] = "cancelled"
    except Exception as error:
        state.update(phase="failed", error=str(error)[:300])
        raise
    finally:
        try:
            publish()
        finally:
            lock.release()
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("profiles", nargs="+", choices=list(CATALOG["profiles"]))
    parser.add_argument("--workers", type=int, choices=range(1, 33), default=16)
    options = parser.parse_args()
    print(json.dumps(download(options.profiles, options.workers), ensure_ascii=False), flush=True)
