"""Local GGUF catalog and real, isolated Vulkan model-server lifecycle."""
import atexit
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import threading
import time

import requests
from background_work import FileLock, atomic_json
from gguf_downloads import CATALOG, inside, model_root


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def gguf_metadata(path):
    """Read bounded header metadata, never tensor data or executable model code."""
    with path.open("rb") as source:
        def take(length):
            if length < 0 or length > 1024 * 1024:
                raise ValueError("GGUF 中繼資料過大")
            data = source.read(length)
            if len(data) != length:
                raise ValueError("GGUF 檔案不完整")
            return data

        def number(fmt):
            return struct.unpack("<" + fmt, take(struct.calcsize(fmt)))[0]

        def string():
            return take(number("Q")).decode("utf-8", errors="replace")

        formats = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}

        def value(kind):
            if kind in formats:
                return number(formats[kind])
            if kind == 8:
                return string()
            if kind == 9:
                element, count = number("I"), number("Q")
                if count > 1000000 or element == 9:
                    raise ValueError("GGUF 陣列格式不支援")
                if element in formats:
                    length = count * struct.calcsize(formats[element])
                    if source.tell() + length > path.stat().st_size:
                        raise ValueError("GGUF 檔案不完整")
                    source.seek(length, 1)
                else:
                    for _ in range(count):
                        value(element)
                return None
            raise ValueError("未知的 GGUF 中繼資料類型")

        if take(4) != b"GGUF" or number("I") not in (2, 3):
            raise ValueError("檔案不是支援的 GGUF v2/v3")
        number("Q")
        count = number("Q")
        if count > 10000:
            raise ValueError("GGUF 中繼資料項目過多")
        metadata = {}
        for _ in range(count):
            key = string()
            item = value(number("I"))
            if key in ("general.architecture", "general.name", "general.file_type"):
                metadata[key] = item
        return metadata


def ensure_directories():
    root = model_root()
    for name in ("diffusion_models", "text_encoders", "vae", "runtime", "logs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def profile_for(relative):
    filename = Path(relative).name.lower()
    if "klein" in filename and "4b" in filename:
        return "klein4b"
    if "qwen" in filename and ("2.1" in filename or "image21" in filename):
        return "qwen21"
    return None


def scan():
    root = ensure_directories()
    result = []
    # Root-level GGUFs are accepted for convenience; companions are never listed as image models.
    paths = list(root.glob("*.gguf")) + list((root / "diffusion_models").rglob("*.gguf"))
    for path in sorted(paths)[:200]:
        try:
            relative = path.relative_to(root).as_posix()
            inside(root, relative)
            metadata = gguf_metadata(path)
            quant = re.search(r"(Q[2-8]_[A-Z0-9_]+|BF16|F16)(?=\.gguf$)", path.name, re.I)
            result.append({"id": relative, "filename": path.name, "bytes": path.stat().st_size,
                           "profile": profile_for(relative), "quantization": quant.group(1).upper() if quant else "未知",
                           "architecture": metadata.get("general.architecture", "未知"), "valid": True})
        except (OSError, ValueError, struct.error) as error:
            result.append({"id": path.relative_to(root).as_posix(), "filename": path.name, "valid": False,
                           "error": str(error), "profile": None})
    return result


def runtime_executable():
    root = model_root() / "runtime" / "bin"
    filename = "sd-server.exe" if os.name == "nt" else "sd-server"
    return next((p for p in root.rglob(filename) if p.resolve().is_relative_to(root.resolve())), None)


def download_status():
    root = model_root()
    state = read_json(root / "download_status.json")
    if state.get("phase") in ("downloading", "verifying", "cancelling"):
        probe = FileLock(root / ".downloads.lock")
        if probe.acquire():
            probe.release()
            state = {**state, "phase": "interrupted"}
    return state


def backend_capabilities(port):
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(f"http://127.0.0.1:{port}/sdcpp/v1/capabilities", timeout=1)
        response.raise_for_status()
        return response.json()


class GGUFManager:
    def __init__(self):
        self.lock = threading.RLock()
        self.process = None
        self.lease = None
        self.epoch = 0
        self.state = {"phase": "unloaded", "loaded_model": None, "error": "", "device": "", "port": None}

    def settings(self):
        return read_json(model_root() / "selection.json")

    def selection(self, model_id, profile=None):
        root = ensure_directories()
        path = inside(root, model_id)
        if path.suffix.lower() != ".gguf" or not path.is_file():
            raise ValueError("找不到 GGUF 模型；請先下載或放入模型資料夾")
        if path.parent != root and not path.is_relative_to(root / "diffusion_models"):
            raise ValueError("請從 diffusion_models 選取影像模型")
        gguf_metadata(path)
        profile = profile or profile_for(model_id)
        if profile not in CATALOG["profiles"]:
            raise ValueError("請指定相容的模型系列（FLUX.2 klein 4B 或 Qwen-Image-2.1）")
        selection = {"model_id": path.relative_to(root).as_posix(), "profile": profile}
        atomic_json(root / "selection.json", selection)
        return selection

    def requirements(self, selection):
        profile = CATALOG["profiles"].get(selection.get("profile"))
        if not profile:
            return ["尚未選擇模型"]
        missing = []
        if not runtime_executable():
            missing.append("Vulkan 執行環境")
        for kind, asset in profile["assets"].items():
            if kind == "model":
                path = inside(model_root(), selection["model_id"])
                if not path.is_file():
                    missing.append(path.name)
            else:
                path = inside(model_root(), asset["path"])
                if not path.is_file() or path.stat().st_size != asset["bytes"]:
                    missing.append(path.name)
        return missing

    def status(self):
        with self.lock:
            if self.process is not None and self.process.poll() is not None and self.state["phase"] in ("loading", "loaded"):
                self.epoch += 1
                self.state.update(phase="failed", error="模型程序已結束，請檢查載入紀錄", loaded_model=None, port=None)
                if self.lease:
                    self.lease.release()
                    self.lease = None
            selection = self.settings()
            return {"directory": str(model_root()), "models": scan(), "selection": selection,
                    "missing": self.requirements(selection), "runtime_installed": bool(runtime_executable()),
                    "runtime": dict(self.state), "download": download_status(),
                    "profiles": [{"id": key, "label": p["label"], "source": p["source"],
                                  "model_path": p["assets"]["model"]["path"],
                                  "download_bytes": sum(a["bytes"] for a in p["assets"].values())}
                                 for key, p in CATALOG["profiles"].items()]}

    def command(self, selection, port):
        root = model_root()
        profile = CATALOG["profiles"][selection["profile"]]
        assets = profile["assets"]
        command = [str(runtime_executable()), "--diffusion-model", str(inside(root, selection["model_id"])),
                   "--llm", str(inside(root, assets["text_encoder"]["path"])),
                   "--vae", str(inside(root, assets["vae"]["path"])),
                   "--listen-ip", "127.0.0.1", "--listen-port", str(port), "--backend", "vulkan0",
                   "--offload-to-cpu", "--mmap", "--vae-tiling", "--flash-attn", "off,diffusion=on", "--rng", "cpu",
                   "--steps", str(profile["steps"]), "--cfg-scale", "1.0"]
        if "vision_encoder" in assets:
            command += ["--llm_vision", str(inside(root, assets["vision_encoder"]["path"]))]
        return command

    def load(self, model_id, profile=None):
        with self.lock:
            if self.state["phase"] == "loading":
                raise RuntimeError("模型正在載入，請稍候或先卸載")
            selection = self.selection(model_id, profile)
            missing = self.requirements(selection)
            if missing:
                raise ValueError("缺少必要檔案：" + "、".join(missing))
            self.unload()
            lease = FileLock(model_root() / ".runtime.lock")
            if not lease.acquire():
                raise RuntimeError("另一個圖庫程序正在使用 GGUF 模型")
            self.lease = lease
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            log_path = model_root() / "logs" / "model-server.log"
            try:
                with log_path.open("wb") as log:
                    self.process = subprocess.Popen(self.command(selection, port), cwd=runtime_executable().parent,
                        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except Exception:
                lease.release()
                self.lease = None
                raise
            self.epoch += 1
            epoch = self.epoch
            self.state.update(phase="loading", loaded_model=None, error="", port=port, device="Vulkan", log=str(log_path))
            threading.Thread(target=self._await_ready, args=(epoch, selection, port, log_path), daemon=True).start()
            return {"message": "正在載入選取的 GGUF 模型", "runtime": dict(self.state)}

    def _await_ready(self, epoch, selection, port, log_path):
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            with self.lock:
                if epoch != self.epoch:
                    return
                if self.process.poll() is not None:
                    text = log_path.read_text(encoding="utf-8", errors="replace")[-1600:]
                    self.state.update(phase="failed", loaded_model=None, port=None, error=text or "模型載入失敗")
                    self.lease.release()
                    self.lease = None
                    return
            try:
                capabilities = backend_capabilities(port)
                if isinstance(capabilities, dict) and isinstance(capabilities.get("model"), dict):
                    with self.lock:
                        if epoch == self.epoch:
                            self.state.update(phase="loaded", loaded_model=selection["model_id"], error="")
                    return
            except (requests.RequestException, ValueError):
                pass
            time.sleep(0.5)
        with self.lock:
            if epoch == self.epoch:
                self.unload()
                self.state.update(phase="failed", error="模型載入逾時（10 分鐘），已停止程序")

    def unload(self):
        with self.lock:
            self.epoch += 1
            if self.process is not None:
                if self.process.poll() is None:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=5)
                self.process = None
            if self.lease:
                self.lease.release()
                self.lease = None
            self.state.update(phase="unloaded", loaded_model=None, error="", device="", port=None)
            return {"message": "GGUF 模型已卸載", "runtime": dict(self.state)}


manager = GGUFManager()
atexit.register(manager.unload)


def start_download(profiles):
    if not profiles or len(profiles) > 2 or any(p not in CATALOG["profiles"] for p in profiles):
        raise ValueError("請選擇有效的模型下載方案")
    root = ensure_directories()
    if download_status().get("phase") in ("downloading", "verifying", "cancelling"):
        raise RuntimeError("模型下載正在執行")
    with (root / "logs" / "downloads.log").open("ab") as log:
        subprocess.Popen([sys.executable, "-u", str(Path(__file__).with_name("gguf_downloads.py")), *dict.fromkeys(profiles)],
                         cwd=Path(__file__).parent, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    return {"message": "已啟動模型下載，可在此查看進度或停止"}


def stop_download():
    root = ensure_directories()
    (root / ".download.cancel").touch()
    return {"message": "已要求停止下載，保留部分檔案供下次續傳"}
