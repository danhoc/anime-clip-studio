"""Saving/loading .acsproj files, autosave paths and relinking moved videos."""

from __future__ import annotations

import json
import os
from fractions import Fraction
from pathlib import Path

from acs.core.atomic import atomic_write_json
from acs.core.model import Project
from acs.core.settings import AppDirs
from acs.core.util import file_fingerprint, norm_path_key, sha1_text

PROJECT_EXT = ".acsproj"


class ProjectLoadError(RuntimeError):
    pass


def save_project(project: Project, path: str | Path, backup: bool = True) -> None:
    path = Path(path)
    if project.video.path:
        try:
            project.video.rel_path = os.path.relpath(project.video.path, path.parent)
        except ValueError:  # different drive
            project.video.rel_path = ""
    atomic_write_json(path, project.to_dict(), backup=backup)


def load_project(path: str | Path) -> Project:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ProjectLoadError(str(e)) from e
    if not isinstance(data, dict) or "video" not in data:
        raise ProjectLoadError("not an Anime Clip Studio project")
    return Project.from_dict(data)


def video_key(video_path: str) -> str:
    return sha1_text(norm_path_key(video_path))[:16]


def autosave_path(video_path: str) -> Path:
    return AppDirs.autosave_dir() / f"{video_key(video_path)}{PROJECT_EXT}"


def write_autosave(project: Project) -> Path | None:
    if not project.video.path:
        return None
    p = autosave_path(project.video.path)
    atomic_write_json(p, project.to_dict())
    return p


def find_autosave(video_path: str) -> Path | None:
    p = autosave_path(video_path)
    return p if p.exists() else None


def remove_autosave(video_path: str) -> None:
    try:
        autosave_path(video_path).unlink()
    except OSError:
        pass


def resolve_video(project: Project, project_path: str | Path | None) -> tuple[str | None, str]:
    """Locate the project's video.

    Returns (path or None, status) where status is one of
    'ok', 'relative', 'fingerprint', 'missing', 'mismatch'.
    """
    ref = project.video
    candidates: list[tuple[str, str]] = []
    if ref.path:
        candidates.append((ref.path, "ok"))
    if project_path and ref.rel_path:
        candidates.append((str((Path(project_path).parent / ref.rel_path).resolve()), "relative"))
    for cand, status in candidates:
        if Path(cand).is_file():
            if ref.fingerprint and ref.size and Path(cand).stat().st_size != ref.size:
                return cand, "mismatch"
            return cand, status
    # Same fingerprint somewhere next to the original or the project file.
    search_dirs = []
    if ref.path:
        search_dirs.append(Path(ref.path).parent)
    if project_path:
        search_dirs.append(Path(project_path).parent)
    if ref.fingerprint and ref.size:
        for d in search_dirs:
            if not d.is_dir():
                continue
            for f in d.iterdir():
                try:
                    if f.is_file() and f.stat().st_size == ref.size and file_fingerprint(f) == ref.fingerprint:
                        return str(f), "fingerprint"
                except OSError:
                    continue
    return None, "missing"


def remap_frames(project: Project, new_fps: Fraction, time_to_frame) -> bool:
    """If the stored fps differs from the video's, recompute frame numbers from stored times."""
    try:
        old = Fraction(project.video.fps) if project.video.fps else None
    except (ValueError, ZeroDivisionError):
        old = None
    if old is not None and old == new_fps:
        return False
    for m in project.markers:
        m.frame = time_to_frame(m.time)
    for c in project.clips:
        c.in_frame = time_to_frame(c.in_time)
        if c.out_time is not None:
            c.out_frame = max(c.in_frame + 1, time_to_frame(c.out_time))
    project.video.fps = str(new_fps)
    return True
