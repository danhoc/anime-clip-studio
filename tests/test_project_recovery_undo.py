import json
import os
from fractions import Fraction

import pytest

from acs.core import recovery
from acs.core.atomic import atomic_write_text
from acs.core.model import Clip, CropRect, Marker, Project
from acs.core.project_io import load_project, remap_frames, resolve_video, save_project
from acs.core.timebase import FrameIndex
from acs.core.util import file_fingerprint


def sample_project(video: str) -> Project:
    p = Project()
    p.video.path = video
    p.video.size = os.path.getsize(video)
    p.video.fingerprint = file_fingerprint(video)
    p.video.fps = "24000/1001"
    p.markers.append(Marker(frame=24, time=1.001, label="a"))
    p.clips.append(Clip(in_frame=10, out_frame=50, in_time=0.417, out_time=2.085, name="c1", muted=True,
                        crop_mode="manual", crop=CropRect(0, 10, 100, 80), target_height=480))
    p.presets_snapshot = {"u1": {"id": "u1", "name": "mine"}}
    return p


def test_save_load_roundtrip(tmp_path):
    video = tmp_path / "v.mkv"
    video.write_bytes(b"0" * 1000)
    p = sample_project(str(video))
    path = tmp_path / "proj.acsproj"
    save_project(p, path)
    q = load_project(path)
    assert q.to_dict() == p.to_dict()
    assert q.clips[0].crop == CropRect(0, 10, 100, 80)
    save_project(q, path)
    assert (tmp_path / "proj.acsproj.bak").exists()


def test_load_without_preset_snapshot(tmp_path):
    path = tmp_path / "old.acsproj"
    path.write_text(json.dumps({"video": {"path": "x.mkv"}, "clips": [{"in_frame": 1, "out_frame": 5}]}))
    q = load_project(path)
    assert q.presets_snapshot == {} and q.clips[0].out_frame == 5


def test_relink_relative_and_fingerprint(tmp_path):
    video = tmp_path / "a" / "v.mkv"
    video.parent.mkdir()
    video.write_bytes(os.urandom(5000))
    p = sample_project(str(video))
    proj = tmp_path / "a" / "p.acsproj"
    save_project(p, proj)
    # move project + video together -> relative path works
    moved = tmp_path / "b"
    os.rename(tmp_path / "a", moved)
    q = load_project(moved / "p.acsproj")
    path, status = resolve_video(q, moved / "p.acsproj")
    assert status == "relative" and os.path.samefile(path, moved / "v.mkv")
    # renamed video -> fingerprint search in the same folder
    os.rename(moved / "v.mkv", moved / "renamed.mkv")
    path, status = resolve_video(q, moved / "p.acsproj")
    assert status == "fingerprint" and path.endswith("renamed.mkv")


def test_remap_frames_from_times():
    p = Project()
    p.video.fps = "25"
    p.markers.append(Marker(frame=25, time=1.0))
    p.clips.append(Clip(in_frame=25, out_frame=50, in_time=1.0, out_time=2.0))
    fi = FrameIndex.cfr(Fraction(24000, 1001), 10)
    assert remap_frames(p, Fraction(24000, 1001), fi.nearest_frame)
    assert p.markers[0].frame == 24 and p.clips[0].out_frame == 48


def test_atomic_write_keeps_old_file_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "f.json"
    atomic_write_text(target, "old")

    def boom(*a, **k):
        raise OSError("disk died")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "new")
    assert target.read_text() == "old"
    assert not [f for f in tmp_path.iterdir() if f.name.endswith(".tmp")]


def test_snapshot_rotation_and_listing(tmp_path):
    p = Project()
    p.video.path = str(tmp_path / "v.mkv")
    for _ in range(13):
        recovery.snapshot(p, None, "before-export")
    snaps = list((recovery.AppDirs.recovery_dir()).glob("*/snapshot-*.acsproj"))
    assert len(snaps) == recovery.MAX_SNAPSHOTS
    items = recovery.list_recovery_items()
    assert items and items[0].reason == "before-export" and items[0].kind == "snapshot"


def test_stale_lock_detection():
    recovery.write_lock("p.acsproj", "v.mkv")
    assert recovery.read_stale_lock() is None  # our own pid is alive
    data = json.loads(recovery.lock_path().read_text())
    data["pid"] = 999999
    recovery.lock_path().write_text(json.dumps(data))
    stale = recovery.read_stale_lock()
    assert stale and stale["video"] == "v.mkv"


def test_part_path_and_orphans(tmp_path):
    final = tmp_path / "clip.mp4"
    part = recovery.part_path(final)
    assert part.name == "clip.part.mp4"
    part.write_bytes(b"x")
    assert recovery.find_orphans([str(tmp_path)]) == [part]


def test_queue_state_roundtrip():
    recovery.save_queue_state([{"id": "a", "status": "running"}])
    assert recovery.load_queue_state()[0]["id"] == "a"


def test_undo_redo_and_zoom_merge(qapp):
    from acs.core.document import ProjectDocument
    doc = ProjectDocument()
    fi = FrameIndex.cfr(Fraction(24000, 1001), 100)
    doc.load(Project(), None, fi, None)
    c = doc.set_in(100)
    assert doc.open_clip() is not None
    doc.set_out(199)
    assert doc.project.clips[0].out_frame == 200 and doc.open_clip() is None
    doc.add_marker(50)
    doc.update_clips([c.id], {"muted": True})
    assert doc.project.clips[0].muted
    doc.undo.undo()
    assert not doc.project.clips[0].muted
    doc.undo.undo()
    assert not doc.project.markers
    doc.undo.redo()
    assert len(doc.project.markers) == 1
    n = doc.undo.index()
    doc.set_view(100, 0)
    doc.set_view(150, 2)
    doc.set_view(200, 4)
    assert doc.undo.index() == n + 1  # three zoom steps merged into one entry
    doc.undo.undo()
    assert doc.project.view.pps == 0
    doc.undo.redo()
    assert doc.project.view.pps == 200 and doc.project.view.scroll == 4
    doc.select_clips([c.id])
    assert doc.delete_selected() and not doc.project.clips
    doc.undo.undo()
    assert doc.project.clips[0].id == c.id


def test_marker_colours_cycle_and_are_editable(qapp):
    from acs.core.document import ProjectDocument
    from acs.core.model import MARKER_COLORS
    doc = ProjectDocument()
    doc.load(Project(), None, FrameIndex.cfr(Fraction(24000, 1001), 100), None)
    colors = [doc.add_marker(f * 10).color for f in range(len(MARKER_COLORS) + 2)]
    assert colors[:len(MARKER_COLORS)] == MARKER_COLORS          # round-robin through the palette
    assert colors[len(MARKER_COLORS):] == MARKER_COLORS[:2]       # then starts over
    m = doc.project.markers[0]
    doc.set_marker_fields(m.id, {"color": "#123456", "label": "OP"})
    assert (m.color, m.label) == ("#123456", "OP")
    doc.undo.undo()
    assert (m.color, m.label) == (MARKER_COLORS[0], "")
    # the counter is persisted, so a reopened project continues the cycle
    q = Project.from_dict(doc.project.to_dict())
    assert q.take_marker_color() == MARKER_COLORS[2]
