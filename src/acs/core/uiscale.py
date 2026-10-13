"""Application UI scale (QT_SCALE_FACTOR), chosen in Settings or derived from the display.

Qt already honours the Windows display scaling; this is an *extra* factor for screens where the
UI still looks too small (e.g. a 4K monitor kept at 100 % Windows scaling). It must be applied
before the QApplication is created, so the "auto" value is computed with plain Win32 calls.
"""

from __future__ import annotations

import os
import sys

SCALE_CHOICES = ("auto", "1.0", "1.25", "1.5", "1.75", "2.0")


def display_metrics() -> tuple[int, int, float] | None:
    """(physical width, physical height, Windows scale e.g. 1.5) of the primary monitor."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        shcore = ctypes.WinDLL("shcore")

        class DEVMODEW(ctypes.Structure):
            _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                        ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD),
                        ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
                        ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
                        ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                        ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                        ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
                        ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
                        ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD),
                        ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
                        ("dmDisplayFlags", wintypes.DWORD), ("dmDisplayFrequency", wintypes.DWORD),
                        ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
                        ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                        ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                        ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]

        dm = DEVMODEW()
        dm.dmSize = ctypes.sizeof(DEVMODEW)
        ENUM_CURRENT_SETTINGS = -1
        if not user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
            return None
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        mon = user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)  # MONITOR_DEFAULTTOPRIMARY
        scale = ctypes.c_int(100)
        shcore.GetScaleFactorForMonitor.argtypes = [wintypes.HMONITOR, ctypes.POINTER(ctypes.c_int)]
        if shcore.GetScaleFactorForMonitor(mon, ctypes.byref(scale)) != 0:
            scale.value = 100
        return int(dm.dmPelsWidth), int(dm.dmPelsHeight), max(1.0, scale.value / 100)
    except (OSError, AttributeError, ValueError):
        return None


def auto_scale_for(phys_height: int, windows_scale: float) -> float:
    """Extra factor so the UI is comfortably large on high-resolution screens."""
    logical_h = phys_height / max(1.0, windows_scale)
    if logical_h >= 1900:   # e.g. 4K at 100 %
        return 1.5
    if logical_h >= 1350:   # e.g. 1440p at 100 %, 5K2K/4K at 150 %
        return 1.25
    return 1.0


def auto_scale() -> float:
    m = display_metrics()
    return 1.0 if m is None else auto_scale_for(m[1], m[2])


def resolve(choice: str) -> float:
    if choice in ("", "auto", None):
        return auto_scale()
    try:
        return max(0.5, min(3.0, float(choice)))
    except ValueError:
        return 1.0


THUMB_ROW_MAX = 150   # logical px of the timeline thumbnail row (see ui/timeline.py)


def auto_thumb_height(device_pixel_ratio: float) -> int:
    """Smallest thumbnail height that still looks sharp in the tallest thumbnail row."""
    from acs.core.thumbs import QUALITY_CHOICES
    need = THUMB_ROW_MAX * device_pixel_ratio * 0.75
    return next((h for h in QUALITY_CHOICES if h >= need), QUALITY_CHOICES[-1])


def auto_wave_bps(device_pixel_ratio: float) -> int:
    return 400 if device_pixel_ratio > 1.5 else 200


def thumb_height(choice: str, device_pixel_ratio: float) -> int:
    try:
        return int(choice)
    except (TypeError, ValueError):
        return auto_thumb_height(device_pixel_ratio)


def wave_bps(choice: str, device_pixel_ratio: float) -> int:
    try:
        return int(choice)
    except (TypeError, ValueError):
        return auto_wave_bps(device_pixel_ratio)


def apply_before_qapplication(choice: str) -> float:
    """Set QT_SCALE_FACTOR (unless the user already set it in the environment)."""
    if "QT_SCALE_FACTOR" in os.environ:
        try:
            return float(os.environ["QT_SCALE_FACTOR"])
        except ValueError:
            return 1.0
    factor = resolve(choice)
    if abs(factor - 1.0) > 1e-3:
        os.environ["QT_SCALE_FACTOR"] = f"{factor:g}"
    return factor
