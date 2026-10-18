from fractions import Fraction

import pytest

from acs.core.probe import PacketScan, parse_packet_csv
from acs.core.smartcut import check_points, parse_first_vcl_nal, plan_segments
from acs.core.timebase import FrameIndex

FPS = Fraction(24000, 1001)
FD = 1001 / 24000


def closed_gop_scan(n_gops: int, gop: int = 24, start: float = 0.0) -> PacketScan:
    """Decode order with B-frames: I P B B ... like a typical closed-GOP encode."""
    pts, key = [], []
    for g in range(n_gops):
        base = g * gop
        order = [0] + [i for i in range(1, gop)]
        # emulate reordering: P(i+2) before B(i+1)
        dec = [0]
        i = 1
        while i < gop:
            if i + 1 < gop:
                dec += [i + 1, i]
                i += 2
            else:
                dec.append(i)
                i += 1
        assert sorted(dec) == order
        for k, d in enumerate(dec):
            pts.append(start + (base + d) * FD)
            key.append(k == 0)
    return PacketScan(pts, key)


def test_cfr_index_roundtrip():
    fi = FrameIndex.cfr(FPS, 60.0)
    for f in (0, 1, 100, 1438):
        assert fi.frame_at(fi.time_of(f)) == f
    assert fi.frame_at(fi.time_of(10) + FD * 0.4) == 10


def test_frame_in_second_counts_real_frames():
    fi = FrameIndex.cfr(FPS, 120.0)
    assert fi.frame_in_second(0) == (1, 24)
    assert fi.frame_in_second(23) == (24, 24)
    assert fi.frame_in_second(24) == (1, 24)          # frame 24 starts second 1 (t = 1.001)
    totals = {}
    for f in range(fi.frame_count):
        pos, total = fi.frame_in_second(f)
        assert 1 <= pos <= total
        totals[int(fi.time_of(f) + 1e-6)] = total
    assert sum(totals.values()) == fi.frame_count      # every frame counted exactly once
    assert set(totals.values()) <= {23, 24}            # 23.976 fps: occasionally 23 frames
    assert 23 in totals.values()


def test_waveform_sample_rates():
    from acs.core.waveform import peaks_from_pcm, sample_rate_for
    for bps in (100, 200, 400, 800):
        rate = sample_rate_for(bps)
        assert rate % bps == 0 and rate // bps >= 20 and rate <= 16000
    import numpy as np
    pcm = np.array([0, 100, -32768, 5, 0, 0], dtype="<i2")
    assert list(peaks_from_pcm(pcm, 3)) == [255, 3]


def test_scan_index_and_gops():
    fi = FrameIndex.from_scan(closed_gop_scan(5), FPS)
    assert fi.exact and fi.frame_count == 120
    assert list(fi.keyframes) == [0, 24, 48, 72, 96]
    assert list(fi.gops.gop_start_frame) == [0, 24, 48, 72, 96]
    assert fi.prev_keyframe(30) == 24 and fi.next_keyframe(30) == 48
    assert fi.is_keyframe(48) and not fi.is_keyframe(47)
    assert fi.typical_gop == pytest.approx(24 * FD)


def test_typical_gop_unknown_without_scan():
    assert FrameIndex.cfr(FPS, 10.0).typical_gop is None


def test_nonzero_start_time_is_relative():
    text = "\n".join(f"{10.5 + i * FD:.6f},{10.5 + i * FD:.6f},{'K_' if i % 24 == 0 else '__'}" for i in range(48))
    scan = parse_packet_csv(text, start_time=10.5)
    fi = FrameIndex.from_scan(scan, FPS)
    assert fi.time_of(0) == pytest.approx(0.0, abs=1e-6)
    assert fi.frame_at(0.99) == int(0.99 / FD)
    assert fi.frame_at(fi.time_of(24)) == 24


def test_plan_segments_head_copy_tail():
    fi = FrameIndex.from_scan(closed_gop_scan(10), FPS)
    plan = plan_segments(30, 150, fi, is_idr=lambda j: True)
    assert not plan.full_encode
    kinds = [(s.kind, s.start_f, s.end_f) for s in plan.segments]
    assert kinds == [("enc", 30, 48), ("copy", 48, 144), ("enc", 144, 150)]
    assert plan.segments[1].packets == 96
    assert sum(s.frames for s in plan.segments) == 120


def test_plan_segments_starting_on_keyframe_and_to_end():
    fi = FrameIndex.from_scan(closed_gop_scan(4), FPS)
    plan = plan_segments(24, 96, fi, is_idr=lambda j: True)
    assert [(s.kind, s.start_f, s.end_f) for s in plan.segments] == [("copy", 24, 96)]


def test_plan_segments_skips_cra_and_falls_back():
    fi = FrameIndex.from_scan(closed_gop_scan(10), FPS)
    plan = plan_segments(30, 200, fi, is_idr=lambda j: j >= 3)
    assert plan.segments[0] == plan.segments[0].__class__("enc", 30, 72)
    assert plan_segments(30, 200, fi, is_idr=lambda j: False).full_encode
    assert plan_segments(30, 40, fi, is_idr=lambda j: True).full_encode  # shorter than a GOP


def test_plan_segments_open_gop_not_used_as_copy_start():
    scan = closed_gop_scan(6)
    # make GOP 2 "open": its second packet gets a pts before the keyframe
    k2 = 2 * 24
    scan.pts[k2 + 1] = (k2 - 0.5) * FD
    fi = FrameIndex.from_scan(scan, FPS)
    plan = plan_segments(30, 140, fi, is_idr=lambda j: True)
    assert plan.full_encode or plan.segments[0].end_f != fi.gops.key_frame[2]


def test_check_points_cover_splices():
    fi = FrameIndex.from_scan(closed_gop_scan(10), FPS)
    plan = plan_segments(30, 150, fi, is_idr=lambda j: True)
    assert check_points(plan, 30) == [0, 17, 18, 113, 114, 119]


def test_parse_trace_headers():
    text = ("[trace_headers] nal_unit_type    100000 = 32\n"
            "[trace_headers] nal_unit_type    010100 = 20\n")
    assert parse_first_vcl_nal(text, "hevc") == 20
    assert parse_first_vcl_nal("nal_unit_type 00101 = 5", "h264") == 5


def test_clip_ending_with_the_last_frame_keeps_it_when_the_exact_index_arrives(qapp):
    from acs.core.document import ProjectDocument
    from acs.core.model import Project
    fps = Fraction(24000, 1001)
    cfr = FrameIndex.cfr(fps, 10.0)
    n = cfr.frame_count
    exact = FrameIndex.from_scan(PacketScan([i / float(fps) for i in range(n)], [i % 24 == 0 for i in range(n)]),
                                 fps)
    assert exact.nearest_boundary(cfr.end_time) == n
    assert exact.nearest_boundary(exact.time_of(n - 1)) == n - 1
    assert exact.nearest_boundary(exact.time_of(10)) == 10
    doc = ProjectDocument()
    doc.load(Project(), None, cfr, None)
    doc.set_in(n - 30)
    doc.set_out(n - 1)                       # O on the last frame: Out = frame_count
    doc.set_index(exact)
    assert doc.project.clips[0].out_frame == n
