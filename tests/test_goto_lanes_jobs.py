"""Go-to parsing, timeline clip lanes, export name conflicts and the queue on exit."""

import threading

from acs.core.util import parse_position


def test_parse_position_times_frames_and_relative():
    assert parse_position("83.5") == ("time", 83.5, False)
    assert parse_position(" 83,5 ") == ("time", 83.5, False)
    assert parse_position("83s") == ("time", 83.0, False)
    assert parse_position("1:23") == ("time", 83.0, False)
    assert parse_position("01:02:03.500") == ("time", 3723.5, False)
    assert parse_position("90:00") == ("time", 5400.0, False)
    assert parse_position("#1234") == ("frame", 1234.0, False)
    assert parse_position("1234F") == ("frame", 1234.0, False)
    assert parse_position("+10f") == ("frame", 10.0, True)
    assert parse_position("-2") == ("time", -2.0, True)
    assert parse_position("−1:00") == ("time", -60.0, True)   # the minus sign of the dialog hint


def test_parse_position_rejects_garbage():
    for bad in ("", "+", "abc", "1:", ":30", "1:75", "1:2:3:4", "#", "#12.5", "nan", "inf", "12x", "1:60:00"):
        assert parse_position(bad) is None, bad


class _C:
    def __init__(self, cid, a, b):
        self.id, self.in_frame, self.out_frame = cid, a, b


def test_clip_lanes_appear_only_for_overlaps():
    from acs.ui.timeline import LANE_GAP, LANE_H, MAX_LANES, assign_lanes, lane_area_height
    assert assign_lanes([]) == ({}, 1)
    lanes, n = assign_lanes([_C("a", 0, 100), _C("b", 100, 200), _C("c", 300, None)])
    assert n == 1 and set(lanes.values()) == {0}
    lanes, n = assign_lanes([_C("a", 0, 100), _C("b", 50, 150)])
    assert n == 2 and lanes == {"a": 0, "b": 1}
    _lanes, n = assign_lanes([_C("a", 0, 100), _C("b", 50, 150), _C("c", 60, 70)])
    assert n == 3
    pitch = LANE_H + LANE_GAP
    assert [lane_area_height(k) for k in (1, 2, 3, 5)] == [pitch, 2 * pitch, 3 * pitch, MAX_LANES * pitch]


def test_export_never_gives_two_jobs_the_same_file(tmp_path):
    from acs.ui.export_dialog import resolve_conflict
    target = tmp_path / "ep.mp4"
    target.write_bytes(b"x")
    taken: set[str] = set()
    first = resolve_conflict(target, "overwrite", taken)
    assert first == target                       # overwriting a file on disk is allowed
    taken.add(str(first).lower())
    assert resolve_conflict(target, "overwrite", taken) == tmp_path / "ep (2).mp4"   # not another clip's file
    busy = {str(tmp_path / "new.mp4").lower()}   # output of a job still in the queue
    assert resolve_conflict(tmp_path / "new.mp4", "number", busy) == tmp_path / "new (2).mp4"
    assert resolve_conflict(tmp_path / "new.mp4", "skip", busy) is None


def test_queue_shutdown_forgets_cancelled_jobs(qapp):
    from acs.core import recovery
    from acs.core.jobs import Job, JobQueue
    from acs.core.settings import AppSettings
    started, release = threading.Event(), threading.Event()

    def slow(ctx, job):
        started.set()
        while not release.wait(0.01):
            ctx.check()

    q = JobQueue(AppSettings(), {"slow": slow})
    q.enqueue([Job("slow", "a", {}), Job("slow", "b", {})])
    assert started.wait(5)
    assert len(recovery.load_queue_state()) == 2
    q.shutdown()
    assert recovery.load_queue_state() == []
    release.set()
    q.enqueue([Job("slow", "c", {})])            # nothing starts or is persisted after shutdown
    assert recovery.load_queue_state() == []
    assert all(j.status != "running" or j.title != "c" for j in q.jobs)
