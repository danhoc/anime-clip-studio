"""Audio waveform for the timeline: per-bucket peak levels of one audio track (uint8).

Resolution is configurable (points per second); audio is decoded at a sample rate that gives each
bucket enough samples, and peaks are computed while streaming so memory stays small.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from acs.core.settings import AppDirs
from acs.core.util import file_fingerprint, spawn

DEFAULT_BPS = 100            # buckets (peak values) per second = 10 ms
QUALITY_CHOICES = (100, 200, 400, 800)
# kept for older callers
BUCKETS_PER_SECOND = DEFAULT_BPS


def sample_rate_for(bps: int) -> int:
    """At least ~20 samples per bucket, capped at 16 kHz (plenty for a visual envelope)."""
    return int(min(16000, max(4000, bps * 20)) // bps * bps)


def cache_path(src: str, track: int, bps: int = DEFAULT_BPS) -> Path:
    suffix = "" if bps == DEFAULT_BPS else f".b{bps}"
    return AppDirs.media_cache() / f"{file_fingerprint(src)[:16]}.wave{track}{suffix}.npy"


def load_cached(src: str, track: int, bps: int = DEFAULT_BPS) -> np.ndarray | None:
    try:
        p = cache_path(src, track, bps)
        if p.exists():
            return np.load(p)
    except (OSError, ValueError):
        pass
    return None


def peaks_from_pcm(pcm: np.ndarray, bucket: int) -> np.ndarray:
    n = len(pcm) // bucket
    if n == 0:
        return np.zeros(0, dtype=np.uint8)
    a = np.abs(pcm[: n * bucket].astype(np.int32)).reshape(n, bucket).max(axis=1)
    # sqrt companding keeps quiet dialogue visible next to loud music
    return np.clip(np.sqrt(a / 32768.0) * 255, 0, 255).astype(np.uint8)


def generate(src: str, track: int, ffmpeg: str = "ffmpeg", cancel=None, bps: int = DEFAULT_BPS) -> np.ndarray | None:
    """Per-bucket peaks of an audio track (cached); None if cancelled."""
    cached = load_cached(src, track, bps)
    if cached is not None:
        return cached
    rate = sample_rate_for(bps)
    bucket = rate // bps
    args = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", src, "-map", f"0:a:{track}",
            "-vn", "-sn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "pipe:1"]
    p = spawn(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    assert p.stdout is not None
    chunk_bytes = bucket * 2 * 4096
    parts: list[np.ndarray] = []
    rest = b""
    while True:
        if cancel is not None and cancel():
            p.kill()
            p.wait()
            p.stdout.close()
            return None
        buf = p.stdout.read(chunk_bytes)
        if not buf:
            break
        buf = rest + buf
        usable = len(buf) // (bucket * 2) * (bucket * 2)
        rest = buf[usable:]
        if usable:
            parts.append(peaks_from_pcm(np.frombuffer(buf[:usable], dtype="<i2"), bucket))
    p.wait()
    p.stdout.close()
    if len(rest) >= 2:
        tail = np.frombuffer(rest[: len(rest) // 2 * 2], dtype="<i2")
        if len(tail):
            parts.append(peaks_from_pcm(np.pad(tail, (0, (-len(tail)) % bucket)), bucket))
    peaks = np.concatenate(parts) if parts else np.zeros(0, dtype=np.uint8)
    try:
        cp = cache_path(src, track, bps)
        cp.parent.mkdir(parents=True, exist_ok=True)
        tmp = cp.with_name(cp.stem + ".tmp.npy")
        np.save(tmp, peaks)
        tmp.replace(cp)
    except OSError:
        pass
    return peaks
