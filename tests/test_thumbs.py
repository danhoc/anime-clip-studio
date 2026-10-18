"""Timeline thumbnails: planning of the two passes, merging of batches, and (slow) a real run."""

import shutil

import numpy as np
import pytest

from acs.core import thumbs


def test_coarse_times_spread_evenly_within_limits():
    assert thumbs.coarse_times(0) == []
    short = thumbs.coarse_times(90)              # a short clip still gets the minimum count
    assert len(short) == thumbs.COARSE_MIN and 0 < short[0] < short[-1] < 90
    episode = thumbs.coarse_times(24 * 60)
    assert len(episode) == 24
    assert np.allclose(np.diff(episode), 60)
    assert len(thumbs.coarse_times(3 * 3600)) == thumbs.COARSE_MAX


def test_min_gap_caps_the_number_of_thumbnails():
    assert thumbs.min_gap(10 * 60) == thumbs.MIN_GAP
    assert thumbs.min_gap(24 * 60) == pytest.approx(1.44)
    two_hours = 2 * 3600
    assert two_hours / thumbs.min_gap(two_hours) == pytest.approx(thumbs.MAX_THUMBS)


def test_split_ranges_cover_the_video_without_gaps():
    assert thumbs.split_ranges(100, 1) == [(0.0, None)]
    assert thumbs.split_ranges(0, 4) == [(0.0, None)]
    r = thumbs.split_ranges(100, 4)
    assert r[0][0] == 0 and r[-1][1] is None
    assert all(a[1] == b[0] for a, b in zip(r, r[1:]))


def test_workers_follow_the_cpu_count():
    assert thumbs.workers(2) == (2, 1)
    assert thumbs.workers(4) == (2, 2)
    assert thumbs.workers(24) == (4, 4)


def test_merge_sorts_and_keeps_existing_pictures_on_equal_times():
    a, b, c = (np.full((2, 2, 3), v, np.uint8) for v in (1, 2, 3))
    t, imgs = thumbs.merge(np.array([10.0]), [a], [5.0, 10.0], [b, c])
    assert t.tolist() == [5.0, 10.0]
    assert imgs[0] is b and imgs[1] is a          # 10 s keeps the picture it already had
    t2, imgs2 = thumbs.merge(np.empty(0), [], [], [])
    assert len(t2) == 0 and imgs2 == []


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
def test_generate_coarse_then_full_on_keyframes(tmp_path):
    import subprocess
    from acs.core.probe import probe, scan_packets
    from acs.core.timebase import FrameIndex
    src = tmp_path / "src.mkv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc2=size=320x180:rate=24:duration=60", "-c:v", "libx264", "-g", "48",
                    "-keyint_min", "48", "-sc_threshold", "0", str(src)], check=True)
    info = probe(src, "ffprobe")
    fi = FrameIndex.from_scan(scan_packets(src, info.start_time, "ffprobe"), info.fps)
    keys = np.array([fi.time_of(k) for k in fi.keyframes])
    v = info.video
    batches = []
    times, images = thumbs.generate(str(src), v.width, v.height, v.sar, info.start_time, "ffmpeg", thumb_h=72,
                                    duration=info.duration, cpus=8,
                                    on_batch=lambda t, im: batches.append((np.asarray(t), im.shape)))
    assert len(batches) >= 2                          # the coarse set first, then the full pass
    assert len(batches[0][0]) == thumbs.COARSE_MIN
    assert np.all(np.diff(times) > 0)
    assert len(times) == len(keys) == len(images)    # one keyframe every 2 s, over the 1 s cap
    assert np.allclose(times, keys, atol=1e-3)
    cached = thumbs.load_cached(str(src), 72)
    assert cached is not None and len(cached[0]) == len(times)
