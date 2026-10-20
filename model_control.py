"""One lifecycle gate for shared DirectML sessions and model memory."""
import functools
import threading
import time

_gate = threading.RLock()
_state_lock = threading.Lock()
_state = {"phase":"idle","busy":False,"waiting":0,"changed_at":0.0}


def snapshot():
    with _state_lock:
        return dict(_state)


def operation(phase):
    def decorate(fn):
        @functools.wraps(fn)
        def run(*args,**kwargs):
            with _state_lock:
                _state["waiting"] += 1
            with _gate:
                with _state_lock:
                    previous = dict(_state)
                    _state.update(phase=phase,busy=True,waiting=_state["waiting"]-1,changed_at=time.time())
                try:
                    return fn(*args,**kwargs)
                finally:
                    with _state_lock:
                        _state.update(phase=previous["phase"] if previous["busy"] else "idle",busy=previous["busy"],changed_at=time.time())
        return run
    return decorate


def configure_directml(options, ort, providers):
    if "DmlExecutionProvider" in providers:
        options.enable_mem_pattern = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
