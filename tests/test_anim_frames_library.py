import os

import numpy as np

from acs.core.anim import AnimParams, anim_args, fit_params, ladder_steps
from acs.core.frames import Deduper, FramesConfig, output_name, parse_showinfo, select_filter, trim_ranges
from acs.core.library import episode_neighbours, scan_folder
from acs.core.util import natural_key


def size_model(p: AnimParams) -> float:
    h = p.height or 1080
    dither = {"sierra2_4a": 1.3, "bayer:2": 1.2, "bayer:3": 1.1, "bayer:4": 1.0, "bayer:5": 0.95, "none": 0.8}
    return h * h * p.fps * (p.colors / 256) ** 0.5 * dither[p.dither] / 50


def test_fit_already_small_enough():
    start = AnimParams(height=480, fps=15, colors=128, dither="bayer:4")
    res = fit_params("gif", start, 1e12, size_model, 1080)
    assert res.reached and res.params == start and res.evaluations == 1


def test_fit_follows_ladder_and_respects_locks_and_mins():
    start = AnimParams(height=720, fps=24, colors=256, dither="sierra2_4a")
    target = size_model(AnimParams(height=720, fps=15, colors=256, dither="none")) * 0.99
    res = fit_params("gif", start, target, size_model, 1080, locks={"fps": True},
                     mins={"height": 480, "colors": 64})
    assert res.params.fps == 24  # locked
    assert res.params.colors >= 64 and (res.params.height or 1080) >= 480
    assert res.reached


def test_fit_unreachable_stops_at_minimums():
    start = AnimParams(height=720, fps=24, colors=256, dither="sierra2_4a")
    res = fit_params("gif", start, 1.0, size_model, 1080, mins={"height": 360, "fps": 12, "colors": 64})
    assert not res.reached
    assert res.params.height == 360 and res.params.fps == 12 and res.params.colors == 64
    assert res.params.dither == "none"


def test_ladder_steps_never_upscale():
    p = AnimParams(height=None, fps=30)
    steps = ladder_steps("height", p, {"height": 480}, 720)
    assert steps[0] is None and all(s is None or s < 720 for s in steps) and min(s for s in steps if s) == 480


def test_gif_args_palette_graph():
    a = anim_args("gif", "ffmpeg", "in.mkv", 5.0, 3.0, AnimParams(480, 15, 128, "bayer:4"), "o.gif")
    graph = a[a.index("-filter_complex") + 1]
    assert "fps=15" in graph and "scale=-2:480" in graph and "max_colors=128" in graph
    assert "bayer:bayer_scale=4" in graph


def test_dedup_strategies():
    base = np.full(64 * 36, 100, np.uint8)
    near = base.copy()
    near[:20] = 110
    other = np.full(64 * 36, 160, np.uint8)
    d = Deduper("strict", 1.0, 6.0, 5)
    assert [d.decide(x) for x in (base, base, near, other)] == [True, False, False, True]
    d = Deduper("similar", 1.0, 6.0, 5)
    slightly = np.full(64 * 36, 104, np.uint8)
    assert [d.decide(x) for x in (base, slightly, other, base)] == [True, False, True, False]


def test_frames_helpers():
    assert parse_showinfo("[Parsed_showinfo_2 @ 0x1] n:   3 pts:  1000 pts_time:12.5 duration") == (3, 12.5)
    cfg = FramesConfig(src="a.mkv", ranges=[(0, 10)], out_dir="x", template="{video}_{time}_{frame}")
    assert output_name(cfg, "Ep 01", 61.5, 1474) == "Ep 01_00-01-01-500_001474.png"
    assert "floor((t-0.000000)/2)" in select_filter(cfg, 0.0)
    assert trim_ranges([(0, 10), (20, 30)], 25.0) == [(25.001, 30)]
    assert trim_ranges([(0, 10)], None) == [(0, 10)]


def test_library_scan_and_neighbours(tmp_path):
    show = tmp_path / "Show" / "Season 1"
    show.mkdir(parents=True)
    for n in ("Ep 10.mkv", "Ep 2.mkv", "Ep 1.mkv", "notes.txt"):
        (show / n).write_bytes(b"x")
    (tmp_path / "empty").mkdir()
    ts = tmp_path / "Wallpaper" / "lib"
    ts.mkdir(parents=True)
    (ts / "lib.es2015.core.d.ts").write_text("declare const x: number;")
    assert not scan_folder(str(tmp_path), ["mkv", "ts"]).folders  # tiny files are not videos
    node = scan_folder(str(tmp_path), ["mkv"], min_size=0)
    assert [f.name for f in node.folders] == ["Show"]
    files = node.folders[0].folders[0].files
    assert [os.path.basename(f) for f in files] == ["Ep 1.mkv", "Ep 2.mkv", "Ep 10.mkv"]
    prev, nxt = episode_neighbours(str(show / "Ep 2.mkv"), ["mkv"])
    assert os.path.basename(prev) == "Ep 1.mkv" and os.path.basename(nxt) == "Ep 10.mkv"
    assert sorted(["b10", "b2"], key=natural_key) == ["b2", "b10"]


def test_frame_name_template_checks_and_fallback():
    from acs.core.frames import template_problem
    assert template_problem("{video}_{time}_{frame}") is None
    assert template_problem("{frame}") is None
    assert template_problem("{video}") == "not_unique"
    assert template_problem("{clip}_{time}") == "invalid"
    assert template_problem("{video_{time}") == "invalid"
    cfg = FramesConfig(src="a.mkv", ranges=[], out_dir="o", template="{clip}")
    assert output_name(cfg, "ep01", 1.5, 36) == "ep01_00-00-01-500_000036.png"   # unusable -> default


def test_frame_ranges_are_sorted_and_merged_for_resuming():
    from acs.core.frames import merge_ranges
    # clips listed in creation order, two of them overlapping
    assert merge_ranges([(600, 610), (60, 70), (65, 80), (80, 90), (5, 5)]) == [(60.0, 90.0), (600.0, 610.0)]
    # resuming after a crash inside the early range no longer drops the late one and vice versa
    assert trim_ranges(merge_ranges([(600, 610), (60, 70)]), 65.0) == [(65.001, 70.0), (600.0, 610.0)]
