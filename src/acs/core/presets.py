"""Export presets: built-in set + user presets stored in %APPDATA%\\AnimeClipStudio\\presets.json."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace

from acs.core.atomic import atomic_write_json, read_json
from acs.core.model import new_id
from acs.core.settings import AppDirs


@dataclass(frozen=True)
class CodecInfo:
    family: str           # h264 | hevc | av1 | vp9 | vvc
    gpu: bool
    supports_10bit: bool
    supports_8bit: bool
    speed_flag: str       # ffmpeg option for speed preset
    speeds: tuple[str, ...]
    default_speed: str
    quality_flag: str     # -crf / -cq / -qp
    quality_range: tuple[int, int]
    default_quality: int
    two_pass: bool        # supports -pass 1/2 style multi-pass in this app
    containers: tuple[str, ...]
    tunes: tuple[str, ...] = ()


CODECS: dict[str, CodecInfo] = {
    "libx264": CodecInfo("h264", False, True, True, "-preset",
                         ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"),
                         "slow", "-crf", (0, 51), 18, True, ("mp4", "mkv"),
                         ("animation", "film", "grain", "stillimage")),
    "libx265": CodecInfo("hevc", False, True, True, "-preset",
                         ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"),
                         "slow", "-crf", (0, 51), 18, True, ("mp4", "mkv"), ("animation", "grain")),
    "libsvtav1": CodecInfo("av1", False, True, True, "-preset", tuple(str(i) for i in range(0, 14)),
                           "5", "-crf", (1, 63), 28, False, ("mp4", "mkv", "webm")),
    "libaom-av1": CodecInfo("av1", False, True, True, "-cpu-used", tuple(str(i) for i in range(0, 9)),
                            "4", "-crf", (0, 63), 26, True, ("mp4", "mkv", "webm")),
    "librav1e": CodecInfo("av1", False, True, True, "-speed", tuple(str(i) for i in range(0, 11)),
                          "6", "-qp", (0, 255), 80, False, ("mp4", "mkv", "webm")),
    "libvpx-vp9": CodecInfo("vp9", False, True, True, "-cpu-used", tuple(str(i) for i in range(0, 6)),
                            "2", "-crf", (0, 63), 30, True, ("webm", "mkv", "mp4")),
    "libvvenc": CodecInfo("vvc", False, True, False, "-preset", ("faster", "fast", "medium", "slow", "slower"),
                          "medium", "-qp", (0, 63), 30, False, ("mp4", "mkv")),
    "h264_nvenc": CodecInfo("h264", True, False, True, "-preset", tuple(f"p{i}" for i in range(1, 8)),
                            "p7", "-cq", (0, 51), 21, True, ("mp4", "mkv")),
    "hevc_nvenc": CodecInfo("hevc", True, True, True, "-preset", tuple(f"p{i}" for i in range(1, 8)),
                            "p7", "-cq", (0, 51), 22, True, ("mp4", "mkv")),
    "av1_nvenc": CodecInfo("av1", True, True, True, "-preset", tuple(f"p{i}" for i in range(1, 8)),
                           "p7", "-cq", (0, 63), 28, True, ("mp4", "mkv", "webm")),
}

AUDIO_CODECS = ("aac", "libopus", "copy")
CONTAINERS = ("mp4", "mkv", "webm")
HEIGHT_STEPS = (2160, 1440, 1080, 900, 720, 576, 480, 360)
ANIM_HEIGHT_STEPS = (1080, 720, 540, 480, 360, 270, 240, 180)
GIF_DITHERS = ("sierra2_4a", "bayer:2", "bayer:3", "bayer:4", "bayer:5", "none")
ANIM_FPS_STEPS = (30, 25, 24, 20, 18, 15, 12, 10, 8)
GIF_COLOR_STEPS = (256, 192, 128, 96, 64, 48, 32, 16)
WEBP_QUALITY_STEPS = (95, 90, 85, 80, 75, 70, 60, 50, 40, 30, 20)


@dataclass
class Preset:
    id: str
    name: str
    kind: str = "video"           # video | gif | webp
    builtin: bool = False
    # ---- video ----
    mode: str = "encode"          # smartcut | encode
    vcodec: str = "libx264"
    bit_depth: int = 8
    quality: int = 18             # CRF / CQ / QP depending on codec
    speed: str = ""
    tune: str = ""
    container: str = "mp4"
    acodec: str = "aac"
    abitrate: int = 192
    downmix_stereo: bool = False
    target_height: int | None = None
    target_size_mb: float | None = None
    allow_auto_downscale: bool = False
    min_auto_height: int = 480
    extra_args: list[str] = field(default_factory=list)
    # ---- gif / webp ----
    fps: int | None = None        # None = source fps capped to 30
    colors: int = 256
    dither: str = "sierra2_4a"
    webp_quality: int = 85
    lossless: bool = False
    compression_level: int = 4
    loop: bool = True
    fit_size_mb: float | None = None
    locks: dict[str, bool] = field(default_factory=dict)     # height/fps/colors/dither/quality
    mins: dict[str, float] = field(default_factory=dict)

    @property
    def codec(self) -> CodecInfo | None:
        return CODECS.get(self.vcodec)

    @property
    def ext(self) -> str:
        if self.kind == "gif":
            return "gif"
        if self.kind == "webp":
            return "webp"
        return self.container

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Preset":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def copy_as_user(self, name: str | None = None) -> "Preset":
        return replace(self, id=new_id(), name=name or f"{self.name} (copy)", builtin=False,
                       locks=dict(self.locks), mins=dict(self.mins), extra_args=list(self.extra_args))


def builtin_presets() -> list[Preset]:
    P = Preset
    return [
        P("orig_smartcut", "Original (smart cut, lossless middle)", builtin=True, mode="smartcut",
          vcodec="copy", container="auto", acodec="copy"),
        P("x264_crf18", "H.264 x264 CRF 18 (max compatibility)", builtin=True, vcodec="libx264",
          bit_depth=8, quality=18, speed="slow", tune="animation"),
        P("x265_10_crf18", "HEVC x265 10-bit CRF 18", builtin=True, vcodec="libx265", bit_depth=10,
          quality=18, speed="slow", tune="animation"),
        P("svtav1_10_crf28", "AV1 SVT-AV1 10-bit CRF 28", builtin=True, vcodec="libsvtav1", bit_depth=10,
          quality=28, speed="5", acodec="libopus", abitrate=160),
        P("aom_10_crf26", "AV1 libaom 10-bit (best, slow)", builtin=True, vcodec="libaom-av1", bit_depth=10,
          quality=26, speed="4", acodec="libopus", abitrate=160),
        P("hevc_nvenc_10", "HEVC NVENC 10-bit (fast, GPU)", builtin=True, vcodec="hevc_nvenc", bit_depth=10,
          quality=22, speed="p7"),
        P("av1_nvenc_10", "AV1 NVENC 10-bit (fast, GPU)", builtin=True, vcodec="av1_nvenc", bit_depth=10,
          quality=28, speed="p7", acodec="libopus", abitrate=160),
        P("vp9_webm", "VP9 WebM CRF 30", builtin=True, vcodec="libvpx-vp9", bit_depth=8, quality=30,
          speed="2", container="webm", acodec="libopus", abitrate=160),
        P("vvc_exp", "VVC H.266 libvvenc (experimental)", builtin=True, vcodec="libvvenc", bit_depth=10,
          quality=30, speed="medium", container="mp4"),
        P("discord_10mb", "Discord 10 MB (x264, 2-pass)", builtin=True, vcodec="libx264", bit_depth=8,
          quality=20, speed="slow", tune="animation", target_size_mb=10, allow_auto_downscale=True,
          min_auto_height=480, abitrate=128, downmix_stereo=True),
        P("discord_50mb", "Discord 50 MB (x264, 2-pass)", builtin=True, vcodec="libx264", bit_depth=8,
          quality=18, speed="slow", tune="animation", target_size_mb=50, allow_auto_downscale=True,
          min_auto_height=720, abitrate=160, downmix_stereo=True),
        P("gif_low", "GIF Low (360p 12fps 64c)", kind="gif", builtin=True, target_height=360, fps=12,
          colors=64, dither="bayer:3"),
        P("gif_medium", "GIF Medium (480p 15fps 128c)", kind="gif", builtin=True, target_height=480, fps=15,
          colors=128, dither="bayer:4"),
        P("gif_high", "GIF High (720p 20fps 256c)", kind="gif", builtin=True, target_height=720, fps=20,
          colors=256, dither="sierra2_4a"),
        P("gif_max", "GIF Max (source res, up to 30fps)", kind="gif", builtin=True, target_height=None,
          fps=None, colors=256, dither="sierra2_4a"),
        P("gif_fit_8mb", "GIF fit 8 MB (auto)", kind="gif", builtin=True, target_height=480, fps=15,
          colors=256, dither="sierra2_4a", fit_size_mb=8,
          mins={"height": 270, "fps": 10, "colors": 64}),
        P("webp_low", "Animated WebP Low (360p q50)", kind="webp", builtin=True, target_height=360, fps=12,
          webp_quality=50),
        P("webp_medium", "Animated WebP Medium (480p q70)", kind="webp", builtin=True, target_height=480,
          fps=15, webp_quality=70),
        P("webp_high", "Animated WebP High (720p q85)", kind="webp", builtin=True, target_height=720, fps=20,
          webp_quality=85),
        P("webp_max", "Animated WebP Max (source, q95)", kind="webp", builtin=True, target_height=None,
          fps=None, webp_quality=95),
    ]


class PresetStore:
    def __init__(self) -> None:
        self.builtins = builtin_presets()
        self.user: list[Preset] = []
        self.load()

    def load(self) -> None:
        raw = read_json(AppDirs.presets_file(), {}) or {}
        self.user = []
        for d in raw.get("presets", []):
            try:
                p = Preset.from_dict(d)
                p.builtin = False
                self.user.append(p)
            except TypeError:
                continue

    def save(self) -> None:
        atomic_write_json(AppDirs.presets_file(), {"presets": [p.to_dict() for p in self.user]})

    def all(self) -> list[Preset]:
        return self.builtins + self.user

    def get(self, pid: str | None, snapshot: dict[str, dict] | None = None) -> Preset | None:
        if pid is None:
            return None
        for p in self.all():
            if p.id == pid:
                return p
        if snapshot and pid in snapshot:
            return Preset.from_dict(snapshot[pid])
        return None

    def upsert(self, preset: Preset) -> None:
        preset.builtin = False
        for i, p in enumerate(self.user):
            if p.id == preset.id:
                self.user[i] = preset
                break
        else:
            self.user.append(preset)
        self.save()

    def delete(self, pid: str) -> None:
        self.user = [p for p in self.user if p.id != pid]
        self.save()
