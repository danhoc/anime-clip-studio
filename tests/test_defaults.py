import json
import os

from acs.core import recovery
from acs.core.clipinfo import pick_sub_track
from acs.core.probe import SubStream
from acs.core.model import Project
from acs.core.settings import DEFAULT_STEPS, AppDirs, AppSettings, parse_step, step_spec


def subs(*specs):
    return [SubStream(index=i + 3, rel=i, codec=codec, language=lang, title=title, forced=forced)
            for i, (lang, title, codec, forced) in enumerate(specs)]


def test_english_dialogue_subtitles_are_default():
    # the Ghost in the Shell layout: eng Forced, eng, jpn, jpn SDH, ...
    s = subs(("eng", "Forced", "subrip", False), ("eng", "", "subrip", False), ("jpn", "", "subrip", False),
             ("jpn", "SDH", "subrip", False))
    assert pick_sub_track(["eng", "en"], s) == 1
    # forced flag (disposition) is honoured too
    s = subs(("eng", "", "subrip", True), ("eng", "Full", "ass", False))
    assert pick_sub_track(["eng", "en"], s) == 1
    # only signs in English -> still English rather than another language
    s = subs(("rus", "Полные", "ass", False), ("eng", "Signs & Songs", "ass", False))
    assert pick_sub_track(["eng", "en"], s) == 1


def test_no_english_takes_first_text_track():
    s = subs(("rus", "Надписи", "ass", False), ("rus", "Полные", "ass", False))
    assert pick_sub_track(["eng", "en"], s) == 0
    s = subs(("eng", "", "hdmv_pgs_subtitle", False), ("jpn", "", "ass", False))  # bitmap is skipped
    assert pick_sub_track(["eng", "en"], s) == 1
    assert pick_sub_track(["eng"], []) is None


def test_old_default_sub_languages_migrate_to_english():
    AppDirs.data.mkdir(parents=True, exist_ok=True)
    AppDirs.settings_file().write_text(json.dumps({"preferred_sub_langs": ["rus", "ru", "eng", "en"]}))
    assert AppSettings.load().preferred_sub_langs == ["eng", "en"]
    AppDirs.settings_file().write_text(json.dumps({"preferred_sub_langs": ["jpn"]}))
    assert AppSettings.load().preferred_sub_langs == ["jpn"]  # a user's own choice is kept


def test_live_lock_detects_other_running_instance():
    recovery.write_lock()
    assert recovery.read_live_lock() is None          # our own lock
    data = json.loads(recovery.lock_path().read_text())
    data["pid"] = os.getppid()                         # the parent process is certainly alive
    recovery.lock_path().write_text(json.dumps(data))
    assert recovery.read_live_lock() is not None
    data["pid"] = 999999
    recovery.lock_path().write_text(json.dumps(data))
    assert recovery.read_live_lock() is None           # dead pid = crash, not a running instance

def test_new_projects_default_to_h264_crf18():
    assert Project().default_preset_id == "x264_crf18"
    assert AppSettings().default_preset_id == "x264_crf18"


def test_arrow_steps_default_and_round_trip():
    s = AppSettings()
    assert parse_step(s.step_arrow) == ("seconds", 1.0)       # arrows: 1 second
    assert parse_step(s.step_shift) == ("frames", 1.0)        # Shift+arrows: 1 frame
    assert parse_step(s.step_ctrl) == ("keyframe", 1.0)
    for kind, value in (("frames", 24.0), ("seconds", 0.5), ("keyframe", 1.0)):
        assert parse_step(step_spec(kind, value)) == (kind, value)
    assert parse_step("garbage", DEFAULT_STEPS["arrow"]) == ("seconds", 1.0)
    assert parse_step("0f", "1f") == ("frames", 1.0)
