"""Extract a subtitle track + attached fonts once per video into a cache folder.

ffmpeg's `subtitles` filter then runs with cwd = that folder and plain relative names, so no
filter-graph escaping of torrent-style paths ([], ', :, commas) is ever needed.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from acs.core.ffmpeg_cmd import SubsSource
from acs.core.model import CropRect
from acs.core.probe import MediaInfo
from acs.core.settings import AppDirs
from acs.core.util import file_fingerprint, run_capture

FONTS_DIR = "fonts"
FIT_PREFIX = "fit_"           # per-job copies of a track, fitted to the exported picture
FIT_STYLE_SUFFIX = "__acs"    # style copies used by the dialogue lines of a fitted track
# Shorts Mode: dialogue font relative to the source picture, times frame height / picture height
# (a full-height frame on a 16:9 episode: 3/4 of the original size, about 25 characters per line).
SHORTS_SUB_FONT = 0.75


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


# ---- fitting dialogue into the exported picture -------------------------------------------------
_POSITIONED = re.compile(r"\\(pos|move)\s*\(")
_ALIGN = re.compile(r"\\a(n?)(\d+)")
_SIZES = re.compile(r"\\(fs|fsp|bord|xbord|ybord|shad|xshad|yshad)(-?\d+(?:\.\d+)?)")
_RESET = re.compile(r"\\r([^\\}]+)")
_OVERRIDE = re.compile(r"\{[^}]*\}")
_LEGACY_ALIGN = {1: 1, 2: 2, 3: 3, 5: 7, 6: 8, 7: 9, 9: 4, 10: 5, 11: 6}


def shorts_sub_scale(frame_h: int, src_h: int) -> float:
    """Automatic dialogue size in Shorts Mode for a frame `frame_h` pixels high."""
    return SHORTS_SUB_FONT * frame_h / src_h if src_h > 0 else 1.0


def _num(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _play_res(lines: list[str]) -> tuple[float, float]:
    vals: dict[str, float] = {}
    for line in lines:
        key, _, val = line.partition(":")
        if key.strip().lower() in ("playresx", "playresy"):
            try:
                vals[key.strip().lower()] = float(val)
            except ValueError:
                pass
    x, y = vals.get("playresx", 0.0), vals.get("playresy", 0.0)
    if not x and not y:     # libass defaults
        return 384.0, 288.0
    if not y:
        return x, (1024.0 if x == 1280 else x * 3 / 4)
    if not x:
        return y * 4 / 3, y
    return x, y


def _alignment(text: str, style_align: int) -> int:
    """Alignment (numpad layout) of a line: its last \\an / \\a override, else its style's."""
    align = style_align
    for block in _OVERRIDE.findall(text):
        for m in _ALIGN.finditer(block):
            n = int(m.group(2))
            if m.group(1):
                align = n if 1 <= n <= 9 else align
            else:
                align = _LEGACY_ALIGN.get(n, align)
    return align


def fit_ass(text: str, src_w: int, src_h: int, area: CropRect | None, scale: float) -> str:
    """Make the dialogue of an ASS script fit the exported picture.

    Lines placed by alignment and margins (dialogue; not signs with \\pos / \\move) get margins that
    keep them inside `area`, a crop of the src_w x src_h picture: they sit at the bottom (or top) of
    the exported picture and wrap within its width. Their font, outline, shadow and spacing are
    scaled by `scale`. Positioned signs belong to the picture and stay as they are.
    """
    lines = text.splitlines()
    prx, pry = _play_res(lines)
    off_l = off_r = off_t = off_b = 0.0
    if area is not None and area.is_valid() and src_w > 0 and src_h > 0:
        off_l = area.x * prx / src_w
        off_r = (src_w - area.x - area.w) * prx / src_w
        off_t = area.y * pry / src_h
        off_b = (src_h - area.y - area.h) * pry / src_h
    section = ""
    style_fmt: list[str] = []
    event_fmt: list[str] = []
    styles: dict[str, list[str]] = {}
    style_at: dict[str, int] = {}
    for i, line in enumerate(lines):
        st = line.strip()
        low = st.lower()
        if st.startswith("["):
            section = low
        elif section == "[v4+ styles]" and low.startswith("format:"):
            style_fmt = [f.strip().lower() for f in st[7:].split(",")]
        elif section == "[v4+ styles]" and low.startswith("style:") and style_fmt:
            vals = [v.strip() for v in st[6:].split(",", len(style_fmt) - 1)]
            if len(vals) == len(style_fmt):
                styles[vals[0]] = vals
                style_at[vals[0]] = i
        elif section == "[events]" and low.startswith("format:"):
            event_fmt = [f.strip().lower() for f in st[7:].split(",")]
    margins = {"marginl", "marginr", "marginv"}
    if (not styles or not margins | {"name", "fontsize", "alignment"} <= set(style_fmt)
            or not margins | {"style", "text"} <= set(event_fmt)):
        return text   # not a V4+ script this understands: burned in unchanged
    S = {k: i for i, k in enumerate(style_fmt)}
    E = {k: i for i, k in enumerate(event_fmt)}

    def fval(vals: list[str], key: str) -> float:
        try:
            return float(vals[S[key]]) if key in S else 0.0
        except ValueError:
            return 0.0

    def fit_size(m: re.Match) -> str:
        return f"\\{m.group(1)}{_num(float(m.group(2)) * scale)}"

    def fit_reset(m: re.Match) -> str:
        return f"\\r{m.group(1)}{FIT_STYLE_SUFFIX}" if m.group(1) in styles else m.group(0)

    default = next(iter(styles))
    used: set[str] = set()
    out = list(lines)
    section = ""
    for i, line in enumerate(lines):
        st = line.strip()
        if st.startswith("["):
            section = st.lower()
            continue
        if section != "[events]" or not st.lower().startswith("dialogue:"):
            continue
        head, _, rest = line.partition(":")
        vals = rest.split(",", len(event_fmt) - 1)
        if len(vals) != len(event_fmt) or _POSITIONED.search(vals[E["text"]]):
            continue
        name = vals[E["style"]].strip().lstrip("*")
        style = styles.get(name) or styles[default]
        name = style[0]
        align = _alignment(vals[E["text"]], int(fval(style, "alignment")) or 2)

        def margin(key: str) -> float:
            try:
                own = float(vals[E[key]])
            except ValueError:
                own = 0.0
            return own or fval(style, key)

        fitted = {"marginl": off_l + margin("marginl") * scale,
                  "marginr": off_r + margin("marginr") * scale,
                  "marginv": margin("marginv") * scale + (off_t if align >= 7 else off_b if align <= 3 else 0.0)}
        for key, v in fitted.items():   # never 0: that would mean "the style's margin" to libass
            vals[E[key]] = str(max(1, int(round(v))))
        vals[E["style"]] = name + FIT_STYLE_SUFFIX
        body = vals[E["text"]]
        if scale != 1.0:
            body = _OVERRIDE.sub(lambda b: _SIZES.sub(fit_size, b.group(0)), body)
        used.add(name)
        used.update(n for n in _RESET.findall(body) if n in styles)
        vals[E["text"]] = _RESET.sub(fit_reset, body)
        out[i] = f"{head}:{','.join(vals)}"
    # scaled copies of the dialogue styles, right after their originals (signs keep the originals)
    for name in sorted(used, key=lambda n: style_at[n], reverse=True):
        copy = list(styles[name])
        copy[0] = name + FIT_STYLE_SUFFIX
        for key in ("fontsize", "outline", "shadow", "spacing"):
            if key in S:
                copy[S[key]] = _num(fval(styles[name], key) * scale)
        out.insert(style_at[name] + 1, "Style: " + ",".join(copy))
    return "\n".join(out) + "\n"


def fit_subs(subs: SubsSource, src_w: int, src_h: int, area: CropRect | None, scale: float,
             job_id: str) -> SubsSource:
    """A per-job copy of the extracted track made with fit_ass (the cached track is not changed);
    `subs` itself when nothing needs to change."""
    if (area is None or not area.is_valid()) and abs(scale - 1.0) < 1e-6:
        return subs
    d = Path(subs.workdir)
    text = (d / subs.ass_name).read_text(encoding="utf-8-sig", errors="replace")
    name = f"{FIT_PREFIX}{job_id}.ass"
    (d / name).write_text(fit_ass(text, src_w, src_h, area, scale), encoding="utf-8")
    return replace(subs, ass_name=name)


def remove_fitted(subs: SubsSource | None) -> None:
    if subs is not None and subs.ass_name.startswith(FIT_PREFIX):
        try:
            (Path(subs.workdir) / subs.ass_name).unlink()
        except OSError:
            pass
