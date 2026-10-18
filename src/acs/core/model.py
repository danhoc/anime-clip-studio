"""Plain data model of a project: one video, free markers and clips. JSON (de)serialisable."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any

PROJECT_SCHEMA_VERSION = 1

CLIP_COLORS = ["#4f9dff", "#ff7a59", "#5cd17b", "#c77dff", "#ffcc4d", "#3fd0d4", "#ff6fae", "#a3a3ff"]
# Standard marker palette, assigned round-robin to new markers (user can pick any colour later).
MARKER_COLORS = ["#f5c542", "#ff6b6b", "#4fd1c5", "#b388ff", "#7bd88f", "#ffa94d", "#74c0fc", "#f783ac"]
MARKER_COLOR_NAMES = ["Yellow", "Red", "Teal", "Purple", "Green", "Orange", "Blue", "Pink"]
SHORTS_ASPECT = 9 / 16   # width / height of a YouTube Shorts picture


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class CropRect:
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0

    def is_valid(self) -> bool:
        return self.w > 0 and self.h > 0

    def as_filter(self) -> str:
        return f"crop={self.w}:{self.h}:{self.x}:{self.y}"

    def as_mpv(self) -> str:
        return f"{self.w}x{self.h}+{self.x}+{self.y}"


@dataclass
class ShortsFrame:
    """9:16 frame used in Shorts Mode, relative to the picture: centre and height as fractions.
    The default is the full height, centred (see core/shorts.py for clamping and pixels)."""

    cx: float = 0.5
    cy: float = 0.5
    h: float = 1.0


@dataclass
class Marker:
    frame: int
    time: float
    label: str = ""
    color: str = "#f5c542"
    id: str = field(default_factory=new_id)


@dataclass
class Clip:
    in_frame: int
    out_frame: int | None          # exclusive; None while the clip is still "open" (only In set)
    in_time: float = 0.0
    out_time: float | None = None
    name: str = ""
    muted: bool = False
    audio_track: int | None = None  # relative audio index; None = project default
    burn_subs: bool = False
    sub_track: int | None = None    # relative subtitle index; None = project default
    sub_size: int = 100             # burned-in dialogue size, percent (Shorts Mode scales on top of it)
    crop_mode: str = "none"         # none | auto | manual
    crop: CropRect | None = None
    crop_aspect: str = "free"       # manual crop: free | source | "w:h" (see core/framing.py)
    shorts: ShortsFrame = field(default_factory=ShortsFrame)   # used for export only in Shorts Mode
    target_height: int | None = None  # None = as preset; 0 = force original
    preset_id: str | None = None      # None = project default preset
    color: str = ""
    id: str = field(default_factory=new_id)

    @property
    def is_complete(self) -> bool:
        return self.out_frame is not None and self.out_frame > self.in_frame

    @property
    def frame_count(self) -> int:
        return (self.out_frame - self.in_frame) if self.out_frame is not None else 0


@dataclass
class ViewState:
    pps: float = 0.0          # timeline pixels per second (0 = fit)
    scroll: float = 0.0       # timeline left edge in seconds
    position: float = 0.0     # playhead time
    aid: int | None = None    # preview audio (relative index)
    sid: int | None = None    # preview subtitles (relative index), None = off


@dataclass
class VideoRef:
    path: str = ""
    rel_path: str = ""
    size: int = 0
    duration: float = 0.0
    fps: str = ""
    fingerprint: str = ""


@dataclass
class Project:
    video: VideoRef = field(default_factory=VideoRef)
    markers: list[Marker] = field(default_factory=list)
    clips: list[Clip] = field(default_factory=list)
    default_preset_id: str = "x264_crf18"
    default_audio_track: int | None = None
    default_sub_track: int | None = None
    output_dir: str = ""
    name_template: str = ""
    view: ViewState = field(default_factory=ViewState)
    presets_snapshot: dict[str, dict] = field(default_factory=dict)
    marker_color_index: int = 0
    mpv_seen_ids: list[str] = field(default_factory=list)   # mpv marks already imported (never duplicated)
    mpv_offer_disabled: bool = False                          # "don't offer marks from this video again"
    version: int = PROJECT_SCHEMA_VERSION

    # ---- lookup helpers -----------------------------------------------------------------
    def clip_by_id(self, cid: str) -> Clip | None:
        return next((c for c in self.clips if c.id == cid), None)

    def marker_by_id(self, mid: str) -> Marker | None:
        return next((m for m in self.markers if m.id == mid), None)

    def next_clip_color(self) -> str:
        return CLIP_COLORS[len(self.clips) % len(CLIP_COLORS)]

    def take_marker_color(self) -> str:
        """Next palette colour, round-robin over every marker ever added to this project."""
        color = MARKER_COLORS[self.marker_color_index % len(MARKER_COLORS)]
        self.marker_color_index += 1
        return color

    def next_clip_name(self) -> str:
        used = {c.name for c in self.clips}
        i = len(self.clips) + 1
        while f"clip{i:02d}" in used:
            i += 1
        return f"clip{i:02d}"

    # ---- serialisation ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Project":
        def pick(dc, data):
            names = {f.name for f in fields(dc)}
            return {k: v for k, v in (data or {}).items() if k in names}

        clips = []
        for c in d.get("clips", []):
            c = pick(Clip, c)
            crop = c.get("crop")
            c["crop"] = CropRect(**pick(CropRect, crop)) if isinstance(crop, dict) else None
            shorts = c.get("shorts")
            c["shorts"] = ShortsFrame(**pick(ShortsFrame, shorts)) if isinstance(shorts, dict) else ShortsFrame()
            clips.append(Clip(**c))
        return cls(
            video=VideoRef(**pick(VideoRef, d.get("video"))),
            markers=[Marker(**pick(Marker, m)) for m in d.get("markers", [])],
            clips=clips,
            default_preset_id=d.get("default_preset_id", "x264_crf18"),
            default_audio_track=d.get("default_audio_track"),
            default_sub_track=d.get("default_sub_track"),
            output_dir=d.get("output_dir", ""),
            name_template=d.get("name_template", ""),
            view=ViewState(**pick(ViewState, d.get("view"))),
            presets_snapshot=d.get("presets_snapshot", {}) or {},
            marker_color_index=int(d.get("marker_color_index", len(d.get("markers", [])))),
            mpv_seen_ids=[str(x) for x in d.get("mpv_seen_ids", []) or []],
            mpv_offer_disabled=bool(d.get("mpv_offer_disabled", False)),
            version=int(d.get("version", PROJECT_SCHEMA_VERSION)),
        )
