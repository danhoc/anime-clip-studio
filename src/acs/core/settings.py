"""Application directories and persistent settings (JSON in %APPDATA%\\AnimeClipStudio)."""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from platformdirs import user_cache_dir, user_data_dir

from acs import APP_NAME
from acs.core.atomic import atomic_write_json, read_json

DEFAULT_VIDEO_EXTS = ["mkv", "mp4", "m4v", "avi", "mov", "webm", "ts", "m2ts", "wmv", "flv"]
# Arrow-key steps: "<n>f" = n frames, "<x>s" = x seconds, "key" = to the next / previous keyframe.
DEFAULT_STEPS = {"arrow": "1s", "shift": "1f", "ctrl": "key"}


def parse_step(spec: str, default: str = "1f") -> tuple[str, float]:
    """'24f' -> ('frames', 24), '1.5s' -> ('seconds', 1.5), 'key' -> ('keyframe', 1); bad specs use `default`."""
    for s in (str(spec).strip().lower(), default):
        try:
            if s == "key":
                return "keyframe", 1.0
            if s.endswith("f") and int(s[:-1]) >= 1:
                return "frames", float(int(s[:-1]))
            if s.endswith("s") and float(s[:-1]) > 0:
                return "seconds", float(s[:-1])
        except ValueError:
            pass
    return "frames", 1.0


def step_spec(kind: str, value: float) -> str:
    if kind == "keyframe":
        return "key"
    if kind == "seconds":
        return f"{value:g}s"
    return f"{max(1, round(value))}f"


class AppDirs:
    """All writable locations. Overridable for tests via `AppDirs.configure(root)`."""

    data: Path = Path(user_data_dir(APP_NAME, appauthor=False, roaming=True))
    cache: Path = Path(user_cache_dir(APP_NAME, appauthor=False))

    @classmethod
    def configure(cls, root: Path) -> None:
        cls.data = Path(root) / "data"
        cls.cache = Path(root) / "cache"

    @classmethod
    def settings_file(cls) -> Path:
        return cls.data / "settings.json"

    @classmethod
    def presets_file(cls) -> Path:
        return cls.data / "presets.json"

    @classmethod
    def autosave_dir(cls) -> Path:
        return cls.data / "autosave"

    @classmethod
    def recovery_dir(cls) -> Path:
        return cls.data / "recovery"

    @classmethod
    def logs_dir(cls) -> Path:
        return cls.data / "logs"

    @classmethod
    def work_dir(cls) -> Path:
        return cls.cache / "work"

    @classmethod
    def subs_cache(cls) -> Path:
        return cls.cache / "subs"

    @classmethod
    def media_cache(cls) -> Path:
        return cls.cache / "media"


def app_root() -> Path:
    """Folder containing the app (project root when run from source, exe dir when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parents[3]


def _find_tool(name: str) -> str:
    """First candidate from well-known locations (no process is started here)."""
    from acs.core.tools import find_ff_candidates
    cands = find_ff_candidates(name, app_root())
    return str(cands[0][0]) if cands else ""


def _default_libmpv() -> str:
    from acs.core.tools import find_libmpv_candidates
    cands = find_libmpv_candidates(app_root())
    return str(cands[0][0]) if cands else ""


@dataclass
class AppSettings:
    ffmpeg: str = ""
    ffprobe: str = ""
    libmpv: str = ""
    mpv_player: str = ""            # optional stand-alone mpv.exe (marking while watching)
    mpv_script_keys: dict = field(default_factory=dict)   # action -> mpv key name for acs_markers.lua
    output_dir: str = ""
    language: str = "en"
    ui_scale: str = "auto"          # auto | 1.0 | 1.25 | 1.5 | 1.75 | 2.0 (applied at start)
    theme: str = "dark"             # dark | light | system (applied at start)
    thumb_quality: str = "auto"     # auto | 72 | 108 | 144 | 216  (thumbnail height in pixels)
    wave_quality: str = "auto"      # auto | 100 | 200 | 400 | 800 (waveform points per second)
    preferred_audio_langs: list[str] = field(default_factory=lambda: ["jpn", "ja"])
    preferred_sub_langs: list[str] = field(default_factory=lambda: ["eng", "en"])
    hwdec: bool = True
    parallel_cpu: int = 1
    parallel_gpu: int = 2
    bpp_x264: float = 0.06
    bpp_hevc_av1: float = 0.035
    min_auto_height: int = 480
    video_exts: list[str] = field(default_factory=lambda: list(DEFAULT_VIDEO_EXTS))
    library_folders: list[str] = field(default_factory=list)
    recent_projects: list[str] = field(default_factory=list)
    show_tour: bool = True
    name_template: str = "{video}_{clip}_{in}-{out}"
    frames_template: str = "{video}_{time}_{frame}"
    default_preset_id: str = "x264_crf18"
    step_arrow: str = DEFAULT_STEPS["arrow"]     # ← / →
    step_shift: str = DEFAULT_STEPS["shift"]     # Shift + ← / →
    step_ctrl: str = DEFAULT_STEPS["ctrl"]       # Ctrl + ← / →
    show_quick_help: bool = True                 # the short help window at every start
    shorts_mode: bool = False                    # Tools → Shorts Mode: export each clip's 9:16 frame
    window_geometry: str = ""
    window_state: str = ""
    splitter_sizes: dict = field(default_factory=dict)
    last_open_dir: str = ""
    frames_last: dict = field(default_factory=dict)

    def resolved(self) -> "AppSettings":
        if not self.ffmpeg:
            self.ffmpeg = _find_tool("ffmpeg")
        if not self.ffprobe:
            self.ffprobe = _find_tool("ffprobe")
        if not self.libmpv:
            self.libmpv = _default_libmpv()
        if not self.output_dir:
            self.output_dir = str(Path.home() / "Videos" / "AnimeClips")
        return self

    def save(self) -> None:
        atomic_write_json(AppDirs.settings_file(), asdict(self))

    @classmethod
    def load(cls) -> "AppSettings":
        raw = read_json(AppDirs.settings_file(), {}) or {}
        known = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in raw.items() if k in known})
        if s.preferred_sub_langs == ["rus", "ru", "eng", "en"]:  # old default -> English first
            s.preferred_sub_langs = ["eng", "en"]
        return s.resolved()

    def add_recent(self, path: str, limit: int = 10) -> None:
        p = str(path)
        self.recent_projects = [p] + [x for x in self.recent_projects if x != p]
        del self.recent_projects[limit:]
