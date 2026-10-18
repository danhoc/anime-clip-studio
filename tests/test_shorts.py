from types import SimpleNamespace

import pytest

from acs.core import shorts
from acs.core.clipinfo import summarize
from acs.core.export import build_export_spec_dict
from acs.core.model import Clip, CropRect, Project, ShortsFrame
from acs.core.presets import PresetStore, builtin_presets
from test_ffmpeg_cmd import media


def settings(shorts_mode: bool):
    return SimpleNamespace(shorts_mode=shorts_mode, bpp_x264=0.06, bpp_hevc_av1=0.035)


def test_default_frame_is_full_height_and_centred():
    assert shorts.crop_rect(ShortsFrame(), 1920, 1080) == CropRect(656, 0, 608, 1080)


def test_frame_is_kept_inside_the_picture():
    dar = shorts.display_aspect(1920, 1080)
    f = shorts.clamp(ShortsFrame(cx=0.0, cy=0.0, h=0.5), dar)
    x, y, w, h = shorts.fractions(f, dar)
    assert x == pytest.approx(0) and y == pytest.approx(0) and h == pytest.approx(0.5)
    assert w == pytest.approx(0.5 * 9 / 16 / dar)
    assert shorts.clamp(ShortsFrame(h=3.0), dar).h == 1.0
    assert shorts.clamp(ShortsFrame(h=0.0), dar).h == shorts.MIN_HEIGHT


def test_crop_is_even_inside_and_9_16():
    for frame in (ShortsFrame(0.9, 0.1, 0.37), ShortsFrame(0.0, 1.0, 0.8), ShortsFrame(0.5, 0.5, 0.123)):
        r = shorts.crop_rect(frame, 1920, 1080)
        assert all(v % 2 == 0 for v in (r.x, r.y, r.w, r.h))
        assert 0 <= r.x and r.x + r.w <= 1920 and 0 <= r.y and r.y + r.h <= 1080
        assert r.w / r.h == pytest.approx(9 / 16, abs=0.01)


def test_portrait_source_and_anamorphic_pixels():
    # a source narrower than 9:16: the frame is limited by the width
    r = shorts.crop_rect(ShortsFrame(), 540, 1200)
    assert r.w == 540 and r.x == 0 and r.h == 960
    # 1440x1080 with 4:3 pixels is shown as 1920x1080: the frame is narrower in source pixels
    assert shorts.crop_rect(ShortsFrame(), 1440, 1080, "4:3").w == 456
    assert shorts.parse_sar("0:1") == 1.0 and shorts.parse_sar("N/A") == 1.0


def test_shorts_frame_survives_save_and_load():
    p = Project(clips=[Clip(0, 10, shorts=ShortsFrame(0.3, 0.6, 0.7)), Clip(20, 30)])
    q = Project.from_dict(p.to_dict())
    assert q.clips[0].shorts == ShortsFrame(0.3, 0.6, 0.7)
    assert q.clips[1].shorts == ShortsFrame()
    old = p.to_dict()
    del old["clips"][0]["shorts"]   # projects saved before Shorts Mode
    assert Project.from_dict(old).clips[0].shorts == ShortsFrame()


def test_export_uses_the_frame_only_in_shorts_mode():
    info = media()
    clip = Clip(0, 240, 0.0, 10.0, name="c", crop_mode="manual", crop=CropRect(0, 140, 1920, 800),
                shorts=ShortsFrame(cx=0.25))
    preset = next(p for p in builtin_presets() if p.id == "orig_smartcut")
    kw = dict(src="in.mkv", clip=clip, in_time=0.0, out_time=10.0, audio_rel=0, sub_rel=None, preset=preset,
              output="out.mkv", height_req=None, hwaccel=False, info=info)
    off = build_export_spec_dict(settings=settings(False), **kw)
    on = build_export_spec_dict(settings=settings(True), **kw)
    assert off["crop"] == {"x": 0, "y": 140, "w": 1920, "h": 800} and not off["shorts"]
    assert on["crop"] == {"x": 176, "y": 0, "w": 608, "h": 1080} and on["shorts"]


def test_summary_reports_the_shorts_frame():
    store = PresetStore()
    project = Project(default_preset_id="orig_smartcut", clips=[Clip(0, 240, 0.0, 10.0)])
    c = project.clips[0]
    assert summarize(project, store, media(), c, settings(False)).mode == "smart cut"
    s = summarize(project, store, media(), c, settings(True))
    assert s.mode == "re-encode" and s.reasons == ["shorts"]
