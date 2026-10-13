"""Extract a subtitle track + attached fonts once per video into a cache folder.

ffmpeg's `subtitles` filter then runs with cwd = that folder and plain relative names, so no
filter-graph escaping of torrent-style paths ([], ', :, commas) is ever needed.
"""

from __future__ import annotations

from pathlib import Path

from acs.core.ffmpeg_cmd import SubsSource
from acs.core.probe import MediaInfo
from acs.core.settings import AppDirs
from acs.core.util import file_fingerprint, run_capture

FONTS_DIR = "fonts"


class SubsError(RuntimeError):
    pass


def subs_cache_dir(src: str) -> Path:
    try:
        key = file_fingerprint(src)[:16]
    except OSError:
        from acs.core.util import norm_path_key, sha1_text
        key = sha1_text(norm_path_key(src))[:16]
    return AppDirs.subs_cache() / key


def prepare_subs(src: str, info: MediaInfo, sub_rel: int, ffmpeg: str = "ffmpeg") -> SubsSource:
    if not info.subs:
        raise SubsError("the video has no subtitle tracks")
    sub_rel = max(0, min(sub_rel, len(info.subs) - 1))
    st = info.subs[sub_rel]
    if st.is_bitmap:
        raise SubsError(f"bitmap subtitles ({st.codec}) cannot be burned in")
    d = subs_cache_dir(src)
    fonts = d / FONTS_DIR
    fonts.mkdir(parents=True, exist_ok=True)
    ass_name = f"track{sub_rel}.ass"
    ass = d / ass_name
    marker = fonts / ".done"
    if not ass.exists() or not marker.exists():
        args = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
        if info.attachments and not marker.exists():
            args += ["-dump_attachment:t", ""]
        args += ["-i", src, "-map", f"0:s:{sub_rel}", "-c:s", "ass", str(ass)]
        # cwd=fonts: -dump_attachment writes each font under its own file name there.
        res = run_capture(args, cwd=fonts, timeout=300)
        if res.returncode != 0 or not ass.exists():
            raise SubsError(res.stderr.strip()[-400:] or "subtitle extraction failed")
        marker.write_text("ok", encoding="utf-8")
    return SubsSource(workdir=str(d), ass_name=ass_name, fonts_name=FONTS_DIR)
