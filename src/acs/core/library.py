"""Media library: recursive folder scan, natural sorting, episode neighbours, metadata cache."""

from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from acs.core.atomic import atomic_write_json, read_json
from acs.core.probe import ProbeError, probe
from acs.core.settings import AppDirs
from acs.core.util import natural_key


@dataclass
class FolderNode:
    path: str
    name: str
    folders: list["FolderNode"] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    def count(self) -> int:
        return len(self.files) + sum(f.count() for f in self.folders)


MIN_VIDEO_BYTES = 1024 * 1024  # skips e.g. TypeScript ".ts" sources that share MPEG-TS's extension


def scan_folder(root: str, exts: list[str], cancel=None, min_size: int = MIN_VIDEO_BYTES) -> FolderNode:
    extset = {"." + e.lower().lstrip(".") for e in exts}

    def walk(path: Path) -> FolderNode | None:
        node = FolderNode(str(path), path.name or str(path))
        try:
            entries = list(os.scandir(path))
        except OSError:
            return None
        for e in sorted(entries, key=lambda e: natural_key(e.name)):
            if cancel is not None and cancel():
                break
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name.startswith(".") or e.name.startswith("$"):
                        continue
                    child = walk(Path(e.path))
                    if child is not None and child.count():
                        node.folders.append(child)
                elif (e.is_file() and os.path.splitext(e.name)[1].lower() in extset
                      and e.stat().st_size >= min_size):
                    node.files.append(e.path)
            except OSError:
                continue
        return node

    return walk(Path(root)) or FolderNode(root, Path(root).name)


def episode_neighbours(path: str, exts: list[str]) -> tuple[str | None, str | None]:
    """Previous/next video in the same folder by natural sort order."""
    p = Path(path)
    extset = {"." + e.lower().lstrip(".") for e in exts}
    try:
        files = sorted((f for f in p.parent.iterdir() if f.is_file() and f.suffix.lower() in extset),
                       key=lambda f: natural_key(f.name))
    except OSError:
        return None, None
    names = [os.path.normcase(str(f)) for f in files]
    try:
        i = names.index(os.path.normcase(str(p)))
    except ValueError:
        return None, None
    prev = str(files[i - 1]) if i > 0 else None
    nxt = str(files[i + 1]) if i + 1 < len(files) else None
    return prev, nxt


@dataclass
class FileMeta:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    codec: str = ""
    bit_depth: int = 8
    audio: int = 0
    subs: int = 0
    error: str = ""

    def resolution(self) -> str:
        return f"{self.width}x{self.height}" if self.width else ""


class MetaCache:
    """ffprobe summaries keyed by path|size|mtime, persisted in the cache folder."""

    def __init__(self) -> None:
        self._path = AppDirs.media_cache() / "library_meta.json"
        raw = read_json(self._path, {}) or {}
        self._data: dict[str, dict] = raw if isinstance(raw, dict) else {}
        self._lock = threading.Lock()
        self._dirty = 0

    @staticmethod
    def key(path: str) -> str | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        return f"{os.path.normcase(path)}|{st.st_size}|{int(st.st_mtime)}"

    def get(self, path: str) -> FileMeta | None:
        k = self.key(path)
        if k is None:
            return None
        with self._lock:
            d = self._data.get(k)
        return FileMeta(**d) if d else None

    def fetch(self, path: str, ffprobe: str) -> FileMeta:
        cached = self.get(path)
        if cached is not None:
            return cached
        try:
            info = probe(path, ffprobe, timeout=30)
            v = info.video
            meta = FileMeta(duration=info.duration, width=v.width if v else 0, height=v.height if v else 0,
                            codec=v.codec if v else "", bit_depth=v.bit_depth if v else 8,
                            audio=len(info.audios), subs=len(info.subs))
        except (ProbeError, OSError, Exception) as e:  # noqa: BLE001 - shown in the tree as an error
            meta = FileMeta(error=str(e)[:200])
        k = self.key(path)
        if k is not None:
            with self._lock:
                self._data[k] = asdict(meta)
                self._dirty += 1
        return meta

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            data = dict(self._data)
            self._dirty = 0
        try:
            atomic_write_json(self._path, data)
        except OSError:
            pass
