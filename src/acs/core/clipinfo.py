"""Effective per-clip settings (clip override -> project default -> app preference)."""

from __future__ import annotations

from dataclasses import dataclass, field

from acs.core.ffmpeg_cmd import (REASON_CROP, REASON_SHORTS, auto_height, bits_per_pixel, bpp_threshold, clamp_height, reencode_reasons,
                                 requested_height, scaled_width, size_video_kbps, source_dims)
from acs.core.model import Clip, Project
from acs.core.presets import Preset, PresetStore
from acs.core.probe import MediaInfo
from acs.core.shorts import crop_rect


def pick_track(langs: list[str], tracks) -> int | None:
    """First track whose language matches the preference list, else the first track."""
    if not tracks:
        return None
    for lang in langs:
        for t in tracks:
            if t.language and t.language.lower() == lang.lower():
                return t.rel
    return tracks[0].rel


PARTIAL_SUB_WORDS = ("forced", "sign", "song", "надпис", "песн", "karaoke", "commentary", "sdh")
FULL_SUB_WORDS = ("full", "полн", "dialog", "диалог")


def is_partial_sub(s) -> bool:
    """Signs / songs / forced / commentary tracks — not the dialogue a viewer usually wants."""
    title = (s.title or "").lower()
    return bool(getattr(s, "forced", False)) or any(w in title for w in PARTIAL_SUB_WORDS)


def pick_sub_track(langs: list[str], subs) -> int | None:
    """English dialogue subtitles by default (or the user's preferred languages), else track 1.

    Within a language, full dialogue tracks win over signs/songs/forced/SDH ones.
    """
    text = [s for s in subs if not s.is_bitmap]
    if not text:
        return None
    for lang in langs:
        cands = [s for s in text if s.language and s.language.lower() == lang.lower()]
        if cands:
            full = [s for s in cands if any(w in s.title.lower() for w in FULL_SUB_WORDS)]
            normal = [s for s in cands if not is_partial_sub(s)]
            return (full or normal or cands)[0].rel
    return text[0].rel


def effective_preset(project: Project, store: PresetStore, clip: Clip) -> Preset:
    pid = clip.preset_id or project.default_preset_id
    return (store.get(pid, project.presets_snapshot) or store.get(project.default_preset_id, project.presets_snapshot)
            or store.builtins[0])


def effective_audio(project: Project, info: MediaInfo | None, clip: Clip) -> int | None:
    if clip.muted or info is None or not info.audios:
        return None
    rel = clip.audio_track if clip.audio_track is not None else project.default_audio_track
    if rel is None:
        rel = 0
    return max(0, min(rel, len(info.audios) - 1))


def effective_sub(project: Project, info: MediaInfo | None, clip: Clip) -> int | None:
    if not clip.burn_subs or info is None or not info.subs:
        return None
    rel = clip.sub_track if clip.sub_track is not None else project.default_sub_track
    if rel is None:
        rel = next((s.rel for s in info.subs if not s.is_bitmap), None)
    if rel is None or rel >= len(info.subs) or info.subs[rel].is_bitmap:
        return None
    return rel


def effective_crop(clip: Clip, info: MediaInfo | None = None, shorts: bool = False):
    """Shorts Mode: the clip's 9:16 frame (the crop tool is off); otherwise its crop, if any."""
    if shorts:
        v = info.video if info is not None else None
        return crop_rect(clip.shorts, v.width, v.height, v.sar) if v is not None else None
    if clip.crop_mode in ("auto", "manual") and clip.crop is not None and clip.crop.is_valid():
        return clip.crop
    return None


@dataclass
class ClipSummary:
    preset: Preset
    mode: str                     # smart cut | encode | gif | webp | re-encode (fallback)
    reasons: list[str] = field(default_factory=list)
    height: int | None = None     # final output height (None = source)
    notes: list[str] = field(default_factory=list)
    height_req: int | None = None


def summarize(project: Project, store: PresetStore, info: MediaInfo | None, clip: Clip, settings) -> ClipSummary:
    p = effective_preset(project, store, clip)
    if info is None or info.video is None:
        return ClipSummary(p, p.kind)
    shorts = bool(getattr(settings, "shorts_mode", False))
    crop = effective_crop(clip, info, shorts)
    src_w, src_h = source_dims(info, crop)
    req = requested_height(clip.target_height, p)
    h = clamp_height(req, src_h)
    sub = effective_sub(project, info, clip)
    reasons = reencode_reasons(sub is not None, crop, h)
    if shorts:
        reasons = [REASON_SHORTS if r == REASON_CROP else r for r in reasons]
    notes: list[str] = []
    if clip.burn_subs and sub is None:
        notes.append("no_text_subs")
    if p.kind in ("gif", "webp"):
        mode = p.kind
    elif p.mode == "smartcut":
        mode = "re-encode" if reasons else "smart cut"
    else:
        mode = "encode"
    if p.kind == "video" and p.target_size_mb and clip.is_complete:
        dur = max(0.04, (clip.out_time or 0) - clip.in_time)
        audio = 0 if effective_audio(project, info, clip) is None else (p.abitrate if p.acodec != "copy" else 192)
        kbps = size_video_kbps(p.target_size_mb, dur, audio)
        thr = bpp_threshold(p.vcodec, settings.bpp_x264, settings.bpp_hevc_av1)
        fps = float(info.fps)
        if p.allow_auto_downscale:
            ah, bpp = auto_height(src_w, src_h, fps, kbps, thr, p.min_auto_height, h or src_h)
            h = ah
            if bpp < thr:
                notes.append("low_bpp")
        else:
            hh = h or src_h
            if bits_per_pixel(kbps, scaled_width(hh, src_w, src_h), hh, fps) < thr:
                notes.append("low_bpp")
    return ClipSummary(p, mode, reasons, h, notes, req)
