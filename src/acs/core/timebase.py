"""Exact frame <-> time mapping.

Before the packet scan finishes we use a CFR estimate; afterwards the index is built from real
packet timestamps (handles VFR and odd start offsets) and also knows the GOP structure that
smart cut needs.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

import numpy as np

from acs.core.probe import PacketScan


@dataclass
class GopInfo:
    """Keyframes in decode order. All arrays have the same length."""

    key_frame: np.ndarray       # display frame index of the keyframe itself
    key_pkt: np.ndarray         # packet index (decode order) of the keyframe
    gop_start_frame: np.ndarray  # lowest display frame index decoded within this GOP (leading pictures)

    def __len__(self) -> int:
        return len(self.key_frame)


class FrameIndex:
    def __init__(self, times: np.ndarray, fps: Fraction, exact: bool, gops: GopInfo | None = None,
                 packet_count: int = 0):
        self.times = np.asarray(times, dtype=np.float64)
        self.fps = fps
        self.exact = exact
        self.gops = gops
        self.packet_count = packet_count
        self._fdur = 1.0 / float(fps) if fps else 1 / 24
        self._tol = min(0.002, 0.25 * self._fdur)
        if gops is not None and len(gops):
            self._sorted_keys = np.unique(gops.key_frame)
        else:
            self._sorted_keys = np.zeros(0, dtype=np.int64)
        # median distance between keyframes in seconds (None until the packet scan knows them)
        k = self._sorted_keys
        self.typical_gop = float(np.median(np.diff(self.times[k]))) if len(k) >= 2 else None

    # ---- construction -------------------------------------------------------------------
    @classmethod
    def cfr(cls, fps: Fraction, duration: float, first: float = 0.0) -> "FrameIndex":
        n = max(1, int(round((duration - first) * float(fps))))
        times = first + np.arange(n, dtype=np.float64) / float(fps)
        return cls(times, fps, exact=False)

    @classmethod
    def from_scan(cls, scan: PacketScan, fps: Fraction) -> "FrameIndex":
        if not scan.pts:
            raise ValueError("empty packet scan")
        pkt_pts = np.asarray(scan.pts, dtype=np.float64)
        # Round to microseconds so container rounding does not create phantom duplicates.
        pkt_pts = np.round(pkt_pts, 6)
        times = np.unique(pkt_pts)
        pkt_frame = np.searchsorted(times, pkt_pts)
        key_pkts = np.flatnonzero(np.asarray(scan.key, dtype=bool))
        key_frame = pkt_frame[key_pkts]
        gop_start = np.empty(len(key_pkts), dtype=np.int64)
        bounds = list(key_pkts) + [len(pkt_pts)]
        for i in range(len(key_pkts)):
            gop_start[i] = int(pkt_frame[bounds[i]:bounds[i + 1]].min())
        gops = GopInfo(key_frame.astype(np.int64), key_pkts.astype(np.int64), gop_start)
        return cls(times, fps, exact=True, gops=gops, packet_count=len(pkt_pts))

    # ---- queries ------------------------------------------------------------------------
    @property
    def frame_count(self) -> int:
        return len(self.times)

    @property
    def frame_duration(self) -> float:
        return self._fdur

    @property
    def end_time(self) -> float:
        return float(self.times[-1]) + self._fdur if len(self.times) else 0.0

    def clamp(self, f: int) -> int:
        return max(0, min(int(f), self.frame_count - 1))

    def time_of(self, f: int) -> float:
        """Presentation time of frame f. f == frame_count returns the end time (exclusive bound)."""
        if f >= self.frame_count:
            return self.end_time + (f - self.frame_count) * self._fdur
        if f < 0:
            return float(self.times[0]) + f * self._fdur
        return float(self.times[int(f)])

    def frame_at(self, t: float) -> int:
        """Frame displayed at time t (largest frame time <= t, with a small tolerance)."""
        i = int(np.searchsorted(self.times, t + self._tol, side="right")) - 1
        return self.clamp(i)

    def nearest_frame(self, t: float) -> int:
        i = int(np.searchsorted(self.times, t))
        if i <= 0:
            return 0
        if i >= self.frame_count:
            return self.frame_count - 1
        return i if (self.times[i] - t) < (t - self.times[i - 1]) else i - 1

    def frame_in_second(self, f: int) -> tuple[int, int]:
        """(1-based position of frame f within its whole second, number of frames in that second).

        Uses the real frame timestamps, so 23.976 fps shows 24 frames in most seconds and 23
        in the occasional one, VFR shows whatever the file really has — nothing is invented.
        """
        if self.frame_count == 0:
            return 0, 0
        f = self.clamp(f)
        sec = math.floor(float(self.times[f]) + 1e-6)
        eps = 1e-6
        first = int(np.searchsorted(self.times, sec - eps, side="left"))
        last = int(np.searchsorted(self.times, sec + 1 - eps, side="left"))
        return f - first + 1, max(1, last - first)

    def span_seconds(self, f_in: int, f_out: int) -> float:
        return self.time_of(f_out) - self.time_of(f_in)

    @property
    def keyframes(self) -> np.ndarray:
        return self._sorted_keys

    def is_keyframe(self, f: int) -> bool:
        k = self._sorted_keys
        i = int(np.searchsorted(k, f))
        return i < len(k) and int(k[i]) == f

    def prev_keyframe(self, f: int) -> int | None:
        k = self._sorted_keys
        i = int(np.searchsorted(k, f, side="left")) - 1
        return int(k[i]) if i >= 0 else None

    def next_keyframe(self, f: int) -> int | None:
        k = self._sorted_keys
        i = int(np.searchsorted(k, f, side="right"))
        return int(k[i]) if i < len(k) else None
