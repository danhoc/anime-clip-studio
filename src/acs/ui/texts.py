"""Human-readable texts for reason / warning keys produced by the core."""

from __future__ import annotations

from acs.i18n import tr

REASONS = {
    "subs": "burned-in subtitles",
    "crop": "crop",
    "scale": "downscale",
}

WARNINGS = {
    "low_bpp": "Bitrate is very low for this resolution — expect visible artifacts",
    "size_not_reached": "Could not reach the target size within the allowed minimums",
    "hwaccel_fallback": "Hardware decoding failed, used software decoding",
    "webm_audio_opus": "WebM needs Opus audio — audio was re-encoded",
    "mp4_audio_compat": "This audio codec in MP4 may not play everywhere (consider AAC/Opus or MKV)",
    "smartcut_codec": "Smart cut supports only H.264/HEVC sources — fully re-encoded",
    "smartcut_no_idr_in_range": "No suitable keyframe inside the clip — fully re-encoded",
    "smartcut_no_full_gop": "Clip shorter than one GOP — fully re-encoded",
    "smartcut_too_short": "Clip too short for smart cut — fully re-encoded",
    "smartcut_gop_mismatch": "Unusual GOP structure — fully re-encoded",
    "smartcut_no_gop_info": "Keyframe index unavailable — fully re-encoded",
    "smartcut_validation": "Smart cut check failed — fully re-encoded with the same codec",
    "no_text_subs": "No text subtitle track to burn in",
}

MODES = {
    "smart cut": "smart cut",
    "re-encode": "re-encode",
    "encode": "encode",
    "gif": "GIF",
    "webp": "WebP",
}

STATUS = {
    "pending": "Waiting",
    "running": "Running",
    "done": "Done",
    "failed": "Failed",
    "cancelled": "Cancelled",
}


def reason_text(key: str) -> str:
    return tr(REASONS.get(key, key))


def warning_text(key: str) -> str:
    return tr(WARNINGS.get(key, key))


def mode_text(key: str) -> str:
    return tr(MODES.get(key, key))


def status_text(key: str) -> str:
    return tr(STATUS.get(key, key))
