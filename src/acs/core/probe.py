"""ffprobe wrappers: stream info and the packet scan used for keyframes and the exact frame index.

All times handled by the app are *relative* times: stream pts minus the container start_time.
This matches both mpv (rebase-start-time=yes) and ffmpeg input seeking (-ss before -i).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from acs.core.util import run_capture, spawn

BITMAP_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}


@dataclass
class VideoStream:
    index: int
    codec: str
    profile: str
    pix_fmt: str
    width: int
    height: int
    sar: str
    fps: Fraction
    start_time: float
    color_space: str
    color_primaries: str
    color_transfer: str
    color_range: str
    bit_depth: int

    @property
    def is_10bit(self) -> bool:
        return self.bit_depth > 8


@dataclass
class AudioStream:
    index: int
    rel: int
    codec: str
    channels: int
    sample_rate: int
    language: str
    title: str
    bitrate: int

    def label(self) -> str:
        parts = [f"#{self.rel + 1}"]
        if self.language:
            parts.append(self.language)
        parts.append(self.codec)
        parts.append(f"{self.channels}ch")
        if self.title:
            parts.append(f"— {self.title}")
        return " ".join(parts)


@dataclass
class SubStream:
    index: int
    rel: int
    codec: str
    language: str
    title: str
    forced: bool = False      # disposition "forced" (signs-only tracks)

    @property
    def is_bitmap(self) -> bool:
        return self.codec in BITMAP_SUB_CODECS

    def label(self) -> str:
        parts = [f"#{self.rel + 1}"]
        if self.language:
            parts.append(self.language)
        parts.append(self.codec)
        if self.title:
            parts.append(f"— {self.title}")
        return " ".join(parts)


@dataclass
class MediaInfo:
    path: str
    duration: float
    format_name: str
    size: int
    bitrate: int
    start_time: float
    video: VideoStream | None
    audios: list[AudioStream] = field(default_factory=list)
    subs: list[SubStream] = field(default_factory=list)
    attachments: int = 0

    @property
    def fps(self) -> Fraction:
        return self.video.fps if self.video else Fraction(24000, 1001)

    @property
    def is_matroska(self) -> bool:
        return "matroska" in self.format_name

    @property
    def is_mp4(self) -> bool:
        return "mp4" in self.format_name or "mov" in self.format_name

    def summary(self) -> str:
        v = self.video
        if not v:
            return "no video"
        depth = "10-bit" if v.is_10bit else "8-bit"
        return f"{v.width}x{v.height} {v.codec} {depth} {float(v.fps):.3f} fps"


class ProbeError(RuntimeError):
    pass


def _fraction(s: str | None, default: Fraction | None = None) -> Fraction | None:
    try:
        if not s or s in ("0/0", "N/A"):
            return default
        f = Fraction(s)
        return f if f > 0 else default
    except (ValueError, ZeroDivisionError):
        return default


def _float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _bit_depth(st: dict) -> int:
    raw = st.get("bits_per_raw_sample")
    if raw and str(raw).isdigit():
        return int(raw)
    pf = st.get("pix_fmt", "") or ""
    for d in (16, 12, 10):
        if f"p{d}" in pf or pf.endswith(f"{d}le") or pf.endswith(f"{d}be"):
            return d
    return 8


def parse_probe_json(path: str, data: dict) -> MediaInfo:
    fmt = data.get("format", {})
    start = _float(fmt.get("start_time"), 0.0)
    video = None
    audios: list[AudioStream] = []
    subs: list[SubStream] = []
    attachments = 0
    for st in data.get("streams", []):
        ctype = st.get("codec_type")
        tags = {k.lower(): v for k, v in (st.get("tags") or {}).items()}
        disp = st.get("disposition") or {}
        if ctype == "video" and video is None and not disp.get("attached_pic"):
            fps = (_fraction(st.get("avg_frame_rate")) or _fraction(st.get("r_frame_rate"))
                   or Fraction(24000, 1001))
            # Prefer r_frame_rate when avg is a weird approximation of a common CFR value.
            r = _fraction(st.get("r_frame_rate"))
            if r and abs(float(r) - float(fps)) < 0.01:
                fps = r
            video = VideoStream(
                index=int(st["index"]),
                codec=st.get("codec_name", "?"),
                profile=st.get("profile", "") or "",
                pix_fmt=st.get("pix_fmt", "") or "",
                width=int(st.get("width") or 0),
                height=int(st.get("height") or 0),
                sar=st.get("sample_aspect_ratio", "1:1") or "1:1",
                fps=fps,
                start_time=_float(st.get("start_time"), start) - start,
                color_space=st.get("color_space", "") or "",
                color_primaries=st.get("color_primaries", "") or "",
                color_transfer=st.get("color_transfer", "") or "",
                color_range=st.get("color_range", "") or "",
                bit_depth=_bit_depth(st),
            )
        elif ctype == "audio":
            audios.append(AudioStream(
                index=int(st["index"]), rel=len(audios), codec=st.get("codec_name", "?"),
                channels=int(st.get("channels") or 0), sample_rate=int(st.get("sample_rate") or 0),
                language=tags.get("language", ""), title=tags.get("title", ""),
                bitrate=int(_float(st.get("bit_rate"), 0)),
            ))
        elif ctype == "subtitle":
            subs.append(SubStream(
                index=int(st["index"]), rel=len(subs), codec=st.get("codec_name", "?"),
                language=tags.get("language", ""), title=tags.get("title", ""),
                forced=bool(disp.get("forced")),
            ))
        elif ctype == "attachment":
            attachments += 1
    return MediaInfo(
        path=path,
        duration=_float(fmt.get("duration"), 0.0),
        format_name=fmt.get("format_name", ""),
        size=int(_float(fmt.get("size"), 0)),
        bitrate=int(_float(fmt.get("bit_rate"), 0)),
        start_time=start,
        video=video, audios=audios, subs=subs, attachments=attachments,
    )


def probe(path: str | Path, ffprobe: str = "ffprobe", timeout: float = 60) -> MediaInfo:
    args = [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
    res = run_capture(args, timeout=timeout)
    if res.returncode != 0:
        raise ProbeError(res.stderr.strip() or f"ffprobe failed ({res.returncode})")
    try:
        data = json.loads(res.stdout)
    except ValueError as e:
        raise ProbeError(f"bad ffprobe output: {e}") from e
    return parse_probe_json(str(path), data)


@dataclass
class PacketScan:
    """Video packets in *decode order*: relative pts (seconds) and keyframe flags."""

    pts: list[float]
    key: list[bool]


def parse_packet_csv(text: str, start_time: float) -> PacketScan:
    pts: list[float] = []
    key: list[bool] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        p, d, flags = parts[0], parts[1], parts[2]
        t = p if p not in ("", "N/A") else d
        if t in ("", "N/A"):
            continue
        try:
            pts.append(float(t) - start_time)
        except ValueError:
            continue
        key.append("K" in flags)
    return PacketScan(pts, key)


def scan_packets(path: str | Path, start_time: float, ffprobe: str = "ffprobe",
                 cancel=None) -> PacketScan:
    """Read every video packet header (no decoding). ~1-3 s for a 24 min episode."""
    args = [ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,dts_time,flags", "-of", "csv=p=0", str(path)]
    proc = spawn(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                 encoding="utf-8", errors="replace")
    chunks: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        chunks.append(line)
        if cancel is not None and cancel():
            proc.kill()
            break
    proc.wait()
    proc.stdout.close()
    return parse_packet_csv("".join(chunks), start_time)
