"""Small shared helpers: subprocess flags, time formatting, natural sort, hashing."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BELOW_NORMAL = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)


def creation_flags(low_priority: bool = False) -> int:
    flags = CREATE_NO_WINDOW
    if low_priority:
        flags |= BELOW_NORMAL
    return flags


_kill_job = None


def _kill_on_close_job():
    """Windows Job Object with KILL_ON_JOB_CLOSE: when the app dies (even via TerminateProcess)
    the OS closes the handle and kills every ffmpeg/ffprobe we started."""
    global _kill_job
    if _kill_job is not None or not IS_WINDOWS:
        return _kill_job
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("Read", "Write", "Other", "ReadT", "WriteT", "OtherT")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    handle = k32.CreateJobObjectW(None, None)
    if not handle:
        return None
    info = EXTENDED()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        return None
    _kill_job = (k32, handle)
    return _kill_job


def spawn(args: list[str], cwd: str | Path | None = None, low_priority: bool = True, **kw) -> subprocess.Popen:
    """Popen that never outlives the app (Windows job object), no console window, low priority."""
    p = subprocess.Popen(args, cwd=cwd, creationflags=creation_flags(low_priority), **kw)
    job = _kill_on_close_job()
    if job is not None:
        k32, handle = job
        try:
            k32.AssignProcessToJobObject(handle, int(p._handle))  # noqa: SLF001
        except (OSError, AttributeError):
            pass
    return p


def run_capture(args: list[str], cwd: str | Path | None = None, timeout: float | None = None,
                low_priority: bool = False) -> subprocess.CompletedProcess:
    """Run a short helper process (ffprobe etc.) and capture its output as UTF-8 text."""
    p = spawn(args, cwd=cwd, low_priority=low_priority, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
              stdin=subprocess.DEVNULL, encoding="utf-8", errors="replace")
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        raise subprocess.TimeoutExpired(args, timeout, out, err)
    return subprocess.CompletedProcess(args, p.returncode, out, err)


def fmt_time(t: float | None, ms: bool = True) -> str:
    """Seconds -> 'HH:MM:SS.mmm' (or without milliseconds)."""
    if t is None:
        return "--:--:--"
    neg = t < 0
    t = abs(t)
    total_ms = int(round(t * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, msec = divmod(rem, 1000)
    out = f"{h:02d}:{m:02d}:{s:02d}" + (f".{msec:03d}" if ms else "")
    return "-" + out if neg else out


def fmt_time_filename(t: float) -> str:
    return fmt_time(t).replace(":", "-").replace(".", "-")


def fmt_duration(t: float | None) -> str:
    if t is None:
        return ""
    if t < 60:
        return f"{t:.2f}s"
    return fmt_time(t, ms=False)


def fmt_size(n: float | None) -> str:
    if n is None:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


_NAT_RE = re.compile(r"(\d+)")


def natural_key(s: str) -> list:
    """Sort key so that 'Ep 2' < 'Ep 10'."""
    return [int(p) if p.isdigit() else p.casefold() for p in _NAT_RE.split(s)]


def sha1_text(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def norm_path_key(p: str | Path) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


_fp_cache: dict[tuple, str] = {}
_fp_lock = threading.Lock()


def file_fingerprint(path: str | Path, chunk: int = 4 * 1024 * 1024) -> str:
    """sha1 of size + first and last `chunk` bytes. Cheap identity for large video files.

    Remembered per (path, size, mtime): opening a video asks for it several times (project,
    thumbnail and waveform caches), and each call would otherwise read 8 MB from disk."""
    p = Path(path)
    st = p.stat()
    key = (os.path.normcase(os.path.abspath(str(p))), st.st_size, st.st_mtime_ns, chunk)
    with _fp_lock:
        hit = _fp_cache.get(key)
    if hit is not None:
        return hit
    size = st.st_size
    h = hashlib.sha1(str(size).encode())
    with open(p, "rb") as f:
        h.update(f.read(chunk))
        if size > chunk:
            f.seek(max(chunk, size - chunk))
            h.update(f.read(chunk))
    digest = h.hexdigest()
    with _fp_lock:
        if len(_fp_cache) > 256:
            _fp_cache.clear()
        _fp_cache[key] = digest
    return digest


def safe_filename(name: str) -> str:
    """Strip characters Windows does not allow in file names."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    return name or "untitled"


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
