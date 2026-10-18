"""Pure functions that turn (clip, preset, media info) into ffmpeg argument lists.

Nothing here touches the file system or spawns processes, so it is fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from acs.core.model import CropRect
from acs.core.presets import CODECS, HEIGHT_STEPS, Preset
from acs.core.probe import MediaInfo

SEEK_EPS = 0.0005  # seek slightly before the frame pts so float rounding never skips it

REASON_SUBS = "subs"
REASON_CROP = "crop"
REASON_SCALE = "scale"
REASON_SHORTS = "shorts"   # the crop is the clip's Shorts frame (Shorts Mode)


@dataclass
class SubsSource:
    """Extracted subtitle file + fonts. ffmpeg runs with cwd=workdir so names stay escape-free."""

    workdir: str
    ass_name: str
    fonts_name: str


@dataclass
class ExportSpec:
    src: str
    in_frame: int
    out_frame: int
    in_time: float
    out_time: float
    audio_rel: int | None          # None => no audio (muted or the file has none)
    preset: Preset
    output: str
    crop: CropRect | None = None
    height: int | None = None      # final downscale height (already clamped), None = keep
    subs: SubsSource | None = None
    video_kbps: int | None = None  # set for target-size mode
    hwaccel: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def frames(self) -> int:
        return self.out_frame - self.in_frame

    @property
    def duration(self) -> float:
        return max(0.0, self.out_time - self.in_time)


# ---- resolution logic ------------------------------------------------------------------------
def requested_height(clip_target: int | None, preset: Preset) -> int | None:
    """Clip override wins: None = use preset, 0 = force original."""
    if clip_target is None:
        return preset.target_height
    return None if clip_target == 0 else clip_target


def clamp_height(target: int | None, src_h: int) -> int | None:
    """Downscale only. Returns None when no scaling is needed."""
    if not target or target >= src_h:
        return None
    return max(2, int(target) // 2 * 2)


def source_dims(info: MediaInfo, crop: CropRect | None) -> tuple[int, int]:
    v = info.video
    if v is None:
        return (0, 0)
    if crop and crop.is_valid():
        return crop.w, crop.h
    return v.width, v.height


def scaled_width(h: int, src_w: int, src_h: int) -> int:
    return max(2, int(round(src_w * h / src_h / 2)) * 2)


def available_heights(src_h: int, steps=HEIGHT_STEPS) -> list[int]:
    return [h for h in steps if h < src_h]


def size_video_kbps(size_mb: float, duration: float, audio_kbps: int) -> int:
    """Video bitrate that fits `size_mb` (MiB) with a 3% container overhead margin."""
    if duration <= 0:
        return 0
    total_kbps = size_mb * 8 * 1024 * 1024 * 0.97 / duration / 1000
    return max(50, int(total_kbps - audio_kbps))


def bits_per_pixel(video_kbps: float, w: int, h: int, fps: float) -> float:
    if w <= 0 or h <= 0 or fps <= 0:
        return 0.0
    return video_kbps * 1000 / (w * h * fps)


def bpp_threshold(vcodec: str, bpp_x264: float, bpp_hevc_av1: float) -> float:
    info = CODECS.get(vcodec)
    if info is None or info.family == "h264":
        return bpp_x264
    return bpp_hevc_av1


def auto_height(src_w: int, src_h: int, fps: float, video_kbps: float, threshold: float,
                min_height: int, start_height: int | None) -> tuple[int | None, float]:
    """Pick the largest height step whose bits-per-pixel stays above `threshold`.

    Never upscales, never goes below `min_height` (unless the source itself is smaller).
    Returns (height or None for 'keep start', resulting bpp).
    """
    start = start_height or src_h
    start = min(start, src_h)
    candidates = [start] + [h for h in HEIGHT_STEPS if h < start]
    chosen = start
    for h in candidates:
        if h < min_height and h != start:
            break
        chosen = h
        w = scaled_width(h, src_w, src_h)
        if bits_per_pixel(video_kbps, w, h, fps) >= threshold:
            break
    w = scaled_width(chosen, src_w, src_h)
    bpp = bits_per_pixel(video_kbps, w, chosen, fps)
    return (None if chosen >= src_h else chosen), bpp


def reencode_reasons(burn_subs: bool, crop: CropRect | None, height: int | None) -> list[str]:
    reasons = []
    if burn_subs:
        reasons.append(REASON_SUBS)
    if crop is not None and crop.is_valid():
        reasons.append(REASON_CROP)
    if height:
        reasons.append(REASON_SCALE)
    return reasons


# ---- filters -----------------------------------------------------------------------------
def subs_filters(subs: SubsSource | None, in_time: float) -> list[str]:
    """Shift pts back to the original timeline for libass, then restore.

    The extracted track is on the app's relative timeline (ffmpeg subtracts the file's start time
    when it writes the .ass), so the offset is the relative clip start alone."""
    if subs is None:
        return []
    return [
        f"setpts=PTS+{in_time:.6f}/TB",
        f"subtitles=f={subs.ass_name}:fontsdir={subs.fonts_name}",
        "setpts=PTS-STARTPTS",
    ]


def geometry_filters(crop: CropRect | None, height: int | None, flags: str = "lanczos") -> list[str]:
    out = []
    if crop is not None and crop.is_valid():
        out.append(crop.as_filter())
    if height:
        out.append(f"scale=-2:{height}:flags={flags}")
    return out


def output_pix_fmt(vcodec: str, bit_depth: int) -> str:
    info = CODECS[vcodec]
    ten = bit_depth > 8 and info.supports_10bit
    if info.gpu:
        return "p010le" if ten else "yuv420p"
    return "yuv420p10le" if ten else "yuv420p"


def color_args(info: MediaInfo) -> list[str]:
    v = info.video
    if v is None:
        return []
    args = []
    valid = {"bt709", "bt470bg", "smpte170m", "bt2020nc", "bt2020", "smpte2084", "arib-std-b67"}
    if v.color_primaries in valid | {"bt2020"}:
        args += ["-color_primaries", v.color_primaries]
    if v.color_transfer in valid or v.color_transfer.startswith("bt2020"):
        args += ["-color_trc", v.color_transfer]
    if v.color_space in valid:
        args += ["-colorspace", v.color_space]
    if v.color_range in ("tv", "pc"):
        args += ["-color_range", v.color_range]
    return args


# ---- audio -------------------------------------------------------------------------------
def audio_args(spec: ExportSpec, info: MediaInfo, container: str) -> list[str]:
    p = spec.preset
    if spec.audio_rel is None or not info.audios:
        return ["-an"]
    rel = min(spec.audio_rel, len(info.audios) - 1)
    src = info.audios[rel]
    args = ["-map", f"0:a:{rel}"]
    if src.language:   # -map_metadata -1 drops the stream tags; the language is worth keeping
        args += ["-metadata:s:a:0", f"language={src.language}"]
    acodec = p.acodec
    if container == "webm" and acodec in ("aac", "copy") and src.codec not in ("opus", "vorbis"):
        acodec = "libopus"
        spec.warnings.append("webm_audio_opus")
    if acodec == "copy" and p.downmix_stereo and src.channels > 2:
        acodec = "aac"
    if acodec == "copy":
        if container == "mp4" and src.codec in ("flac", "truehd", "dts", "pcm_s16le", "pcm_s24le"):
            spec.warnings.append("mp4_audio_compat")
        # Input -ss does not trim stream-copied packets: without this the audio would start at the
        # keyframe before the clip (seconds of sound before the first picture in MKV).
        args += ["-c:a", "copy", "-copypriorss", "0"]
        return args
    args += ["-c:a", acodec, "-b:a", f"{p.abitrate}k"]
    if p.downmix_stereo and src.channels > 2:
        args += ["-ac", "2"]
    elif acodec == "libopus" and src.channels > 2:
        args += ["-af", "aformat=channel_layouts=7.1|5.1|stereo|mono"]
    return args


# ---- video encode ------------------------------------------------------------------------
def video_codec_args(p: Preset, pass_no: int, video_kbps: int | None, passlog: str = "passlog") -> list[str]:
    info = CODECS[p.vcodec]
    args = ["-c:v", p.vcodec]
    pix = output_pix_fmt(p.vcodec, p.bit_depth)
    args += ["-pix_fmt", pix]
    speed = p.speed or info.default_speed
    if info.speed_flag and speed:
        args += [info.speed_flag, speed]
    if p.tune and p.tune in info.tunes:
        args += ["-tune", p.tune]
    if p.vcodec == "libx264" and pix.startswith("yuv420p10"):
        args += ["-profile:v", "high10"]
    if p.vcodec == "libvpx-vp9":
        args += ["-deadline", "good", "-row-mt", "1"]
        if pix.startswith("yuv420p10"):
            args += ["-profile:v", "2"]
    if p.vcodec == "hevc_nvenc" and pix == "p010le":
        args += ["-profile:v", "main10"]
    if p.vcodec == "libaom-av1":
        args += ["-row-mt", "1"]

    if video_kbps:  # target size
        b = f"{video_kbps}k"
        if info.gpu:
            args += ["-rc", "vbr", "-multipass", "fullres", "-b:v", b,
                     "-maxrate", f"{int(video_kbps * 1.5)}k", "-bufsize", f"{video_kbps * 2}k"]
        elif info.two_pass and pass_no in (1, 2):
            args += ["-b:v", b]
            if p.vcodec == "libx265":
                args += ["-x265-params", f"pass={pass_no}:stats={passlog}.x265"]
            else:
                args += ["-pass", str(pass_no), "-passlogfile", passlog]
        else:
            args += ["-b:v", b]
    else:
        q = str(p.quality)
        if info.gpu:
            args += ["-rc", "vbr", "-cq", q, "-b:v", "0"]
        elif p.vcodec in ("libaom-av1", "libvpx-vp9"):
            args += ["-crf", q, "-b:v", "0"]
        else:
            args += [info.quality_flag, q]
    return args


def container_for(p: Preset, info: MediaInfo) -> str:
    if p.container in ("mp4", "mkv", "webm"):
        return p.container
    # "auto": keep source container family
    return "mp4" if info.is_mp4 else "mkv"


def muxer_args(container: str, vcodec_family: str | None = None) -> list[str]:
    if container == "mp4":
        args = ["-movflags", "+faststart"]
        if vcodec_family == "hevc":
            args += ["-tag:v", "hvc1"]
        return args
    if container == "webm":
        return ["-f", "webm"]
    return ["-f", "matroska"] if container == "mkv" else []


def input_args(spec: ExportSpec, hwaccel: bool | None = None) -> list[str]:
    args = ["-hide_banner", "-nostdin", "-y"]
    if hwaccel if hwaccel is not None else spec.hwaccel:
        args += ["-hwaccel", "auto"]
    args += ["-ss", f"{max(0.0, spec.in_time - SEEK_EPS):.6f}", "-i", spec.src]
    return args


def progress_args() -> list[str]:
    return ["-progress", "pipe:1", "-nostats", "-loglevel", "warning"]


def build_encode_passes(spec: ExportSpec, info: MediaInfo, ffmpeg: str = "ffmpeg") -> list[list[str]]:
    """One or two ffmpeg invocations for a (re-)encoded video clip."""
    p = spec.preset
    container = container_for(p, info)
    filters = (subs_filters(spec.subs, spec.in_time)
               + geometry_filters(spec.crop, spec.height))
    vf = ["-vf", ",".join(filters)] if filters else []
    timing = ["-t", f"{spec.duration:.6f}", "-frames:v", str(spec.frames)]
    family = CODECS[p.vcodec].family
    common_v = ["-map", "0:v:0"] + vf + color_args(info) + ["-fps_mode", "passthrough"]
    two_pass = bool(spec.video_kbps) and CODECS[p.vcodec].two_pass and not CODECS[p.vcodec].gpu
    passes: list[list[str]] = []
    if two_pass:
        first = ([ffmpeg] + input_args(spec) + common_v
                 + video_codec_args(p, 1, spec.video_kbps) + timing
                 + ["-an", "-sn", "-dn"] + progress_args() + ["-f", "null", "-"])
        passes.append(first)
    pass_no = 2 if two_pass else 0
    final = ([ffmpeg] + input_args(spec) + common_v
             + video_codec_args(p, pass_no, spec.video_kbps)
             + audio_args(spec, info, container) + ["-sn", "-dn", "-map_metadata", "-1",
                                                    "-map_chapters", "-1"]
             + timing + list(p.extra_args) + muxer_args(container, family) + progress_args()
             + [spec.output])
    passes.append(final)
    return passes
