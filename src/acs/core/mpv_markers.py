"""Marks made in the stand-alone mpv player with acs_markers.lua.

The Lua script appends JSON lines to %APPDATA%\\AnimeClipStudio\\mpv-markers.jsonl (or to a
"<video>.acsmarks.jsonl" sidecar). Records:
  {"op": "add", "id": ..., "kind": "marker"|"in"|"out", "time": seconds, "path", "filename", "size", "created"}
  {"op": "del", "target": <id>, "path", "filename", "size", "created"}
A record belongs to a video if the full path matches, or (file moved/renamed folder) the file name
and size match.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from acs.core.atomic import atomic_write_text
from acs.core.settings import AppDirs

SIDECAR_SUFFIX = ".acsmarks.jsonl"
KINDS = ("marker", "in", "out")


@dataclass(frozen=True)
class MpvMark:
    id: str
    kind: str        # marker | in | out
    time: float
    created: float = 0.0


def store_path() -> Path:
    return AppDirs.data / "mpv-markers.jsonl"


def sidecar_path(video: str) -> Path:
    return Path(video + SIDECAR_SUFFIX)


def script_source() -> Path:
    return Path(__file__).resolve().parents[1] / "mpv" / "acs_markers.lua"


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p.replace("/", "\\")))


class _Matcher:
    def __init__(self, video: str):
        self.path = _norm(video)
        self.name = os.path.basename(video).casefold()
        try:
            self.size = os.path.getsize(video)
        except OSError:
            self.size = None

    def __call__(self, rec: dict) -> bool:
        p = rec.get("path")
        if isinstance(p, str) and _norm(p) == self.path:
            return True
        name, size = rec.get("filename"), rec.get("size")
        return (isinstance(name, str) and self.size is not None and size is not None
                and name.casefold() == self.name and int(size) == self.size)


def _records(file: Path):
    try:
        with open(file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def read_marks(video: str) -> list[MpvMark]:
    """Current marks for `video` (deletions applied), sorted by time."""
    match = _Matcher(video)
    marks: dict[str, MpvMark] = {}
    for file in (store_path(), sidecar_path(video)):
        for rec in _records(file):
            if not match(rec):
                continue
            if rec.get("op") == "add" and rec.get("kind") in KINDS and isinstance(rec.get("time"), (int, float)):
                rid = str(rec.get("id") or f"{rec.get('created')}-{rec.get('time')}")
                marks[rid] = MpvMark(rid, rec["kind"], float(rec["time"]), float(rec.get("created") or 0))
            elif rec.get("op") == "del":
                marks.pop(str(rec.get("target")), None)
    return sorted(marks.values(), key=lambda m: (m.time, m.created))


def clear_marks(video: str) -> int:
    """Remove every record of `video` from the central file and delete its sidecar. Returns count removed."""
    match = _Matcher(video)
    removed = 0
    central = store_path()
    if central.exists():
        keep: list[str] = []
        try:
            with open(central, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    raw = line.rstrip("\n")
                    try:
                        rec = json.loads(raw) if raw.strip() else None
                    except ValueError:
                        rec = None
                    if isinstance(rec, dict) and match(rec):
                        removed += 1
                    elif raw.strip():
                        keep.append(raw)
        except OSError:
            keep = []
        atomic_write_text(central, "".join(k + "\n" for k in keep))
    side = sidecar_path(video)
    if side.exists():
        removed += sum(1 for _ in _records(side))
        try:
            side.unlink()
        except OSError:
            pass
    return removed


def split_marks(marks: list[MpvMark]) -> tuple[list[MpvMark], list[tuple[MpvMark, MpvMark]], list[MpvMark]]:
    """(plain markers, clip (in, out) pairs, unpaired in/out marks).

    Each clip start is paired with the next clip end after it; a start followed by another start
    before any end stays unpaired (imported as a plain marker so nothing is lost).
    """
    markers = [m for m in marks if m.kind == "marker"]
    edges = sorted((m for m in marks if m.kind in ("in", "out")), key=lambda m: (m.time, m.created))
    clips: list[tuple[MpvMark, MpvMark]] = []
    unpaired: list[MpvMark] = []
    pending: MpvMark | None = None
    for m in edges:
        if m.kind == "in":
            if pending is not None:
                unpaired.append(pending)
            pending = m
        else:
            if pending is not None and m.time > pending.time:
                clips.append((pending, m))
                pending = None
            else:
                unpaired.append(m)
    if pending is not None:
        unpaired.append(pending)
    return markers, clips, unpaired


def ensure_store_dir() -> None:
    """The Lua script cannot create folders; make sure the central file's folder exists."""
    store_path().parent.mkdir(parents=True, exist_ok=True)
