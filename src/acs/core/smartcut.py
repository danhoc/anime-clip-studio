"""Smart cut: re-encode only the partial GOPs at the clip edges, stream-copy the middle.

Copy segments start only at *closed* keyframes that are real IDR pictures (checked with
trace_headers), so no RASL/leading-picture or POC continuity issues can appear at the splice.
Segments are written as MPEG-TS (in-band parameter sets + per-packet timestamps) and joined
with the concat demuxer. Anything unusual falls back to a full high-quality re-encode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Callable

from acs.core.ffmpeg_cmd import SEEK_EPS, ExportSpec, audio_args, color_args, container_for
from acs.core.presets import Preset
from acs.core.probe import MediaInfo
from acs.core.timebase import FrameIndex

SMART_CODECS = ("hevc", "h264")
IDR_TYPES = {"hevc": {19, 20}, "h264": {5}}
MAX_CRA_SKIPS = 3


@dataclass
class Segment:
    kind: str          # enc | copy
    start_f: int
    end_f: int         # exclusive
    packets: int = 0   # copy only: number of packets (decode order) to copy
    key_time: float = 0.0  # copy only: keyframe pts to seek to

    @property
    def frames(self) -> int:
        return self.end_f - self.start_f


@dataclass
class SmartPlan:
    segments: list[Segment] = field(default_factory=list)
    full_encode: bool = False
    reason: str = ""

    @property
    def copied_frames(self) -> int:
        return sum(s.frames for s in self.segments if s.kind == "copy")


def plan_segments(in_f: int, out_f: int, fi: FrameIndex,
                  is_idr: Callable[[int], bool] | None = None,
                  max_skips: int = MAX_CRA_SKIPS) -> SmartPlan:
    """Pure planning step. `is_idr(gop_index)` verifies the NAL type of a candidate keyframe."""
    g = fi.gops
    if not fi.exact or g is None or len(g) == 0:
        return SmartPlan(full_encode=True, reason="no_gop_info")
    if out_f - in_f < 2:
        return SmartPlan(full_encode=True, reason="too_short")
    n = len(g)
    # Sentinel "keyframe" at end of stream so a clip can be copied up to the last frame.
    key_frame = list(map(int, g.key_frame)) + [fi.frame_count]
    key_pkt = list(map(int, g.key_pkt)) + [fi.packet_count]
    gop_start = list(map(int, g.gop_start_frame)) + [fi.frame_count]

    j1 = None
    skips = 0
    for j in range(n):
        if key_frame[j] < in_f:
            continue
        if key_frame[j] >= out_f:
            break
        if gop_start[j] != key_frame[j]:
            continue  # open GOP: leading pictures would reference the previous GOP
        if is_idr is not None and not is_idr(j):
            skips += 1
            if skips >= max_skips:
                break
            continue
        j1 = j
        break
    if j1 is None:
        return SmartPlan(full_encode=True, reason="no_idr_in_range")

    jE = None
    for j in range(n, j1, -1):  # include sentinel n
        if gop_start[j] <= out_f:
            jE = j
            break
    if jE is None or jE <= j1:
        return SmartPlan(full_encode=True, reason="no_full_gop")

    copy_start = key_frame[j1]
    copy_end = gop_start[jE]
    packets = key_pkt[jE] - key_pkt[j1]
    if copy_end <= copy_start or packets != copy_end - copy_start:
        return SmartPlan(full_encode=True, reason="gop_mismatch")

    segs: list[Segment] = []
    if in_f < copy_start:
        segs.append(Segment("enc", in_f, copy_start))
    segs.append(Segment("copy", copy_start, copy_end, packets=packets, key_time=fi.time_of(copy_start)))
    if copy_end < out_f:
        segs.append(Segment("enc", copy_end, out_f))
    return SmartPlan(segs)


# ---- ffmpeg command builders ------------------------------------------------------------
# trace_headers prints e.g. "nal_unit_type          010100 = 20"
_NAL_RE = re.compile(r"nal_unit_type\s+[01]*\s*=\s*(\d+)")


def parse_first_vcl_nal(trace_text: str, family: str) -> int | None:
    for m in _NAL_RE.finditer(trace_text):
        t = int(m.group(1))
        if family == "hevc" and t < 32:
            return t
        if family == "h264" and 1 <= t <= 5:
            return t
    return None


def nal_probe_args(ffmpeg: str, src: str, key_time: float) -> list[str]:
    return [ffmpeg, "-hide_banner", "-nostdin", "-ss", f"{key_time:.6f}", "-i", src, "-map", "0:v:0",
            "-c:v", "copy", "-frames:v", "1", "-bsf:v", "trace_headers", "-f", "null", "-"]


def matching_encoder(info: MediaInfo, quality: int | None = None) -> list[str]:
    """Encoder args that reproduce the source stream's format as closely as possible."""
    v = info.video
    assert v is not None
    ten = v.bit_depth > 8
    if v.codec == "hevc":
        pix = "yuv420p10le" if ten else "yuv420p"
        return ["-c:v", "libx265", "-pix_fmt", pix, "-preset", "medium", "-crf", str(quality or 14),
                "-x265-params", "repeat-headers=1:log-level=error"]
    pix = "yuv420p10le" if ten else "yuv420p"
    args = ["-c:v", "libx264", "-pix_fmt", pix, "-preset", "medium", "-crf", str(quality or 12)]
    if ten:
        args += ["-profile:v", "high10"]
    return args


# Segments keep the source's absolute timestamps (-copyts, no mux delay) so that joining the
# MPEG-TS files byte-wise (concat protocol) yields one continuous timeline. The concat
# *demuxer* is not used: it re-bases each file on its first dts, which shifts B-frame GOPs.
_TS_OUT = ["-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts"]


def enc_segment_args(ffmpeg: str, src: str, info: MediaInfo, fi: FrameIndex, seg: Segment,
                     out_name: str) -> list[str]:
    t0 = fi.time_of(seg.start_f)
    return ([ffmpeg, "-hide_banner", "-nostdin", "-y", "-copyts", "-ss", f"{max(0.0, t0 - SEEK_EPS):.6f}",
             "-i", src, "-map", "0:v:0"] + matching_encoder(info) + color_args(info)
            + ["-fps_mode", "passthrough", "-an", "-sn", "-dn", "-frames:v", str(seg.frames),
               "-progress", "pipe:1", "-nostats", "-loglevel", "warning"] + _TS_OUT + [out_name])


def copy_segment_args(ffmpeg: str, src: str, seg: Segment, out_name: str, end_time: float,
                      start_time: float = 0.0) -> list[str]:
    """Stream-copy exactly the packets whose pts lies in [key_time, end_time).

    Input -ss with stream copy is only a coarse seek (the demuxer may land several GOPs
    earlier), so the precise selection is done by the `noise` bsf on absolute (-copyts) pts.
    With closed GOPs every packet of the copied GOPs is inside that range and nothing else is.
    """
    lo = seg.key_time + start_time - SEEK_EPS
    hi = end_time + start_time - SEEK_EPS
    drop = f"noise=drop=lt(pts*tb\\,{lo:.6f})+gte(pts*tb\\,{hi:.6f})"
    return ([ffmpeg, "-hide_banner", "-nostdin", "-y", "-copyts",
             "-ss", f"{seg.key_time:.6f}", "-to", f"{end_time + 3.0:.6f}", "-i", src,
             "-map", "0:v:0", "-c:v", "copy", "-bsf:v", drop, "-an", "-sn", "-dn",
             "-progress", "pipe:1", "-nostats", "-loglevel", "warning"] + _TS_OUT + [out_name])


def concat_input(names: list[str]) -> str:
    return "concat:" + "|".join(names)


def mux_args(ffmpeg: str, spec: ExportSpec, info: MediaInfo, names: list[str]) -> list[str]:
    container = container_for(spec.preset, info)
    a = audio_args(spec, info, container)
    a = [x.replace("0:a:", "1:a:") if x.startswith("0:a:") else x for x in a]
    family = info.video.codec if info.video else ""
    tag: list[str] = []
    if container == "mp4":
        tag = ["-tag:v", "hev1" if family == "hevc" else "avc3", "-movflags", "+faststart"]
    fmt = ["-f", "matroska"] if container == "mkv" else []
    inputs = ["-f", "mpegts", "-i", concat_input(names)]
    if spec.audio_rel is not None and info.audios:
        inputs += ["-ss", f"{max(0.0, spec.in_time - SEEK_EPS):.6f}", "-i", spec.src]
    return ([ffmpeg, "-hide_banner", "-nostdin", "-y"] + inputs + ["-map", "0:v:0", "-c:v", "copy"] + a
            + ["-t", f"{spec.duration:.6f}", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1"]
            + tag + fmt + ["-progress", "pipe:1", "-nostats", "-loglevel", "warning", spec.output])


def fallback_preset(info: MediaInfo, base: Preset) -> Preset:
    """Full re-encode with the source codec family at high quality (used when smart cut can't)."""
    v = info.video
    codec = "libx265" if v is not None and v.codec == "hevc" else "libx264"
    depth = 10 if v is not None and v.bit_depth > 8 else 8
    container = container_for(base, info)
    acodec = base.acodec if base.acodec != "copy" or container != "webm" else "libopus"
    return replace(base, mode="encode", vcodec=codec, bit_depth=depth,
                   quality=14 if codec == "libx265" else 12, speed="medium", tune="",
                   container=container, acodec=acodec, target_size_mb=None)


# ---- validation ----------------------------------------------------------------------------
def count_packets_args(ffprobe: str, path: str) -> list[str]:
    return [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
            "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path]


def decode_check_args(ffmpeg: str, path: str) -> list[str]:
    return [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-i", path, "-map", "0:v:0", "-f", "null", "-"]


def frame_psnr_args(ffmpeg: str, out: str, src: str, in_time: float, offset_frames: int,
                    src_time: float) -> list[str]:
    """PSNR of output frame #offset_frames against the source frame at src_time."""
    graph = (f"[0:v:0]trim=start_frame={offset_frames}:end_frame={offset_frames + 1},"
             "setpts=PTS-STARTPTS,format=yuv420p[a];"
             "[1:v:0]trim=end_frame=1,setpts=PTS-STARTPTS,format=yuv420p[b];[a][b]psnr")
    return [ffmpeg, "-hide_banner", "-nostdin", "-i", out,
            "-ss", f"{max(0.0, src_time - SEEK_EPS):.6f}", "-i", src,
            "-filter_complex", graph, "-f", "null", "-"]


def check_points(plan: SmartPlan, in_f: int) -> list[int]:
    """Output frame offsets worth comparing: first frame and both sides of every splice."""
    pts = {0}
    for s in plan.segments:
        a, b = s.start_f - in_f, s.end_f - in_f
        pts.update({a, max(a, b - 1)})
    return sorted(pts)


_PSNR_RE = re.compile(r"average:([0-9.]+|inf)")


def parse_psnr(text: str) -> float | None:
    m = _PSNR_RE.search(text)
    if not m:
        return None
    return 99.0 if m.group(1) == "inf" else float(m.group(1))
