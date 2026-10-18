"""Shorts Mode geometry: the 9:16 frame of a clip, kept inside the picture, and its crop in pixels.

A frame is stored relative to the displayed picture (centre and height as fractions), so it does not
depend on the resolution; pixel sizes are derived for the preview and the export.
"""

from __future__ import annotations

from dataclasses import replace

from acs.core import framing
from acs.core.framing import display_aspect, parse_sar  # noqa: F401 - part of this module's API
from acs.core.model import SHORTS_ASPECT, CropRect, ShortsFrame

MIN_HEIGHT = 0.1   # smallest frame: a tenth of the picture height


def max_height(dar: float) -> float:
    """Largest frame height (fraction of the picture height) whose width still fits the picture."""
    return framing.max_size(SHORTS_ASPECT, dar)[1]


def clamp(frame: ShortsFrame, dar: float) -> ShortsFrame:
    """The frame resized to the allowed range and moved fully inside the picture."""
    h = min(max(frame.h, min(MIN_HEIGHT, max_height(dar))), max_height(dar))
    w = h * SHORTS_ASPECT / dar
    cx = min(max(frame.cx, w / 2), 1 - w / 2)
    cy = min(max(frame.cy, h / 2), 1 - h / 2)
    return replace(frame, cx=cx, cy=cy, h=h)


def fractions(frame: ShortsFrame, dar: float) -> framing.Rect:
    """(x, y, w, h) of the clamped frame as fractions of the picture width / height."""
    f = clamp(frame, dar)
    w = f.h * SHORTS_ASPECT / dar
    return f.cx - w / 2, f.cy - f.h / 2, w, f.h


def from_fractions(r: framing.Rect, dar: float) -> ShortsFrame:
    x, y, w, h = r
    return clamp(ShortsFrame(x + w / 2, y + h / 2, h), dar)


def crop_rect(frame: ShortsFrame, width: int, height: int, sar: str | None = "1:1") -> CropRect:
    """Crop of the source picture (in source pixels, even sizes and offsets) for this frame."""
    dar = display_aspect(width, height, sar)
    return framing.to_crop(fractions(frame, dar), width, height, sar, SHORTS_ASPECT)
