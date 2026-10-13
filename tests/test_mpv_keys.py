import pytest

from acs.core.mpv_keys import (default_keys, find_conflicts, mpv_to_qt, normalize, qt_to_mpv, render_script)
from acs.core.mpv_markers import script_source


@pytest.mark.parametrize("qt, mpv", [
    ("Alt+M", "Alt+m"), ("Ctrl+Alt+I", "Ctrl+Alt+i"), ("Alt+Shift+M", "Alt+M"), ("Alt+Backspace", "Alt+BS"),
    ("Ctrl+Left", "Ctrl+LEFT"), ("F5", "F5"), ("Alt+PgDown", "Alt+PGDWN"), ("", ""),
])
def test_qt_to_mpv(qt, mpv):
    assert qt_to_mpv(qt) == mpv


def test_round_trip_and_normalize():
    for k in ("Alt+m", "Alt+M", "Alt+BS", "Ctrl+Shift+LEFT", "F12"):
        assert qt_to_mpv(mpv_to_qt(k)) == k
    assert normalize("alt+shift+m") == normalize("Alt+M") == "Alt+M"
    assert normalize("Alt+m") != normalize("Alt+M")
    assert normalize("ctrl+alt+bs") == normalize("Alt+Ctrl+BS")


def test_conflict_kinds():
    bindings = [
        {"key": "Alt+BS", "section": "default", "is_weak": True, "cmd": "set video-zoom 0", "comment": "reset zoom"},
        {"key": "Alt+i", "section": "default", "is_weak": False, "cmd": "show-text hi"},           # input.conf
        {"key": "Alt+l", "section": "input_playlistmanager", "owner": "playlistmanager", "cmd": "x"},
        {"key": "Alt+m", "section": "input_acs_markers", "owner": "acs_markers", "cmd": "y"},     # our old copy
        {"key": "Alt+o", "section": "default", "is_weak": True, "cmd": "script-binding acs_markers/acs-clip-end"},
    ]
    keys = {"marker": "Alt+m", "in": "Alt+i", "out": "Alt+o", "undo": "Alt+BS", "list": "Alt+l"}
    got = {(c.action, c.kind) for c in find_conflicts(keys, bindings)}
    assert got == {("undo", "builtin"), ("in", "input_conf"), ("list", "script")}
    dup = find_conflicts({"marker": "Alt+m", "in": "alt+m"}, [])
    assert [(c.action, c.kind, c.owner) for c in dup] == [("in", "duplicate", "marker")]
    assert find_conflicts(default_keys(), bindings[:1]) == []   # the default undo key avoids Alt+BS


def test_render_script_writes_chosen_keys():
    src = script_source().read_text(encoding="utf-8")
    out = render_script(src, {"marker": "Ctrl+m", "in": "Alt+i", "out": "Alt+o", "undo": "", "list": "Alt+l"})
    assert 'key_marker = "Ctrl+m"' in out and 'key_undo = ""' in out
    assert out.count("key_marker") == src.count("key_marker")
