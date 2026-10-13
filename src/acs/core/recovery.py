"""Crash protection: session lock, pre-task snapshots, crash handlers, orphan cleanup."""

from __future__ import annotations

import faulthandler
import json
import logging
import os
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from acs.core.atomic import atomic_write_json, read_json
from acs.core.model import Project
from acs.core.project_io import PROJECT_EXT, video_key
from acs.core.settings import AppDirs
from acs.core.util import pid_alive

log = logging.getLogger("acs")

MAX_SNAPSHOTS = 10
MAX_LOGS = 20


# ---- snapshots -----------------------------------------------------------------------------
def project_id(project: Project, project_path: str | None) -> str:
    if project_path:
        from acs.core.util import norm_path_key, sha1_text
        return sha1_text(norm_path_key(project_path))[:16]
    return video_key(project.video.path or "untitled")


def snapshot(project: Project, project_path: str | None, reason: str) -> Path:
    """Synchronously write a recovery snapshot; keeps the newest MAX_SNAPSHOTS per project."""
    pid = project_id(project, project_path)
    d = AppDirs.recovery_dir() / pid
    d.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = d / f"snapshot-{ts}-{int(time.time() * 1000) % 1000:03d}-{reason}{PROJECT_EXT}"
    data = project.to_dict()
    data["_recovery"] = {"reason": reason, "project_path": project_path or "", "time": time.time()}
    atomic_write_json(path, data)
    rotate_snapshots(d)
    return path


def rotate_snapshots(d: Path, keep: int = MAX_SNAPSHOTS) -> None:
    snaps = sorted(d.glob(f"snapshot-*{PROJECT_EXT}"), key=lambda p: p.name, reverse=True)
    for old in snaps[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


@dataclass
class RecoveryItem:
    path: Path
    kind: str          # snapshot | autosave
    reason: str
    mtime: float
    video: str
    project_path: str
    clips: int
    markers: int


def list_recovery_items(max_age_days: float = 14) -> list[RecoveryItem]:
    items: list[RecoveryItem] = []
    cutoff = time.time() - max_age_days * 86400
    sources = [(p, "snapshot") for p in AppDirs.recovery_dir().glob(f"*/snapshot-*{PROJECT_EXT}")]
    sources += [(p, "autosave") for p in AppDirs.autosave_dir().glob(f"*{PROJECT_EXT}")]
    for p, kind in sources:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        data = read_json(p, {}) or {}
        rec = data.get("_recovery", {})
        items.append(RecoveryItem(
            path=p, kind=kind, reason=rec.get("reason", "autosave" if kind == "autosave" else ""),
            mtime=mtime, video=(data.get("video") or {}).get("path", ""),
            project_path=rec.get("project_path", ""),
            clips=len(data.get("clips", [])), markers=len(data.get("markers", [])),
        ))
    items.sort(key=lambda i: i.mtime, reverse=True)
    return items


# ---- session lock --------------------------------------------------------------------------
def lock_path() -> Path:
    return AppDirs.data / "session.lock"


def read_stale_lock() -> dict | None:
    """Lock left by a process that is no longer running => previous session crashed."""
    data = read_json(lock_path())
    if not isinstance(data, dict):
        return None
    pid = int(data.get("pid", 0))
    if pid == os.getpid() or pid_alive(pid):
        return None
    return data


def read_live_lock() -> dict | None:
    """Lock held by *another running* instance (two instances would overwrite each other's
    settings and autosaves)."""
    data = read_json(lock_path())
    if not isinstance(data, dict):
        return None
    pid = int(data.get("pid", 0))
    if pid and pid != os.getpid() and pid_alive(pid):
        return data
    return None


def write_lock(current_project: str = "", current_video: str = "") -> None:
    atomic_write_json(lock_path(), {
        "pid": os.getpid(), "started": time.time(),
        "project": current_project, "video": current_video,
    })


def release_lock() -> None:
    data = read_json(lock_path())
    if isinstance(data, dict) and int(data.get("pid", 0)) == os.getpid():
        try:
            lock_path().unlink()
        except OSError:
            pass


# ---- crash handlers ------------------------------------------------------------------------
_fault_file = None
_fault_path: Path | None = None


def native_fault_logging_safe() -> bool:
    """faulthandler must NOT be enabled on Windows while libmpv runs in-process.

    On Windows it installs a vectored exception handler that fires on *first-chance* SEH
    exceptions too. LuaJIT inside libmpv uses SEH exceptions (code 0xE24C4A02) for normal
    control flow; faulthandler then walks Python thread state from that foreign thread without
    the GIL and corrupts memory (access violation in python313.dll). Native crashes on Windows
    are still recorded by Windows Error Reporting and by the session trace.
    """
    return sys.platform != "win32"


def install_crash_handlers(emergency_save: Callable[[], None]) -> Path | None:
    """Python exceptions => emergency snapshot + log (+ faulthandler log where it is safe)."""
    global _fault_file, _fault_path
    logs = AppDirs.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    _rotate_logs(logs)
    if faulthandler.is_enabled() and not native_fault_logging_safe():
        faulthandler.disable()  # e.g. enabled by -X dev / PYTHONFAULTHANDLER
    if native_fault_logging_safe():
        ts = time.strftime("%Y%m%d-%H%M%S")
        _fault_path = logs / f"crash-{ts}.log"
        _fault_file = open(_fault_path, "w", encoding="utf-8")
        faulthandler.enable(_fault_file, all_threads=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(logs / "app.log", encoding="utf-8"), logging.StreamHandler()],
    )

    def _handle(exc_type, exc, tb):
        try:
            emergency_save()
        except Exception:  # noqa: BLE001 - never let the handler raise
            pass
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("Unhandled exception:\n%s", text)
        try:
            from acs.core import trace
            trace.log("error", "unhandled exception:\n" + text)
        except Exception:  # noqa: BLE001
            pass
        if _fault_file is not None:
            try:
                _fault_file.write(text)
                _fault_file.flush()
            except Exception:  # noqa: BLE001
                pass

    def _sys_hook(exc_type, exc, tb):
        _handle(exc_type, exc, tb)
        sys.__excepthook__(exc_type, exc, tb)

    def _thread_hook(args):
        _handle(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    return _fault_path


def shutdown_crash_handlers() -> None:
    """Clean exit: drop the (empty) native-crash log."""
    global _fault_file
    if _fault_file is not None:
        try:
            faulthandler.disable()
        except Exception:  # noqa: BLE001
            pass
    if _fault_file is not None:
        empty = _fault_file.tell() == 0
        _fault_file.close()
        _fault_file = None
        if empty and _fault_path is not None:
            try:
                _fault_path.unlink()
            except OSError:
                pass


def _rotate_logs(logs: Path) -> None:
    crash_logs = sorted(logs.glob("crash-*.log"), key=lambda p: p.name, reverse=True)
    for old in crash_logs[MAX_LOGS:]:
        try:
            old.unlink()
        except OSError:
            pass
    app_log = logs / "app.log"
    try:
        if app_log.exists() and app_log.stat().st_size > 5 * 1024 * 1024:
            app_log.replace(logs / "app.old.log")
    except OSError:
        pass


def latest_crash_log() -> Path | None:
    logs = sorted(AppDirs.logs_dir().glob("crash-*.log"), key=lambda p: p.name, reverse=True)
    for p in logs:
        try:
            if p.stat().st_size > 0:
                return p
        except OSError:
            continue
    return None


# ---- orphan cleanup ------------------------------------------------------------------------
PART_MARK = ".part"


def part_path(final: Path) -> Path:
    return final.with_name(f"{final.stem}{PART_MARK}{final.suffix}")


def find_orphans(output_dirs: list[str]) -> list[Path]:
    found: list[Path] = []
    for d in {os.path.normcase(x) for x in output_dirs if x}:
        p = Path(d)
        if p.is_dir():
            found += [f for f in p.glob(f"*{PART_MARK}.*") if f.is_file()]
    return found


def cleanup_work_dirs() -> int:
    """Remove temp job folders left by a crashed session."""
    import shutil
    n = 0
    work = AppDirs.work_dir()
    if work.is_dir():
        for d in work.iterdir():
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def queue_file() -> Path:
    return AppDirs.recovery_dir() / "queue.json"


def save_queue_state(jobs: list[dict]) -> None:
    atomic_write_json(queue_file(), {"saved": time.time(), "jobs": jobs})


def load_queue_state() -> list[dict]:
    data = read_json(queue_file(), {}) or {}
    return [j for j in data.get("jobs", []) if isinstance(j, dict)]


def dump_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
