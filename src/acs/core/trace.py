"""Session event trace: one line per event, flushed immediately so it survives a hard crash.

logs/trace-<timestamp>.log — user input, actions, player commands, background work, jobs.
Verbose mode (diagnostic launch) also records high-frequency events (paints, playback time).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

MAX_TRACE_FILES = 20

_lock = threading.Lock()
_file = None
_path: Path | None = None
_verbose = False
_last_throttle: dict[str, float] = {}


def start(logs_dir: Path, verbose: bool = False) -> Path:
    global _file, _path, _verbose
    logs_dir.mkdir(parents=True, exist_ok=True)
    old = sorted(logs_dir.glob("trace-*.log"), key=lambda p: p.name, reverse=True)
    for p in old[MAX_TRACE_FILES - 1:]:
        try:
            p.unlink()
        except OSError:
            pass
    _path = logs_dir / f"trace-{time.strftime('%Y%m%d-%H%M%S')}.log"
    _file = open(_path, "a", encoding="utf-8", buffering=1)
    _verbose = verbose
    log("session", f"trace started (verbose={verbose})")
    return _path


def path() -> Path | None:
    return _path


def is_verbose() -> bool:
    return _verbose


def log(category: str, message: str) -> None:
    f = _file
    if f is None:
        return
    now = time.time()
    stamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"
    line = f"{stamp} [{threading.current_thread().name}] {category}: {message}\n"
    with _lock:
        try:
            f.write(line)
            f.flush()  # into the OS cache: survives a process crash
        except (OSError, ValueError):
            pass


def vlog(category: str, message: str) -> None:
    if _verbose:
        log(category, message)


def throttled(key: str, interval: float, category: str, message: str) -> None:
    """Log at most once per `interval` seconds for this key (for very frequent events)."""
    now = time.monotonic()
    if now - _last_throttle.get(key, 0.0) >= interval:
        _last_throttle[key] = now
        log(category, message)


def stop() -> None:
    global _file
    log("session", "trace stopped (clean exit)")
    with _lock:
        if _file is not None:
            try:
                _file.close()
            except OSError:
                pass
            _file = None
