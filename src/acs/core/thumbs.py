"""Timeline thumbnails: decode keyframes only (-skip_frame nokey), tiny RGB frames + their times."""

from __future__ import annotations

import re
import subprocess
import threading
from pathlib import Path

import numpy as np

from acs.core.settings import AppDirs
from acs.core.util import file_fingerprint, spawn

THUMB_H = 72
QUALITY_CHOICES = (72, 108, 144, 216)
_PTS_RE = re.compile(r"pts_time:\s*([-0-9.]+)")


def thumb_size(width: int, height: int, sar: str = "1:1", thumb_h: int = THUMB_H) -> tuple[int, int]:
    try:
        n, d = (int(x) for x in sar.split(":"))
        ratio = (width * n / d) / height if d and n else width / height
    except (ValueError, ZeroDivisionError):
        ratio = width / max(1, height)
    thumb_h = min(thumb_h, height) // 2 * 2  # never upscale tiny sources
    w = max(2, int(round(thumb_h * ratio / 2)) * 2)
    return w, thumb_h


def cache_path(src: str, thumb_h: int = THUMB_H) -> Path:
    suffix = "" if thumb_h == THUMB_H else f".h{thumb_h}"
    return AppDirs.media_cache() / f"{file_fingerprint(src)[:16]}.thumbs{suffix}.npz"


def load_cached(src: str, thumb_h: int = THUMB_H) -> tuple[np.ndarray, np.ndarray] | None:
    try:
        p = cache_path(src, thumb_h)
        if p.exists():
            data = np.load(p)
            return data["times"], data["images"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def generate(src: str, width: int, height: int, sar: str, start_time: float, ffmpeg: str = "ffmpeg",
             cancel=None, on_progress=None, hwaccel: bool = True,
             thumb_h: int = THUMB_H) -> tuple[np.ndarray, np.ndarray] | None:
    """Returns (times[N], images[N,H,W,3]) and caches them; None if cancelled."""
    cached = load_cached(src, thumb_h)
    if cached is not None:
        return cached
    tw, th = thumb_size(width, height, sar, thumb_h)
    args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info", "-skip_frame", "nokey"]
    if hwaccel:
        args += ["-hwaccel", "auto"]
    args += ["-i", src, "-map", "0:v:0", "-an", "-sn",
             "-vf", f"scale={tw}:{th}:flags=bicubic,showinfo", "-fps_mode", "passthrough",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    p = spawn(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    times: list[float] = []

    def read_err():
        assert p.stderr is not None
        for raw in p.stderr:
            m = _PTS_RE.search(raw.decode("utf-8", "replace"))
            if m and "showinfo" in raw.decode("utf-8", "replace"):
                times.append(float(m.group(1)) - start_time)

    t = threading.Thread(target=read_err, daemon=True)
    t.start()
    frames: list[np.ndarray] = []
    size = tw * th * 3
    assert p.stdout is not None
    while True:
        if cancel is not None and cancel():
            p.kill()
            break
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        frames.append(np.frombuffer(buf, dtype=np.uint8).reshape(th, tw, 3))
        if on_progress is not None and len(frames) % 20 == 0:
            on_progress(len(frames))
    p.wait()
    t.join(timeout=5)
    for pipe in (p.stdout, p.stderr):
        if pipe is not None:
            pipe.close()
    if cancel is not None and cancel():
        return None
    n = min(len(frames), len(times))
    if n == 0:
        if hwaccel:
            return generate(src, width, height, sar, start_time, ffmpeg, cancel, on_progress, hwaccel=False,
                            thumb_h=thumb_h)
        raise RuntimeError("no thumbnails produced")
    times_a = np.asarray(times[:n], dtype=np.float64)
    images = np.stack(frames[:n])
    order = np.argsort(times_a)
    times_a, images = times_a[order], images[order]
    try:
        p_cache = cache_path(src, thumb_h)
        p_cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = p_cache.with_name(p_cache.stem + ".tmp.npz")
        np.savez_compressed(tmp, times=times_a, images=images)
        tmp.replace(p_cache)
    except OSError:
        pass
    return times_a, images
