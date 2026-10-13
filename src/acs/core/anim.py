"""GIF / animated WebP export: filter graphs and the 'fit into N MB' parameter search."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from acs.core.ffmpeg_cmd import SEEK_EPS, SubsSource, geometry_filters, subs_filters
from acs.core.model import CropRect
from acs.core.presets import (ANIM_FPS_STEPS, ANIM_HEIGHT_STEPS, GIF_COLOR_STEPS, GIF_DITHERS,
                              WEBP_QUALITY_STEPS, Preset)

LADDER = {"gif": ("dither", "fps", "colors", "height"), "webp": ("quality", "fps", "height")}
MAX_FPS = 30


@dataclass(frozen=True)
class AnimParams:
    height: int | None   # None = source height (after crop)
    fps: int
    colors: int = 256
    dither: str = "sierra2_4a"
    quality: int = 85
    lossless: bool = False
    compression_level: int = 4
    loop: bool = True

    def describe(self, kind: str, src_h: int) -> str:
        h = self.height or src_h
        if kind == "gif":
            return f"{h}p {self.fps}fps {self.colors}c {self.dither}"
        return f"{h}p {self.fps}fps q{self.quality}{' lossless' if self.lossless else ''}"


def params_from_preset(p: Preset, src_h: int, src_fps: float, height_override: int | None = None) -> AnimParams:
    h = height_override if height_override is not None else p.target_height
    if h == 0 or (h and h >= src_h):
        h = None
    fps_cap = min(MAX_FPS, int(round(src_fps)) or MAX_FPS)
    fps = min(p.fps or fps_cap, fps_cap)
    return AnimParams(height=h, fps=fps, colors=p.colors, dither=p.dither, quality=p.webp_quality,
                      lossless=p.lossless, compression_level=p.compression_level, loop=p.loop)


def _dither_opt(d: str) -> str:
    if d.startswith("bayer:"):
        return f"bayer:bayer_scale={d.split(':', 1)[1]}"
    return d


def anim_args(kind: str, ffmpeg: str, src: str, start: float, duration: float, params: AnimParams,
              output: str, crop: CropRect | None = None, subs: SubsSource | None = None,
              start_time: float = 0.0, progress: bool = True) -> list[str]:
    pre = subs_filters(subs, start, start_time) + geometry_filters(crop, None)
    chain = pre + [f"fps={params.fps}"]
    if params.height:
        chain.append(f"scale=-2:{params.height}:flags=lanczos")
    head = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-ss", f"{max(0.0, start - SEEK_EPS):.6f}",
            "-t", f"{duration:.6f}", "-i", src]
    tail = (["-progress", "pipe:1", "-nostats"] if progress else []) + ["-loglevel", "warning"]
    loop = ["-loop", "0" if params.loop else "-1"] if kind == "gif" else ["-loop", "0" if params.loop else "1"]
    if kind == "gif":
        graph = (f"[0:v:0]{','.join(chain)},split[a][b];"
                 f"[a]palettegen=max_colors={params.colors}:stats_mode=diff[p];"
                 f"[b][p]paletteuse=dither={_dither_opt(params.dither)}:diff_mode=rectangle[out]")
        return head + ["-filter_complex", graph, "-map", "[out]", "-an", "-sn"] + loop + tail + ["-f", "gif", output]
    return (head + ["-map", "0:v:0", "-vf", ",".join(chain), "-an", "-sn", "-c:v", "libwebp_anim",
                    "-lossless", "1" if params.lossless else "0", "-quality", str(params.quality),
                    "-compression_level", str(params.compression_level), "-pix_fmt", "yuv420p"]
            + loop + tail + ["-f", "webp", output])


def sample_windows(start: float, duration: float, n: int = 3, length: float = 1.0) -> list[tuple[float, float]]:
    """Short windows spread over the clip used to estimate the final size."""
    if duration <= n * length * 1.5:
        return [(start, duration)]
    return [(start + duration * (i + 0.5) / n - length / 2, length) for i in range(n)]


# ---- fit-to-size search -------------------------------------------------------------------
def ladder_steps(param: str, current: AnimParams, mins: dict, src_h: int) -> list:
    """Candidate values from `current` (best) downwards, honouring the user's minimum."""
    if param == "height":
        cur = current.height or src_h
        lo = int(mins.get("height", 0) or 0)
        vals = [cur] + [h for h in ANIM_HEIGHT_STEPS if h < cur and h >= lo]
        return [None if v >= src_h else v for v in vals]
    if param == "fps":
        lo = int(mins.get("fps", 0) or 0)
        return [current.fps] + [f for f in ANIM_FPS_STEPS if f < current.fps and f >= lo]
    if param == "colors":
        lo = int(mins.get("colors", 0) or 0)
        return [current.colors] + [c for c in GIF_COLOR_STEPS if c < current.colors and c >= lo]
    if param == "quality":
        lo = int(mins.get("quality", 0) or 0)
        return [current.quality] + [q for q in WEBP_QUALITY_STEPS if q < current.quality and q >= lo]
    if param == "dither":
        order = list(GIF_DITHERS)
        i = order.index(current.dither) if current.dither in order else 0
        return order[i:]
    raise ValueError(param)


def _with(params: AnimParams, param: str, value) -> AnimParams:
    return replace(params, **{param: value})


@dataclass
class FitResult:
    params: AnimParams
    estimate: float
    reached: bool
    evaluations: int


def fit_params(kind: str, start: AnimParams, target_bytes: float, estimate: Callable[[AnimParams], float],
               src_h: int, locks: dict | None = None, mins: dict | None = None) -> FitResult:
    """Lower parameters in ladder order until estimate(params) <= target_bytes.

    Each unlocked parameter is binary-searched over its steps (sizes are assumed to shrink
    monotonically along a ladder). Locked parameters never change; minimums are never crossed.
    """
    locks = locks or {}
    mins = mins or {}
    cache: dict[AnimParams, float] = {}

    def est(p: AnimParams) -> float:
        if p not in cache:
            cache[p] = float(estimate(p))
        return cache[p]

    cur = start
    if est(cur) <= target_bytes:
        return FitResult(cur, est(cur), True, len(cache))
    for param in LADDER[kind]:
        if locks.get(param):
            continue
        steps = ladder_steps(param, cur, mins, src_h)
        if len(steps) <= 1:
            continue
        lo, hi = 1, len(steps) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if est(_with(cur, param, steps[mid])) <= target_bytes:
                best = mid
                hi = mid - 1
            else:
                lo = mid + 1
        if best is not None:
            cur = _with(cur, param, steps[best])
            return FitResult(cur, est(cur), True, len(cache))
        cur = _with(cur, param, steps[-1])  # strongest allowed step, continue with next param
    return FitResult(cur, est(cur), est(cur) <= target_bytes, len(cache))
