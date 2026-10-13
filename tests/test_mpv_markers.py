import json
import shutil
import subprocess
from pathlib import Path

import pytest

from acs.core import mpv_markers as mm
from acs.core.model import Project


def write_records(recs, path=None):
    p = path or mm.store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")


def add(id_, kind, t, video, **extra):
    rec = {"v": 1, "op": "add", "id": id_, "kind": kind, "time": t, "path": str(video),
           "filename": Path(video).name, "size": Path(video).stat().st_size, "created": 1}
    rec.update(extra)
    return rec


@pytest.fixture
def videos(tmp_path):
    a = tmp_path / "Show" / "Ep 01.mkv"
    b = tmp_path / "Show" / "Ep 02.mkv"
    a.parent.mkdir()
    a.write_bytes(b"a" * 1000)
    b.write_bytes(b"b" * 2000)
    return a, b


def test_read_filters_by_video_and_applies_deletions(videos):
    a, b = videos
    write_records([add("1", "marker", 10.5, a), add("2", "in", 20.0, a), add("3", "out", 25.0, a),
                   add("4", "marker", 5.0, b), {"op": "del", "target": "2", "path": str(a)},
                   add("5", "in", 21.0, a)])
    marks = mm.read_marks(str(a))
    assert [(m.id, m.kind, m.time) for m in marks] == [("1", "marker", 10.5), ("5", "in", 21.0), ("3", "out", 25.0)]
    assert [m.id for m in mm.read_marks(str(b))] == ["4"]


def test_moved_file_matches_by_name_and_size(videos, tmp_path):
    a, _ = videos
    write_records([add("1", "marker", 3.0, a)])
    moved = tmp_path / "elsewhere" / a.name
    moved.parent.mkdir()
    shutil.move(str(a), moved)
    assert [m.id for m in mm.read_marks(str(moved))] == ["1"]


def test_sidecar_and_clear(videos):
    a, b = videos
    write_records([add("1", "marker", 1.0, a), add("2", "marker", 2.0, b)])
    write_records([add("s1", "marker", 4.0, a)], mm.sidecar_path(str(a)))
    assert [m.id for m in mm.read_marks(str(a))] == ["1", "s1"]
    assert mm.clear_marks(str(a)) == 2
    assert mm.read_marks(str(a)) == []
    assert not mm.sidecar_path(str(a)).exists()
    assert [m.id for m in mm.read_marks(str(b))] == ["2"]   # other videos untouched


def test_split_pairs_starts_and_ends():
    M = mm.MpvMark
    marks = [M("m", "marker", 1), M("i1", "in", 2), M("o1", "out", 5), M("i2", "in", 7), M("i3", "in", 8),
             M("o3", "out", 9), M("o4", "out", 12)]
    markers, clips, unpaired = mm.split_marks(marks)
    assert [m.id for m in markers] == ["m"]
    assert [(a.id, b.id) for a, b in clips] == [("i1", "o1"), ("i3", "o3")]
    assert sorted(m.id for m in unpaired) == ["i2", "o4"]


def test_seen_ids_persist_in_project():
    p = Project()
    p.mpv_seen_ids = ["a", "b"]
    assert Project.from_dict(p.to_dict()).mpv_seen_ids == ["a", "b"]


def _mpv_console():
    from acs.core.settings import AppSettings
    from acs.core.tools import _console_mpv, detect_mpv_player
    st = detect_mpv_player(AppSettings().mpv_player, Path("."))
    return _console_mpv(Path(st.path)) if st.ok else None


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_lua_script_in_real_mpv(tmp_path):
    """Run the real acs_markers.lua in the user's mpv on a synthetic clip and read the result back."""
    mpv = _mpv_console()
    if mpv is None:
        pytest.skip("no stand-alone mpv found")
    video = tmp_path / "clip.mkv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=320x180:rate=24000/1001:duration=6", str(video)], check=True)
    store = tmp_path / "store.jsonl"
    driver = tmp_path / "driver.lua"
    driver.write_text("""
mp.register_event("file-loaded", function()
  mp.commandv("seek", "1.5", "absolute+exact")
  mp.add_timeout(0.6, function() mp.commandv("script-message-to", "acs_markers", "acs-add", "marker") end)
  mp.add_timeout(0.9, function() mp.commandv("seek", "3", "absolute+exact") end)
  mp.add_timeout(1.4, function() mp.commandv("script-message-to", "acs_markers", "acs-add", "in") end)
  mp.add_timeout(1.6, function() mp.commandv("script-message-to", "acs_markers", "acs-add", "out") end)
  mp.add_timeout(1.8, function() mp.commandv("script-message-to", "acs_markers", "acs-undo") end)
  mp.add_timeout(2.4, function() mp.commandv("quit") end)
end)
""", encoding="utf-8")
    subprocess.run([str(mpv), "--no-config", "--vo=null", "--ao=null", "--pause", "--keep-open=yes",
                    f"--script={mm.script_source()}", f"--script={driver}",
                    f"--script-opts=acs_markers-file={store}", str(video)], timeout=60, capture_output=True)
    recs = [json.loads(l) for l in store.read_text(encoding="utf-8").splitlines() if l.strip()]
    adds = [r for r in recs if r["op"] == "add"]
    assert [r["kind"] for r in adds] == ["marker", "in", "out"]
    assert abs(adds[0]["time"] - 1.5) < 0.05 and abs(adds[1]["time"] - 3.0) < 0.05
    assert recs[-1]["op"] == "del" and recs[-1]["target"] == adds[-1]["id"]   # undo removed the "out"
    assert Path(adds[0]["path"]).resolve() == video.resolve() and adds[0]["size"] == video.stat().st_size
    # and the app side reads exactly what the script wrote
    mm.ensure_store_dir()
    shutil.copyfile(store, mm.store_path())
    marks = mm.read_marks(str(video))
    assert [m.kind for m in marks] == ["marker", "in"]
