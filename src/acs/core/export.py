"""Export runner: turns an export job spec into ffmpeg runs (encode / smart cut / GIF / WebP)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from acs.core import anim, smartcut
from acs.core.clipinfo import effective_crop
from acs.core.ffmpeg_cmd import (ExportSpec, auto_height, bits_per_pixel, bpp_threshold, build_encode_passes,
                                 clamp_height, reencode_reasons, scaled_width, size_video_kbps, source_dims)
from acs.core.jobs import JobContext, JobError, clean_stderr
from acs.core.model import CropRect
from acs.core.presets import CODECS, Preset
from acs.core.probe import MediaInfo, probe, scan_packets
from acs.core.recovery import part_path
from acs.core.settings import AppDirs
from acs.core.subs import SubsError, fit_subs, prepare_subs, remove_fitted, shorts_sub_scale
from acs.core.timebase import FrameIndex

SIZE_TOLERANCE = 1.03
COPY_MIN_PSNR = 60.0
ENC_MIN_PSNR = 30.0


def build_export_spec_dict(*, src: str, clip, in_time: float, out_time: float, audio_rel: int | None,
                           sub_rel: int | None, preset: Preset, output: str, height_req: int | None,
                           hwaccel: bool, settings, info: MediaInfo | None = None) -> dict:
    """Serializable job spec (stored in the persisted queue for crash recovery).

    In Shorts Mode (needs `info` for the picture size) the clip's 9:16 frame replaces its crop."""
    shorts = bool(getattr(settings, "shorts_mode", False)) and info is not None
    crop = effective_crop(clip, info, shorts)
    return {
        "src": src, "clip_id": clip.id, "clip_name": clip.name,
        "in_frame": clip.in_frame, "out_frame": clip.out_frame, "in_time": in_time, "out_time": out_time,
        "audio_rel": None if clip.muted else audio_rel,
        "burn_subs": bool(clip.burn_subs and sub_rel is not None), "sub_rel": sub_rel,
        "sub_scale": max(10, clip.sub_size) / 100,
        "crop": None if crop is None else {"x": crop.x, "y": crop.y, "w": crop.w, "h": crop.h},
        "height_req": height_req, "preset": preset.to_dict(), "output": output, "hwaccel": hwaccel,
        "bpp_x264": settings.bpp_x264, "bpp_hevc_av1": settings.bpp_hevc_av1, "shorts": shorts,
    }


def _check_disk(path: str, est_bytes: float) -> None:
    d = Path(path).parent
    d.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(d).free
    if est_bytes > 0 and free < est_bytes * 1.5 + 50 * 1024 * 1024:
        raise JobError(f"not enough disk space in {d} (free {free / 2**20:.0f} MB)")


def run_export(ctx: JobContext, job) -> None:
    s = job.spec
    st = ctx.settings
    src = s["src"]
    if not Path(src).is_file():
        raise JobError(f"source not found: {src}")
    preset = Preset.from_dict(s["preset"])
    final = Path(s["output"])
    if final.exists() and job.state.get("completed"):
        ctx.set_progress(1.0, "already done", force=True)
        return
    part = part_path(final)
    work = AppDirs.work_dir() / job.id
    work.mkdir(parents=True, exist_ok=True)
    ctx.set_progress(0.0, "probing", force=True)
    info = probe(src, st.ffprobe)
    if info.video is None:
        raise JobError("no video stream")
    crop = CropRect(**s["crop"]) if s.get("crop") else None
    subs = None
    if s.get("burn_subs"):
        try:
            subs = prepare_subs(src, info, int(s["sub_rel"] or 0), st.ffmpeg)
            # dialogue inside the exported picture (Shorts frame or crop), at the chosen size
            v = info.video
            scale = float(s.get("sub_scale") or 1.0)
            if s.get("shorts") and crop is not None:
                scale *= shorts_sub_scale(crop.h, v.height)
            subs = fit_subs(subs, v.width, v.height, crop, scale, job.id)
        except SubsError as e:
            raise JobError(f"subtitles: {e}") from e
        except OSError as e:
            raise JobError(f"subtitles: cannot prepare the subtitle file: {e}") from e
    src_w, src_h = source_dims(info, crop)
    height = clamp_height(s.get("height_req"), src_h)
    spec = ExportSpec(src=src, in_frame=s["in_frame"], out_frame=s["out_frame"], in_time=s["in_time"],
                      out_time=s["out_time"], audio_rel=s.get("audio_rel"), preset=preset, output=str(part),
                      crop=crop, height=height, subs=subs, hwaccel=bool(s.get("hwaccel")))
    _check_disk(str(final), info.bitrate / 8 * spec.duration if info.bitrate else 0)
    try:
        if preset.kind in ("gif", "webp"):
            _run_anim(ctx, spec, info, preset, work, src_w, src_h)
        elif preset.mode == "smartcut" and not reencode_reasons(bool(subs), crop, height):
            _run_smartcut(ctx, spec, info, work)
        else:
            _run_encode(ctx, spec, info, work, s)
        if not part.exists() or part.stat().st_size == 0:
            raise JobError("ffmpeg produced no output")
        os.replace(part, final)
        job.output = str(final)
        job.state["completed"] = True
        ctx.set_progress(1.0, f"done — {final.stat().st_size / 2**20:.1f} MB", force=True)
    finally:
        if part.exists() and not job.state.get("completed"):
            try:
                part.unlink()
            except OSError:
                pass
        remove_fitted(subs)
        shutil.rmtree(work, ignore_errors=True)


# ---- encode ----------------------------------------------------------------------------------
def _cwd(spec: ExportSpec, work: Path) -> str:
    return spec.subs.workdir if spec.subs else str(work)


def _run_passes(ctx: JobContext, spec: ExportSpec, info: MediaInfo, work: Path, span=(0.02, 1.0)) -> None:
    passes = build_encode_passes(spec, info, ctx.settings.ffmpeg)
    passlog = f"passlog_{ctx.job.id}"
    passes = [[passlog if a == "passlog" else a.replace("stats=passlog.", f"stats={passlog}.") for a in p]
              for p in passes]
    a, b = span
    n = len(passes)
    try:
        for i, args in enumerate(passes):
            lo = a + (b - a) * i / n
            hi = a + (b - a) * (i + 1) / n
            label = "encoding" if n == 1 else f"encoding pass {i + 1}/{n}"
            try:
                ctx.run(args, cwd=_cwd(spec, work), duration=spec.duration, span=(lo, hi), message=label)
            except JobError:
                if spec.hwaccel:  # hardware decode can fail on odd streams: retry in software
                    ctx.warn("hwaccel_fallback")
                    spec.hwaccel = False
                    return _run_passes(ctx, spec, info, work, span)
                raise
    finally:
        for f in Path(_cwd(spec, work)).glob(f"{passlog}*"):
            try:
                f.unlink()
            except OSError:
                pass


def _audio_kbps(spec: ExportSpec, info: MediaInfo) -> int:
    if spec.audio_rel is None or not info.audios:
        return 0
    if spec.preset.acodec == "copy":
        a = info.audios[min(spec.audio_rel, len(info.audios) - 1)]
        return max(96, a.bitrate // 1000) if a.bitrate else 640
    return spec.preset.abitrate


def _run_encode(ctx: JobContext, spec: ExportSpec, info: MediaInfo, work: Path, s: dict) -> None:
    p = spec.preset
    codec = CODECS.get(p.vcodec)
    if codec is None:
        raise JobError(f"unsupported encoder {p.vcodec}")
    if p.target_size_mb:
        fps = float(info.fps)
        src_w, src_h = source_dims(info, spec.crop)
        kbps = size_video_kbps(p.target_size_mb, spec.duration, _audio_kbps(spec, info))
        thr = bpp_threshold(p.vcodec, s.get("bpp_x264", 0.06), s.get("bpp_hevc_av1", 0.035))
        if p.allow_auto_downscale:
            h, bpp = auto_height(src_w, src_h, fps, kbps, thr, p.min_auto_height, spec.height or src_h)
            if h != spec.height:
                ctx.log(f"auto downscale to {h or src_h}p (bpp {bpp:.3f})")
            spec.height = h
            ctx.job.state["auto_height"] = h or src_h
            if bpp < thr:
                ctx.warn("low_bpp")
        else:
            hh = spec.height or src_h
            if bits_per_pixel(kbps, scaled_width(hh, src_w, src_h), hh, fps) < thr:
                ctx.warn("low_bpp")
        spec.video_kbps = kbps
        target = p.target_size_mb * 1024 * 1024
        _run_passes(ctx, spec, info, work, span=(0.02, 0.9))
        actual = Path(spec.output).stat().st_size
        if actual > target * SIZE_TOLERANCE:
            spec.video_kbps = max(50, int(kbps * target / actual * 0.97))
            ctx.log(f"size {actual / 2**20:.2f} MB over target, retry at {spec.video_kbps} kbps")
            _run_passes(ctx, spec, info, work, span=(0.9, 1.0))
            if Path(spec.output).stat().st_size > target * SIZE_TOLERANCE:
                ctx.warn("size_not_reached")
    else:
        _run_passes(ctx, spec, info, work)


# ---- smart cut -------------------------------------------------------------------------------
def _run_smartcut(ctx: JobContext, spec: ExportSpec, info: MediaInfo, work: Path) -> None:
    st = ctx.settings
    family = info.video.codec if info.video else ""
    if family not in smartcut.SMART_CODECS:
        ctx.warn("smartcut_codec")
        return _smart_fallback(ctx, spec, info, work, "codec")
    ctx.set_progress(0.02, "indexing keyframes", force=True)
    scan = scan_packets(spec.src, info.start_time, st.ffprobe, cancel=lambda: ctx.cancelled)
    ctx.check()
    fi = FrameIndex.from_scan(scan, info.fps)

    def is_idr(j: int) -> bool:
        kt = fi.time_of(int(fi.gops.key_frame[j]))
        r = ctx.capture(smartcut.nal_probe_args(st.ffmpeg, spec.src, kt), timeout=60)
        return smartcut.parse_first_vcl_nal(r.stderr, family) in smartcut.IDR_TYPES[family]

    plan = smartcut.plan_segments(spec.in_frame, spec.out_frame, fi, is_idr)
    if plan.full_encode:
        ctx.log(f"smart cut not possible ({plan.reason})")
        ctx.warn(f"smartcut_{plan.reason}")
        return _smart_fallback(ctx, spec, info, work, plan.reason)

    total = max(1, sum(s.frames * (1 if s.kind == "enc" else 0.05) for s in plan.segments))
    done = 0.0
    names = []
    for i, seg in enumerate(plan.segments):
        name = f"seg{i}.ts"
        weight = seg.frames * (1 if seg.kind == "enc" else 0.05) / total
        lo, hi = 0.05 + 0.75 * done, 0.05 + 0.75 * (done + weight)
        if seg.kind == "enc":
            args = smartcut.enc_segment_args(st.ffmpeg, spec.src, info, fi, seg, name)
            dur = fi.time_of(seg.end_f) - fi.time_of(seg.start_f)
            ctx.run(args, cwd=str(work), duration=dur, span=(lo, hi), message="re-encoding edges")
        else:
            args = smartcut.copy_segment_args(st.ffmpeg, spec.src, seg, name, fi.time_of(seg.end_f),
                                              info.start_time)
            ctx.run(args, cwd=str(work), span=(lo, hi), message="copying")
        names.append(name)
        done += weight
    ctx.run(smartcut.mux_args(st.ffmpeg, spec, info, names), cwd=str(work), duration=spec.duration,
            span=(0.8, 0.88), message="muxing")
    problem = _validate_smart(ctx, spec, plan, fi)
    if problem:
        ctx.log(f"smart cut validation failed: {problem}")
        ctx.warn("smartcut_validation")
        try:
            os.unlink(spec.output)
        except OSError:
            pass
        _smart_fallback(ctx, spec, info, work, "validation")


def _validate_smart(ctx: JobContext, spec: ExportSpec, plan: smartcut.SmartPlan, fi: FrameIndex) -> str:
    st = ctx.settings
    ctx.set_progress(0.9, "verifying", force=True)
    r = ctx.capture(smartcut.count_packets_args(st.ffprobe, spec.output))
    try:
        n = int(r.stdout.strip().split(",")[0])
    except ValueError:
        return "cannot count frames"
    if n != spec.frames:
        return f"frame count {n} != {spec.frames}"
    r = ctx.capture(smartcut.decode_check_args(st.ffmpeg, spec.output))
    errors = clean_stderr(r.stderr)
    if r.returncode != 0 or errors:
        return f"decode errors: {errors[-300:]}"
    for off in smartcut.check_points(plan, spec.in_frame):
        f = spec.in_frame + off
        seg = next(s for s in plan.segments if s.start_f <= f < s.end_f)
        r = ctx.capture(smartcut.frame_psnr_args(st.ffmpeg, spec.output, spec.src, spec.in_time, off,
                                                 fi.time_of(f)))
        psnr = smartcut.parse_psnr(r.stderr)
        need = COPY_MIN_PSNR if seg.kind == "copy" else ENC_MIN_PSNR
        if psnr is None or psnr < need:
            return f"frame {off}: psnr {psnr} < {need}"
    return ""


def _smart_fallback(ctx: JobContext, spec: ExportSpec, info: MediaInfo, work: Path, reason: str) -> None:
    ctx.set_progress(0.05, f"full re-encode ({reason})", force=True)
    spec.preset = smartcut.fallback_preset(info, spec.preset)
    spec.video_kbps = None
    _run_passes(ctx, spec, info, work, span=(0.05, 1.0))


# ---- GIF / WebP ------------------------------------------------------------------------------
def _run_anim(ctx: JobContext, spec: ExportSpec, info: MediaInfo, preset: Preset, work: Path,
              src_w: int, src_h: int) -> None:
    st = ctx.settings
    kind = preset.kind
    params = anim.params_from_preset(preset, src_h, float(info.fps), spec.height if spec.height else None)
    cwd = _cwd(spec, work)

    def encode(p: anim.AnimParams, start: float, dur: float, out: str, span=None, message=None) -> int:
        args = anim.anim_args(kind, st.ffmpeg, spec.src, start, dur, p, out, spec.crop, spec.subs)
        if span is None:
            r = ctx.capture(args, cwd=cwd)
            if r.returncode != 0:
                raise JobError(clean_stderr(r.stderr)[-400:] or "encode failed")
        else:
            ctx.run(args, cwd=cwd, duration=dur, span=span, message=message)
        return Path(out).stat().st_size

    if preset.fit_size_mb:
        target = preset.fit_size_mb * 1024 * 1024
        windows = anim.sample_windows(spec.in_time, spec.duration)
        sample_total = sum(d for _, d in windows)
        raw_cache: dict[anim.AnimParams, float] = {}

        def raw_estimate(p: anim.AnimParams) -> float:
            if p not in raw_cache:
                ctx.check()
                ctx.set_progress(min(0.6, 0.05 + 0.04 * len(raw_cache)),
                                 f"estimating size ({p.describe(kind, src_h)})")
                size = 0
                for k, (t, d) in enumerate(windows):
                    size += encode(p, t, d, str(work / f"sample{k}.{kind}"))
                raw_cache[p] = size * spec.duration / sample_total
            return raw_cache[p]

        # Samples mis-estimate the full clip (palette / inter-frame effects), so after each real
        # encode the estimator is rescaled by actual/estimated and the search repeated. The
        # largest result that fits is kept.
        bias = 1.0
        best: tuple[int, anim.AnimParams] | None = None
        best_file = work / f"best.{kind}"
        tried: set[anim.AnimParams] = set()
        for attempt in range(3):
            res = anim.fit_params(kind, params, target, lambda p: raw_estimate(p) * bias, src_h,
                                  preset.locks, preset.mins)
            if res.params in tried:
                break
            tried.add(res.params)
            ctx.log(f"fit attempt {attempt}: {res.params.describe(kind, src_h)} est {res.estimate / 2**20:.2f} MB")
            actual = encode(res.params, spec.in_time, spec.duration, spec.output, span=(0.6, 0.98),
                            message=f"encoding {res.params.describe(kind, src_h)}")
            if actual <= target * SIZE_TOLERANCE and (best is None or actual > best[0]):
                best = (actual, res.params)
                shutil.copyfile(spec.output, best_file)
            if target * 0.8 <= actual <= target * SIZE_TOLERANCE:
                break  # close enough
            if res.params == params and actual <= target * SIZE_TOLERANCE:
                break  # already at the starting (best) quality
            raw = raw_cache.get(res.params) or res.estimate
            bias = actual / raw if raw else bias
        if best is not None:
            shutil.copyfile(best_file, spec.output)
            ctx.job.state["anim_params"] = best[1].describe(kind, src_h)
        else:
            ctx.job.state["anim_params"] = res.params.describe(kind, src_h)
            ctx.warn("size_not_reached")
            ctx.job.state["anim_size_mb"] = round(Path(spec.output).stat().st_size / 2**20, 2)
    else:
        encode(params, spec.in_time, spec.duration, spec.output, span=(0.02, 1.0), message="encoding")
        ctx.job.state["anim_params"] = params.describe(kind, src_h)

