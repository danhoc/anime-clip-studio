"""Rectangles over the picture (the Shorts frame, the manual crop): fractions <-> source pixels.

A rectangle is (x, y, w, h) as fractions of the picture width / height. An aspect ratio is the
*displayed* width / height (pixel aspect ratio applied), None = free.
"""

from __future__ import annotations

from acs.core.model import CropRect

Rect = tuple[float, float, float, float]

# Aspect ratios offered for the manual crop (free = any, source = the picture's own)
CROP_ASPECTS = ["free", "source", "16:9", "4:3", "1:1", "4:5", "9:16", "21:9"]


def parse_sar(sar: str | None) -> float:
    """'1:1' -> 1.0; unknown or invalid ('0:1', 'N/A', '') -> 1.0."""
    try:
        num, den = (float(x) for x in str(sar).split(":"))
        return num / den if num > 0 and den > 0 else 1.0
    except (ValueError, ZeroDivisionError):
        return 1.0


def display_aspect(width: int, height: int, sar: str | None = "1:1") -> float:
    """Width / height of the picture as it is shown (pixel aspect ratio applied)."""
    if width <= 0 or height <= 0:
        return 16 / 9
    return width * parse_sar(sar) / height


def parse_aspect(key: str | None, dar: float) -> float | None:
    """'16:9' -> 1.777…, 'source' -> the picture's own aspect, 'free' / unknown -> None."""
    if key == "source":
        return dar
    try:
        a, b = (float(x) for x in str(key).split(":"))
        return a / b if a > 0 and b > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def max_size(aspect: float | None, dar: float) -> tuple[float, float]:
    """(w, h) fractions of the largest rectangle with this aspect that fits the picture."""
    if not aspect:
        return 1.0, 1.0
    h = min(1.0, dar / aspect)
    return h * aspect / dar, h


def clamp_rect(r: Rect, dar: float, aspect: float | None = None, min_size: float = 0.05) -> Rect:
    """Size limited to [min_size, picture] (aspect enforced from the height when locked), then the
    rectangle moved fully inside the picture."""
    x, y, w, h = r
    mw, mh = max_size(aspect, dar)
    if aspect:
        h = min(max(h, min(min_size, mh)), mh)
        w = h * aspect / dar
    else:
        w = min(max(w, min_size), 1.0)
        h = min(max(h, min_size), 1.0)
    return min(max(x, 0.0), 1.0 - w), min(max(y, 0.0), 1.0 - h), w, h


def centered_max(aspect: float | None, dar: float) -> Rect:
    w, h = max_size(aspect, dar)
    return (1 - w) / 2, (1 - h) / 2, w, h


def fit_aspect_inside(r: Rect, aspect: float | None, dar: float) -> Rect:
    """The largest rectangle with `aspect` inside r, centred on it."""
    x, y, w, h = r
    if not aspect or w <= 0 or h <= 0:
        return r
    if w * dar / h > aspect:
        nw, nh = h * aspect / dar, h
    else:
        nw, nh = w, w * dar / aspect
    return x + (w - nw) / 2, y + (h - nh) / 2, nw, nh


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def from_crop(c: CropRect, width: int, height: int) -> Rect:
    return c.x / width, c.y / height, c.w / width, c.h / height


def to_crop(r: Rect, width: int, height: int, sar: str | None = "1:1", aspect: float | None = None) -> CropRect:
    """Source-pixel crop (even sizes and offsets, inside the picture) around the rectangle's centre;
    with an aspect the width follows from the height so the ratio is as exact as even sizes allow."""
    x, y, w, h = r
    max_w, max_h = width // 2 * 2, height // 2 * 2
    ph = min(_even(h * height), max_h)
    if aspect:
        sar_f = parse_sar(sar)
        pw = _even(ph * aspect / sar_f)
        if pw > max_w:
            pw = max_w
            ph = min(_even(pw * sar_f / aspect), max_h)
    else:
        pw = min(_even(w * width), max_w)
    cx, cy = (x + w / 2) * width, (y + h / 2) * height
    px = min(max(0, int(round((cx - pw / 2) / 2)) * 2), width - pw)
    py = min(max(0, int(round((cy - ph / 2) / 2)) * 2), height - ph)
    return CropRect(px, py, pw, ph)
