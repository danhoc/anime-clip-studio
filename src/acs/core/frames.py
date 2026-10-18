"""Bulk frame extraction: fixed interval or scene changes, optional subtitles and de-duplication.

Single-pass mode: one ffmpeg per range writes full-size images to a temp folder, prints each
selected frame's pts via `showinfo` and (for de-duplication) streams a 64x36 grey copy to
stdout. Python decides keep/drop and moves kept files into the output folder as they finish,
recording progress in job.state so an interrupted run can resume.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from acs.core.ffmpeg_cmd import SEEK_EPS, SubsSource
from acs.core.jobs import JobContext, JobError, clean_stderr
from acs.core.probe import MediaInfo, probe
from acs.core.settings import AppDirs
from acs.core.subs import SubsError, prepare_subs
from acs.core.util import fmt_time_filename, safe_filename

SMALL_W, SMALL_H = 64, 36
SMALL_BYTES = SMALL_W * SMALL_H
PARALLEL_SEEK_MIN_INTERVAL = 10.0
FORMATS = {"png": "png", "png16": "png", "jpg": "jpg", "webp": "webp"}


@dataclass
class FramesConfig:
    src: str
    ranges: list[tuple[float, float]]
    out_dir: str
    mode: str = "interval"          # interval | scene
    interval: float = 2.0
    scene_threshold: float = 10.0   # scdet score 0..100
    fmt: str = "png"                # png | png16 | jpg | webp
    quality: int = 90
    webp_lossless: bool = False
    sub_rel: int | None = None      # burn subtitles from this track
    dedup: str = "none"             # none | strict | similar
    strict_thresh: float = 1.0      # mean abs diff (0..255) vs last kept
    similar_thresh: float = 6.0
    similar_window: int = 5
    template: str = "{video}_{time}_{frame}"
    hwaccel: bool = True
    fps: float = 23.976

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FramesConfig":
        names = set(cls.__dataclass_fields__)
        cfg = cls(**{k: v for k, v in d.items() if k in names})
        cfg.ranges = [tuple(r) for r in cfg.ranges]
        return cfg


# ---- pure helpers -------------------------------------------------------------------------
def color_filter(info: MediaInfo, fmt: str) -> str:
    v = info.video
    matrix = "bt709"
    if v is not None:
        if v.color_space in ("bt470bg", "smpte170m") or (not v.color_space and v.height < 720):
            matrix = "bt601"
        elif v.color_space.startswith("bt2020"):
            matrix = "bt2020"
    in_range = "pc" if v is not None and v.color_range == "pc" else "tv"
    pix = "rgb48be" if fmt == "png16" else "rgb24"
    return f"scale=in_color_matrix={matrix}:in_range={in_range}:out_range=pc:flags=lanczos,format={pix}"


def encoder_args(cfg: FramesConfig) -> list[str]:
    if cfg.fmt in ("png", "png16"):
        return ["-c:v", "png", "-compression_level", "3"]
    if cfg.fmt == "jpg":
        q = int(round(2 + (100 - max(1, min(100, cfg.quality))) / 100 * 29))
        return ["-c:v", "mjpeg", "-q:v", str(q), "-pix_fmt", "yuvj444p"]
    if cfg.fmt == "webp":
        return ["-c:v", "libwebp", "-quality", str(cfg.quality), "-lossless", "1" if cfg.webp_lossless else "0",
                "-pix_fmt", "bgra" if cfg.webp_lossless else "yuv420p"]
    raise ValueError(cfg.fmt)


def select_filter(cfg: FramesConfig, t0_abs: float) -> str:
    if cfg.mode == "scene":
        return f"scdet=threshold={cfg.scene_threshold:g}:sc_pass=1"
    n = max(0.04, float(cfg.interval))
    # First frame of every new N-second bucket: no drift over long episodes.
    return (f"select='isnan(prev_selected_t)+gte(floor((t-{t0_abs:.6f})/{n:g}),"
            f"floor((prev_selected_t-{t0_abs:.6f})/{n:g})+1)'")


def single_pass_args(ffmpeg: str, cfg: FramesConfig, info: MediaInfo, t0: float, t1: float,
                     tmp_pattern: str, subs: SubsSource | None, with_small: bool) -> list[str]:
    # pts back on the app's relative timeline: that is where the extracted subtitles are timed and
    # what showinfo reports for the file names
    pre = [f"setpts=PTS+{t0:.6f}/TB"]
    if subs is not None:
        pre.append(f"subtitles=f={subs.ass_name}:fontsdir={subs.fonts_name}")
    chain = pre + [select_filter(cfg, t0), "showinfo"]
    if with_small:
        graph = (f"[0:v:0]{','.join(chain)},split=2[f][s];[f]{color_filter(info, cfg.fmt)}[fo];"
                 f"[s]scale={SMALL_W}:{SMALL_H}:flags=area,format=gray[so]")
    else:
        graph = f"[0:v:0]{','.join(chain)},{color_filter(info, cfg.fmt)}[fo]"
    args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "info"]
    if cfg.hwaccel:
        args += ["-hwaccel", "auto"]
    args += ["-ss", f"{max(0.0, t0 - SEEK_EPS):.6f}", "-t", f"{max(0.01, t1 - t0):.6f}", "-i", cfg.src,
             "-filter_complex", graph,
             "-map", "[fo]", "-fps_mode", "passthrough"] + encoder_args(cfg) + ["-f", "image2", tmp_pattern]
    if with_small:
        args += ["-map", "[so]", "-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1"]
    return args


def seek_frame_args(ffmpeg: str, cfg: FramesConfig, info: MediaInfo, t: float, out: str,
                    subs: SubsSource | None) -> list[str]:
    chain = []
    if subs is not None:
        chain += [f"setpts=PTS+{t:.6f}/TB",
                  f"subtitles=f={subs.ass_name}:fontsdir={subs.fonts_name}"]
    chain.append(color_filter(info, cfg.fmt))
    return ([ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{max(0.0, t - SEEK_EPS):.6f}",
             "-i", cfg.src, "-map", "0:v:0", "-frames:v", "1", "-vf", ",".join(chain)]
            + encoder_args(cfg) + ["-f", "image2", "-update", "1", out])


_SHOWINFO_RE = re.compile(r"Parsed_showinfo.*?\bn:\s*(\d+).*?pts_time:\s*([-0-9.]+)")


def parse_showinfo(line: str) -> tuple[int, float] | None:
    m = _SHOWINFO_RE.search(line)
    if not m:
        return None
    return int(m.group(1)), float(m.group(2))


class Deduper:
    """Keep/drop decisions on tiny grey thumbnails (mean absolute difference, 0..255)."""

    def __init__(self, mode: str, strict: float, similar: float, window: int):
        self.mode = mode
        self.strict = strict
        self.similar = similar
        self.window = max(1, window)
        self.kept: list[np.ndarray] = []

    def decide(self, small: np.ndarray) -> bool:
        cur = small.astype(np.float32)
        if self.mode == "none" or not self.kept:
            self._keep(cur)
            return True
        if self.mode == "strict":
            dup = float(np.abs(cur - self.kept[-1]).mean()) < self.strict
        else:
            dup = any(float(np.abs(cur - k).mean()) < self.similar for k in self.kept[-self.window:])
        if not dup:
            self._keep(cur)
        return not dup

    def _keep(self, cur: np.ndarray) -> None:
        self.kept.append(cur)
        if len(self.kept) > self.window:
            del self.kept[0]


DEFAULT_TEMPLATE = "{video}_{time}_{frame}"


def _render(template: str, video_name: str, t: float, frame: int) -> str:
    return template.format(video=video_name, time=fmt_time_filename(t), frame=f"{frame:06d}")


def template_problem(template: str) -> str | None:
    """None when the file-name template is usable; "invalid" (unknown variable, stray brace) or
    "not_unique" (neither {time} nor {frame}: every frame would get the same name)."""
    try:
        _render(template, "v", 0.0, 0)
    except (KeyError, IndexError, ValueError):
        return "invalid"
    if _render(template, "v", 0.0, 0) == _render(template, "v", 1.0, 1):
        return "not_unique"
    return None


def output_name(cfg: FramesConfig, video_name: str, t: float, frame: int) -> str:
    try:
        name = _render(cfg.template, video_name, t, frame)
    except (KeyError, IndexError, ValueError):   # e.g. a template saved by an older version
        name = _render(DEFAULT_TEMPLATE, video_name, t, frame)
    return safe_filename(name) + "." + FORMATS[cfg.fmt]


def default_frames_dir(output_dir: str, src: str) -> str:
    """Where frames of a video go unless the user picks another folder."""
    return str(Path(output_dir) / "frames" / Path(src).stem)


def merge_ranges(ranges) -> list[tuple[float, float]]:
    """Ranges in time order with overlaps joined: resuming relies on the order (done_until), and
    overlapping clips must not extract the same frames twice."""
    out: list[tuple[float, float]] = []
    for a, b in sorted((float(a), float(b)) for a, b in ranges if b > a):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def trim_ranges(ranges: list[tuple[float, float]], done_until: float | None) -> list[tuple[float, float]]:
    if done_until is None:
        return list(ranges)
    out = []
    for a, b in ranges:
        if b <= done_until:
            continue
        out.append((max(a, done_until + 1e-3), b))
    return out


# ---- runner ------------------------------------------------------------------------------
def run_frames(ctx: JobContext, job) -> None:
    st = ctx.settings
    cfg = FramesConfig.from_dict(job.spec["config"])
    cfg.ranges = merge_ranges(cfg.ranges)
    src = cfg.src
    if not Path(src).is_file():
        raise JobError(f"source not found: {src}")
    info = probe(src, st.ffprobe)
    if info.video is None:
        raise JobError("no video stream")
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    subs = None
    if cfg.sub_rel is not None:
        try:
            subs = prepare_subs(src, info, cfg.sub_rel, st.ffmpeg)
        except SubsError as e:
            raise JobError(f"subtitles: {e}") from e
    video_name = Path(src).stem
    fps = float(info.fps)
    done_until = job.state.get("done_until")
    ranges = trim_ranges(cfg.ranges, done_until)
    total = sum(b - a for a, b in cfg.ranges) or 1.0
    job.state.setdefault("kept", 0)
    job.state.setdefault("dropped", 0)
    job.output = str(out_dir)

    def frame_no(t: float) -> int:
        return int(round(t * fps))

    if (cfg.mode == "interval" and cfg.dedup == "none" and cfg.interval >= PARALLEL_SEEK_MIN_INTERVAL):
        _run_parallel_seeks(ctx, job, cfg, info, ranges, subs, out_dir, video_name, frame_no, total)
        return

    work = AppDirs.work_dir() / f"frames_{job.id}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        for (t0, t1) in ranges:
            _single_pass(ctx, job, cfg, info, t0, t1, subs, work, out_dir, video_name, frame_no, total)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    ctx.set_progress(1.0, f"kept {job.state['kept']}, dropped {job.state['dropped']}", force=True)


def _done_fraction(cfg: FramesConfig, t: float, total: float) -> float:
    acc = 0.0
    for a, b in cfg.ranges:
        if t >= b:
            acc += b - a
        elif t > a:
            acc += t - a
    return acc / total


def _single_pass(ctx, job, cfg, info, t0, t1, subs, work: Path, out_dir: Path, video_name, frame_no, total):
    st = ctx.settings
    ext = FORMATS[cfg.fmt]
    with_small = cfg.dedup != "none"
    pattern = f"%08d.{ext}"
    tmp = work / f"r{int(t0 * 1000)}"
    tmp.mkdir(parents=True, exist_ok=True)
    args = single_pass_args(st.ffmpeg, cfg, info, t0, t1, str(tmp / pattern), subs, with_small)
    cwd = subs.workdir if subs else str(work)
    p = ctx.popen(args, cwd=cwd, stdout=subprocess.PIPE if with_small else subprocess.DEVNULL,
                  stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    times: dict[int, float] = {}
    err_tail: list[str] = []
    cond = threading.Condition()
    finished = [False]

    def read_err():
        assert p.stderr is not None
        for raw in p.stderr:
            line = raw.decode("utf-8", "replace")
            r = parse_showinfo(line)
            if r is not None:
                with cond:
                    times[r[0]] = r[1]
                    cond.notify_all()
            elif "error" in line.lower():
                err_tail.append(line.strip())
                del err_tail[:-20]
        with cond:
            finished[0] = True
            cond.notify_all()

    te = threading.Thread(target=read_err, daemon=True)
    te.start()
    dedup = Deduper(cfg.dedup, cfg.strict_thresh, cfg.similar_thresh, cfg.similar_window)
    decisions: dict[int, bool] = {}
    next_move = 0

    def file_for(i: int) -> Path:
        return tmp / (pattern % (i + 1))

    def move_ready(final: bool) -> None:
        nonlocal next_move
        while True:
            i = next_move
            with cond:
                t = times.get(i)
            if t is None:
                return
            if with_small and i not in decisions:
                return
            f = file_for(i)
            if not final and not file_for(i + 1).exists():
                return  # file i may still be being written
            if not f.exists():
                if final:
                    next_move += 1
                    continue
                return
            keep = decisions.get(i, True)
            if keep:
                dest = out_dir / output_name(cfg, video_name, t, frame_no(t))
                shutil.move(str(f), dest)
                job.state["kept"] += 1
            else:
                f.unlink()
                job.state["dropped"] += 1
            job.state["done_until"] = t
            next_move += 1
            ctx.set_progress(_done_fraction(cfg, t, total),
                             f"kept {job.state['kept']}, dropped {job.state['dropped']}")
            if next_move % 25 == 0:
                ctx.save_state()

    try:
        if with_small:
            assert p.stdout is not None
            idx = 0
            while True:
                buf = p.stdout.read(SMALL_BYTES)
                if not buf or len(buf) < SMALL_BYTES:
                    break
                decisions[idx] = dedup.decide(np.frombuffer(buf, dtype=np.uint8))
                idx += 1
                move_ready(False)
        else:
            while p.poll() is None:
                with cond:
                    cond.wait(timeout=0.3)
                move_ready(False)
                ctx.check()
        p.wait()
        te.join(timeout=10)
        ctx.check()
        if p.returncode != 0:
            raise JobError(clean_stderr("\n".join(err_tail))[-500:] or f"ffmpeg exited with {p.returncode}")
        move_ready(True)
        job.state["done_until"] = t1
        ctx.save_state()
    finally:
        ctx._release(p)
        if p.poll() is None:
            p.kill()


def _run_parallel_seeks(ctx, job, cfg, info, ranges, subs, out_dir: Path, video_name, frame_no, total):
    st = ctx.settings
    fps = float(info.fps)
    targets: list[float] = []
    for a, b in ranges:
        t = a
        while t < b:
            # snap to the frame grid (first frame at or after t)
            targets.append(np.ceil(t * fps - 1e-6) / fps)
            t += cfg.interval
    cwd = subs.workdir if subs else None
    lock = threading.Lock()
    processed = [0]

    def one(t: float) -> None:
        if ctx.cancelled:
            return
        name = output_name(cfg, video_name, t, frame_no(t))
        dest = out_dir / name
        if dest.exists():
            with lock:
                processed[0] += 1
            return
        tmp = out_dir / (name + ".part." + FORMATS[cfg.fmt])
        r = ctx.capture(seek_frame_args(st.ffmpeg, cfg, info, t, str(tmp), subs), cwd=cwd, timeout=120)
        if r.returncode != 0 or not tmp.exists():
            raise JobError(clean_stderr(r.stderr)[-400:] or "frame extraction failed")
        os.replace(tmp, dest)
        with lock:
            processed[0] += 1
            job.state["kept"] += 1
            ctx.set_progress(processed[0] / max(1, len(targets)), f"saved {job.state['kept']}")
            if job.state["kept"] % 20 == 0:
                ctx.save_state()

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(one, t) for t in targets]
        try:
            for fut in futures:
                fut.result()
        except BaseException:
            pool.shutdown(cancel_futures=True)   # the first failure ends the job: skip the rest
            raise
    ctx.check()
    ctx.set_progress(1.0, f"saved {job.state['kept']}", force=True)


def run_snapshot(ctx: JobContext, job) -> None:
    """Save one frame (the player's current one). spec: config + its exact time and frame number."""
    st = ctx.settings
    cfg = FramesConfig.from_dict(job.spec["config"])
    t, frame = float(job.spec["time"]), int(job.spec["frame"])
    if not Path(cfg.src).is_file():
        raise JobError(f"source not found: {cfg.src}")
    info = probe(cfg.src, st.ffprobe)
    if info.video is None:
        raise JobError("no video stream")
    subs = None
    if cfg.sub_rel is not None:
        try:
            subs = prepare_subs(cfg.src, info, cfg.sub_rel, st.ffmpeg)
        except SubsError as e:
            raise JobError(f"subtitles: {e}") from e
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / output_name(cfg, Path(cfg.src).stem, t, frame)
    tmp = out_dir / (dest.name + ".part." + FORMATS[cfg.fmt])
    ctx.set_progress(0.1, "saving frame", force=True)
    try:
        r = ctx.capture(seek_frame_args(st.ffmpeg, cfg, info, t, str(tmp), subs),
                        cwd=subs.workdir if subs else None, timeout=120)
        if r.returncode != 0 or not tmp.exists():
            raise JobError(clean_stderr(r.stderr)[-400:] or "frame extraction failed")
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    job.output = str(dest)
    ctx.set_progress(1.0, f"saved {dest.name}", force=True)


def estimate_count(cfg: FramesConfig) -> int:
    span = sum(max(0.0, b - a) for a, b in cfg.ranges)
    if cfg.mode == "interval":
        return int(span / max(0.04, cfg.interval)) + len(cfg.ranges)
    return 0
