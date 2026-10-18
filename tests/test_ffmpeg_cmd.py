from fractions import Fraction

from acs.core.ffmpeg_cmd import (ExportSpec, SubsSource, audio_args, auto_height, build_encode_passes, clamp_height,
                                 requested_height, size_video_kbps)
from acs.core.model import CropRect
from acs.core.presets import builtin_presets
from acs.core.probe import AudioStream, MediaInfo, VideoStream


def media(h=1080, w=1920, audios=2):
    v = VideoStream(0, "hevc", "Main 10", "yuv420p10le", w, h, "1:1", Fraction(24000, 1001), 0.0,
                    "bt709", "bt709", "bt709", "tv", 10)
    a = [AudioStream(i + 1, i, "flac" if i else "ac3", 6 if i else 2, 48000, "jpn", "", 0) for i in range(audios)]
    return MediaInfo("in.mkv", 1500.0, "matroska,webm", 10**9, 8_000_000, 0.0, v, a, [], 0)


def preset(pid):
    return next(p for p in builtin_presets() if p.id == pid)


def spec(p, **kw):
    base = dict(src="in.mkv", in_frame=240, out_frame=480, in_time=10.01, out_time=20.02, audio_rel=0,
                preset=p, output="out.mp4")
    base.update(kw)
    return ExportSpec(**base)


def test_heights_never_upscale():
    assert clamp_height(2160, 1080) is None
    assert clamp_height(1080, 1080) is None
    assert clamp_height(721, 1080) == 720
    assert requested_height(None, preset("x264_crf18")) is None
    assert requested_height(0, preset("discord_10mb")) is None
    assert requested_height(480, preset("x264_crf18")) == 480


def test_scale_filter_only_when_downscaling():
    info = media()
    args = build_encode_passes(spec(preset("x264_crf18"), height=None), info)[0]
    assert not any("scale=" in a for a in args)
    args = build_encode_passes(spec(preset("x264_crf18"), height=720), info)[0]
    assert "scale=-2:720:flags=lanczos" in args[args.index("-vf") + 1]


def test_mute_and_track_selection():
    info = media()
    args = build_encode_passes(spec(preset("x265_10_crf18"), audio_rel=None), info)[-1]
    assert "-an" in args and "0:a:0" not in args
    args = build_encode_passes(spec(preset("x265_10_crf18"), audio_rel=1), info)[-1]
    assert args[args.index("-map", args.index("0:v:0")) + 1] == "0:a:1" or "0:a:1" in args
    assert "-pix_fmt" in args and args[args.index("-pix_fmt") + 1] == "yuv420p10le"


def test_burn_in_and_crop_filter_order():
    info = media()
    subs = SubsSource("C:/cache/x", "track0.ass", "fonts")
    args = build_encode_passes(spec(preset("x264_crf18"), subs=subs, crop=CropRect(0, 140, 1920, 800),
                                    height=720), info)[0]
    vf = args[args.index("-vf") + 1]
    assert vf.index("subtitles=f=track0.ass") < vf.index("crop=1920:800:0:140") < vf.index("scale=-2:720")
    assert "setpts=PTS+10.010000/TB" in vf


def test_two_pass_for_target_size():
    info = media()
    p = preset("discord_10mb")
    passes = build_encode_passes(spec(p, video_kbps=900), info)
    assert len(passes) == 2
    assert "-pass" in passes[0] and passes[0][-3:] == ["-f", "null", "-"]
    assert passes[1][passes[1].index("-pass") + 1] == "2"
    x265 = preset("x265_10_crf18")
    x265.target_size_mb = 20
    passes = build_encode_passes(spec(x265, video_kbps=2000), info)
    assert any(a.startswith("pass=1:stats=") for a in passes[0])


def test_nvenc_uses_p010_and_cq():
    args = build_encode_passes(spec(preset("hevc_nvenc_10")), media())[-1]
    assert args[args.index("-pix_fmt") + 1] == "p010le"
    assert "-cq" in args and "main10" in args


def test_size_bitrate_and_auto_height():
    kbps = size_video_kbps(10, 60, 128)
    assert 1100 < kbps < 1300
    # very low bitrate for 1080p -> goes down but not below min
    h, bpp = auto_height(1920, 1080, 23.976, 400, 0.06, 480, None)
    assert h == 480
    # plenty of bitrate -> no downscale
    h, _ = auto_height(1920, 1080, 23.976, 8000, 0.06, 480, None)
    assert h is None
    # start height respected (never above)
    h, _ = auto_height(1920, 1080, 23.976, 8000, 0.06, 480, 720)
    assert h == 720


def test_copied_audio_drops_packets_before_the_clip_and_keeps_language():
    info = media()
    args = audio_args(spec(preset("orig_smartcut")), info, "mkv")   # smart cut mux and its fallback
    i = args.index("-c:a")
    assert args[i + 1] == "copy" and args[i + 2:i + 4] == ["-copypriorss", "0"]
    assert args[args.index("-metadata:s:a:0") + 1] == "language=jpn"
    args = build_encode_passes(spec(preset("x264_crf18")), info)[-1]
    assert "-copypriorss" not in args       # re-encoded audio is trimmed exactly by the input seek


def test_burned_subtitles_ignore_the_file_start_time():
    info = media()
    info.start_time = 2.0                   # the extracted .ass is on the relative timeline
    subs = SubsSource("work", "track0.ass", "fonts")
    args = build_encode_passes(spec(preset("x264_crf18"), subs=subs), info)[-1]
    assert args[args.index("-vf") + 1].startswith("setpts=PTS+10.010000/TB,subtitles=")
