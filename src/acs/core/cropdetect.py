"""Black-bar detection: sample ~10 points of a range with cropdetect and keep the union."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from acs.core.model import CropRect
from acs.core.util import run_capture

_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


def parse_crop(text: str) -> CropRect | None:
    found = _CROP_RE.findall(text)
    if not found:
        return None
    w, h, x, y = map(int, found[-1])
    return CropRect(x, y, w, h)


def union(rects: list[CropRect], src_w: int, src_h: int) -> CropRect | None:
    """Most conservative crop (largest visible area) over all samples; None = nothing to crop."""
    rects = [r for r in rects if r and r.is_valid()]
    if not rects:
        return None
    x0 = min(r.x for r in rects)
    y0 = min(r.y for r in rects)
    x1 = max(r.x + r.w for r in rects)
    y1 = max(r.y + r.h for r in rects)
    x0, y0 = x0 // 2 * 2, y0 // 2 * 2
    w, h = (x1 - x0) // 2 * 2, (y1 - y0) // 2 * 2
    if w >= src_w - 4 and h >= src_h - 4:
        return None
    return CropRect(x0, y0, w, h)


def detect_crop(src: str, t0: float, t1: float, src_w: int, src_h: int, ffmpeg: str = "ffmpeg",
                samples: int = 10) -> CropRect | None:
    span = max(0.0, t1 - t0)
    times = [t0 + span * (i + 0.5) / samples for i in range(samples)] if span > 1 else [t0]

    def one(t: float) -> CropRect | None:
        args = [ffmpeg, "-hide_banner", "-nostdin", "-ss", f"{t:.3f}", "-i", src, "-map", "0:v:0",
                "-frames:v", "4", "-vf", "cropdetect=limit=0.094:round=2:reset=0", "-f", "null", "-"]
        res = run_capture(args, timeout=60, low_priority=True)
        return parse_crop(res.stderr)

    with ThreadPoolExecutor(max_workers=4) as pool:
        rects = list(pool.map(one, times))
    return union([r for r in rects if r], src_w, src_h)
