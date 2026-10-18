"""Timeline thumbnails in two passes: a quick coarse set first, then every keyframe in the background.

1. Coarse: 12-24 single keyframes at evenly spaced seeks (-ss), a few ffmpeg processes at once; ready
   in a second or two even for a long episode, so the timeline is never empty for long.
2. Full: the keyframes of the whole video (-skip_frame nokey), the duration split into ranges decoded
   in parallel; results are handed over in batches while they arrive and the final set is cached.

Limits that keep weak PCs usable: at most one thumbnail per MIN_GAP seconds and MAX_THUMBS per video
(some decoders, e.g. VP9, ignore -skip_frame and decode every frame; a very short GOP would otherwise
yield thousands of pictures), slice threading (frame threading gives no parallelism when only
keyframes are decoded) and low-priority processes. Hardware decoding is not used: copying every frame
back from the GPU made it slower than the CPU for most codecs.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from acs.core.settings import AppDirs
from acs.core.util import file_fingerprint, spawn

THUMB_H = 72
QUALITY_CHOICES = (72, 108, 144, 216)
MIN_GAP = 1.0            # seconds between two thumbnails at least
MAX_THUMBS = 1000        # per video (~60 MB at 108 px, ~250 MB at 216 px)
COARSE_EVERY = 60.0      # seconds of video per coarse thumbnail ...
COARSE_MIN, COARSE_MAX = 12, 24   # ... within these counts (a fitted timeline shows ~8 tiles)
BATCH_SECONDS = 1.0      # hand over full-pass results this often
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


# ---- planning (pure) --------------------------------------------------------------------------
def coarse_times(duration: float) -> list[float]:
    """Seek targets of the coarse pass: evenly spaced, in the middle of equal parts of the video."""
    if duration <= 0:
        return []
    n = max(COARSE_MIN, min(COARSE_MAX, round(duration / COARSE_EVERY)))
    return [(i + 0.5) * duration / n for i in range(n)]


def min_gap(duration: float) -> float:
    return max(MIN_GAP, duration / MAX_THUMBS) if duration > 0 else MIN_GAP


def split_ranges(duration: float, parts: int) -> list[tuple[float, float | None]]:
    """[start, end) ranges of the full pass; the last one is open (runs to the end of the file)."""
    if duration <= 0 or parts <= 1:
        return [(0.0, None)]
    step = duration / parts
    return [(i * step, (i + 1) * step if i < parts - 1 else None) for i in range(parts)]


def workers(cpus: int | None = None) -> tuple[int, int]:
    """(parallel coarse seeks, parallel full-pass ranges) for this many logical CPUs."""
    cpus = cpus or os.cpu_count() or 2
    return max(2, min(4, cpus // 2)), max(1, min(4, cpus // 2))


def merge(times: np.ndarray, images: list, new_times, new_images) -> tuple[np.ndarray, list]:
    """Union of two thumbnail sets sorted by time; a time present in both keeps the existing picture."""
    t = np.concatenate([np.asarray(times, dtype=np.float64), np.asarray(new_times, dtype=np.float64)])
    imgs = list(images) + list(new_images)
    order = np.argsort(t, kind="stable")
    t = t[order]
    keep = np.ones(len(t), dtype=bool)
    keep[1:] = np.diff(t) > 1e-6
    idx = order[keep]
    return t[keep], [imgs[i] for i in idx]


# ---- ffmpeg -----------------------------------------------------------------------------------
def _args(ffmpeg: str, src: str, tw: int, th: int, gap: float, ss: float | None = None,
          single: bool = False, threads: int = 0) -> list[str]:
    """No -t / -to: they count from the keyframe the seek lands on, not from `ss`, and would cut off
    the last keyframes of a range; the reader stops at the range end instead."""
    a = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info"]
    if threads:
        a += ["-threads", str(threads)]
    a += ["-thread_type", "slice", "-skip_frame", "nokey"]
    if ss is not None:
        a += ["-noaccurate_seek", "-ss", f"{ss:.3f}"]
    # -copyts: showinfo reports the file's own timestamps, the same ones the frame index uses
    a += ["-copyts", "-i", src, "-map", "0:v:0", "-an", "-sn", "-dn"]
    if single:
        a += ["-frames:v", "1"]
        vf = f"scale={tw}:{th}:flags=bicubic,showinfo"
    else:
        vf = (f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{gap:.3f})',"
              f"scale={tw}:{th}:flags=bicubic,showinfo")
    return a + ["-vf", vf, "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]


def _frames(args: list[str], tw: int, th: int, start_time: float, cancel=None):
    """Yield (time, image) per decoded thumbnail; the process is killed when the caller stops early."""
    p = spawn(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    times: list[float] = []
    cond = threading.Condition()
    done = [False]

    def read_err():
        try:
            assert p.stderr is not None
            for raw in p.stderr:
                line = raw.decode("utf-8", "replace")
                m = _PTS_RE.search(line)
                if m and "showinfo" in line:
                    with cond:
                        times.append(float(m.group(1)) - start_time)
                        cond.notify_all()
        finally:
            with cond:
                done[0] = True
                cond.notify_all()

    reader = threading.Thread(target=read_err, daemon=True)
    reader.start()
    size = tw * th * 3
    i = 0
    try:
        assert p.stdout is not None
        while not (cancel is not None and cancel()):
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            with cond:   # showinfo prints before the frame is written; wait in the rare other case
                cond.wait_for(lambda: len(times) > i or done[0], timeout=5)
                if len(times) <= i:
                    break
                t = times[i]
            i += 1
            yield t, np.frombuffer(buf, dtype=np.uint8).reshape(th, tw, 3).copy()
    finally:
        if p.poll() is None:
            p.kill()
        p.wait()
        reader.join(timeout=5)
        for pipe in (p.stdout, p.stderr):
            if pipe is not None:
                pipe.close()


def generate(src: str, width: int, height: int, sar: str, start_time: float, ffmpeg: str = "ffmpeg",
             cancel=None, on_progress=None, thumb_h: int = THUMB_H, duration: float = 0.0,
             on_batch=None, cpus: int | None = None) -> tuple[np.ndarray, np.ndarray] | None:
    """Returns (times[N], images[N,H,W,3]) of every keyframe (within the limits) and caches them;
    None if cancelled. `on_batch(times, images)` receives the coarse set and then the full pass in
    parts while they are decoded (from worker threads); it is not called for a cached result."""
    cached = load_cached(src, thumb_h)
    if cached is not None:
        return cached
    cancelled = (lambda: False) if cancel is None else cancel
    tw, th = thumb_size(width, height, sar, thumb_h)
    gap = min_gap(duration)
    n_seek, n_ranges = workers(cpus)

    if on_batch is not None:
        def one(t: float):
            if cancelled():
                return None
            args = _args(ffmpeg, src, tw, th, gap, ss=t, single=True, threads=1)
            with contextlib.closing(_frames(args, tw, th, start_time, cancelled)) as frames:
                return next(frames, None)

        with ThreadPoolExecutor(n_seek, thread_name_prefix="thumbs-coarse") as ex:
            got = [r for r in ex.map(one, coarse_times(duration)) if r is not None]
        if got and not cancelled():
            t, imgs = merge(np.empty(0), [], [g[0] for g in got], [g[1] for g in got])
            on_batch(t, np.stack(imgs))

    ranges = split_ranges(duration, n_ranges)
    threads = max(1, (cpus or os.cpu_count() or 2) // len(ranges))
    results: list[list[tuple[float, np.ndarray]]] = [[] for _ in ranges]
    lock = threading.Lock()
    count = [0]

    def run(k: int) -> None:
        lo, hi = ranges[k]
        args = _args(ffmpeg, src, tw, th, gap, ss=lo if k else None, threads=threads)
        pending: list[tuple[float, np.ndarray]] = []
        last = time.monotonic()
        with contextlib.closing(_frames(args, tw, th, start_time, cancelled)) as frames:
            for t, img in frames:
                if k and t < lo - 1e-6:
                    continue   # the keyframe before the range start belongs to the previous range
                if hi is not None and t >= hi:
                    break
                results[k].append((t, img))
                pending.append((t, img))
                if time.monotonic() - last >= BATCH_SECONDS:
                    last = time.monotonic()
                    flush(pending)
                    pending = []
        if pending and not cancelled():
            flush(pending)

    def flush(part: list[tuple[float, np.ndarray]]) -> None:
        with lock:
            count[0] += len(part)
            n = count[0]
        if on_batch is not None and not cancelled():
            on_batch(np.array([t for t, _ in part]), np.stack([img for _, img in part]))
        if on_progress is not None:
            on_progress(n)

    with ThreadPoolExecutor(len(ranges), thread_name_prefix="thumbs-full") as ex:
        list(ex.map(run, range(len(ranges))))
    if cancelled():
        return None
    allp = [r for part in results for r in part]
    if not allp:
        raise RuntimeError("no thumbnails produced")
    times_a, imgs = merge(np.empty(0), [], [t for t, _ in allp], [img for _, img in allp])
    images = np.stack(imgs)
    try:
        p_cache = cache_path(src, thumb_h)
        p_cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = p_cache.with_name(p_cache.stem + ".tmp.npz")
        np.savez_compressed(tmp, times=times_a, images=images)
        tmp.replace(p_cache)
    except OSError:
        pass
    return times_a, images
