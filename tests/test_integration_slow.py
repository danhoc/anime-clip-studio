"""End-to-end runs of the export / frames runners on synthetic media (needs ffmpeg). `pytest -m slow`."""

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from acs.core.export import build_export_spec_dict, run_export
from acs.core.frames import FramesConfig, run_frames
from acs.core.jobs import DONE, Job, JobQueue
from acs.core.model import Clip
from acs.core.presets import builtin_presets
from acs.core.probe import probe, scan_packets
from acs.core.settings import AppSettings
from acs.core.timebase import FrameIndex

pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")]

FPS = 24000 / 1001


def make_source(path: Path, codec: str) -> None:
    v = (["-c:v", "libx265", "-pix_fmt", "yuv420p10le", "-x265-params", "keyint=48:min-keyint=48:log-level=error"]
         if codec == "hevc" else ["-c:v", "libx264", "-g", "48", "-keyint_min", "48", "-sc_threshold", "0"])
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24000/1001:duration=20",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=20"] + v
                   + ["-c:a", "aac", "-shortest", str(path)], check=True)


def run_jobs(jobs, settings, timeout=300):
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication.instance() or QCoreApplication([])
    q = JobQueue(settings, {"export": run_export, "frames": run_frames})
    q.enqueue(jobs)
    t0 = time.time()
    while q.has_active() and time.time() - t0 < timeout:
        app.processEvents()
        time.sleep(0.1)
    return q.jobs


def packets(path: Path) -> int:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
                        "stream=nb_read_packets", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return int(r.stdout.strip().split(",")[0])


def has_audio(path: Path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(r.stdout.strip())


@pytest.mark.parametrize("codec", ["h264", "hevc"])
def test_smartcut_and_mute(tmp_path, codec):
    src = tmp_path / f"src_{codec}.mkv"
    make_source(src, codec)
    settings = AppSettings().resolved()
    presets = {p.id: p for p in builtin_presets()}
    # Times come from the exact frame index, exactly like the app (mkv + B-frames shift the start).
    info = probe(str(src), settings.ffprobe)
    fi = FrameIndex.from_scan(scan_packets(str(src), info.start_time, settings.ffprobe), info.fps)
    in_f, out_f = 30, 300   # spans several 48-frame GOPs, starts and ends mid-GOP
    t_in, t_out = fi.time_of(in_f), fi.time_of(out_f)
    clip = Clip(in_frame=in_f, out_frame=out_f, in_time=t_in, out_time=t_out, name="c")
    muted = Clip(in_frame=in_f, out_frame=out_f, in_time=t_in, out_time=t_out, name="m", muted=True)
    jobs = []
    for c, preset, name in ((clip, presets["orig_smartcut"], "smart.mkv"), (muted, presets["x264_crf18"], "muted.mp4")):
        spec = build_export_spec_dict(src=str(src), clip=c, in_time=c.in_time, out_time=c.out_time,
                                      audio_rel=0, sub_rel=None, preset=preset, output=str(tmp_path / name),
                                      height_req=None, hwaccel=False, settings=settings)
        jobs.append(Job("export", name, spec))
    done = run_jobs(jobs, settings)
    assert all(j.status == DONE for j in done), [(j.title, j.message) for j in done]
    smart = tmp_path / "smart.mkv"
    assert packets(smart) == out_f - in_f
    assert not [w for w in done[0].warnings if w.startswith("smartcut_validation")]
    assert has_audio(smart)
    assert packets(tmp_path / "muted.mp4") == out_f - in_f
    assert not has_audio(tmp_path / "muted.mp4")
    assert not list(tmp_path.glob("*.part.*"))


def test_frames_interval(tmp_path):
    src = tmp_path / "src.mkv"
    make_source(src, "h264")
    settings = AppSettings().resolved()
    cfg = FramesConfig(src=str(src), ranges=[(0.0, 10.0)], out_dir=str(tmp_path / "frames"), interval=2.0,
                       hwaccel=False)
    done = run_jobs([Job("frames", "f", {"config": cfg.to_dict()})], settings)
    assert done[0].status == DONE, done[0].message
    assert len(list((tmp_path / "frames").glob("*.png"))) == 5
