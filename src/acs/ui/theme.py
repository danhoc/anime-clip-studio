"""Colour themes (palette + colours used by custom-painted widgets). Code that needs the kind of a
theme checks `Theme.dark`, not its name. Colours are "#rrggbb", or "rgba(r,g,b,a)" (read with `qcolor`)."""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass, replace

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QColor, QPalette


@dataclass(frozen=True)
class Theme:
    name: str
    window: str
    base: str
    alt_base: str
    text: str
    dim: str            # secondary text
    info: str           # informational text (hints, notes)
    button: str
    highlight: str
    tooltip_bg: str
    accent: str         # primary action buttons
    accent_text: str
    timeline_bg: str
    ruler_bg: str
    thumbs_bg: str
    wave_bg: str
    wave: str
    grid: str           # rgba
    transport_bg: str
    border: str
    empty_bg: str
    clip_label: str
    dark: bool = True   # dark surfaces (light text): picks native frames, tour pictures, contrast variants
    caption: str = ""   # Windows title bar colour ("" = window colour)


# Dark with blue-steel surfaces (after Discord's Retro Storm colours); the default theme.
STEEL = Theme(
    name="steel", window="#171c2a", base="#121725", alt_base="#1b2132", text="#dfe5f1", dim="#8592aa",
    info="#9fb6de", button="#222b3f", highlight="#3a5aa8", tooltip_bg="#1f2739", accent="#4a6ade",
    accent_text="#ffffff", timeline_bg="#131827", ruler_bg="#1d2333", thumbs_bg="#0e121d", wave_bg="#121726",
    wave="#7f9fd8", grid="rgba(255,255,255,22)", transport_bg="#161b29", border="#29324a", empty_bg="#0c0f19",
    clip_label="#ffffff", caption="#151a28",
)

# Steel blue in other hues: every colour turned to the new hue with the lightness re-fitted so that
# its perceived brightness equals the steel blue one, so text, buttons and selection keep exactly its
# contrast and calm. Dark is the near-neutral graphite version.
DARK = replace(
    STEEL, name="dark", window="#1b1c21", base="#16171c", alt_base="#1f2127", text="#e3e5e9", dim="#8d929a",
    info="#adb5c5", button="#282b32", highlight="#535d76", tooltip_bg="#25272d", accent="#6471a0",
    timeline_bg="#17181d", ruler_bg="#212328", thumbs_bg="#111216", wave_bg="#16171c", wave="#939fb5",
    transport_bg="#1a1b20", border="#30323a", empty_bg="#0e0f12", caption="#191a1f",
)

TEAL = replace(   # wave and hints a little less saturated: green-blue reads stronger than blue
    STEEL, name="teal", window="#131e21", base="#0f191c", alt_base="#162327", text="#d6e8eb", dim="#76979c",
    info="#8dbdc3", button="#1c2e31", highlight="#2c6571", tooltip_bg="#1a2a2d", accent="#247c99",
    timeline_bg="#0f1a1d", ruler_bg="#182528", thumbs_bg="#0b1416", wave_bg="#0e191c", wave="#63a9b1",
    transport_bg="#121d20", border="#22363a", empty_bg="#0a1012", caption="#111c1f",
)

INDIGO = replace(
    STEEL, name="indigo", window="#1e1a2c", base="#191527", alt_base="#231e34", text="#e5e3f2", dim="#928ead",
    info="#b6afe0", button="#2d2744", highlight="#624bb9", tooltip_bg="#29243d", accent="#7f5bd9",
    timeline_bg="#1a1629", ruler_bg="#252035", thumbs_bg="#14101f", wave_bg="#191428", wave="#9f96da",
    transport_bg="#1d192b", border="#352e4e", empty_bg="#110d1a", caption="#1c182a",
)

LIGHT = Theme(
    name="light", window="#eef0f3", base="#ffffff", alt_base="#f4f6f8", text="#1d2128", dim="#6b717d",
    info="#3b5f8f", button="#e3e6eb", highlight="#3d7be0", tooltip_bg="#ffffff", accent="#2f6fd6",
    accent_text="#ffffff", timeline_bg="#f7f8fa", ruler_bg="#e6e9ee", thumbs_bg="#dfe3e8", wave_bg="#eef1f4",
    wave="#4a6f9e", grid="rgba(0,0,0,26)", transport_bg="#e6e9ee", border="#c9ced6", empty_bg="#e9ecf0",
    clip_label="#10141a", dark=False,
)

# Light with warm sand surfaces; the golden #fcdd76 only on the primary button (with dark text), where
# a surface that saturated would glare.
SAND = Theme(
    name="sand", window="#f4ecd8", base="#fffcf3", alt_base="#faf4e6", text="#2a2416", dim="#6a6250",
    info="#6d5524", button="#ecdfbf", highlight="#86660e", tooltip_bg="#fffcf3", accent="#fcdd76",
    accent_text="#3a2e10", timeline_bg="#fbf6ea", ruler_bg="#efe4c8", thumbs_bg="#e8dcbc", wave_bg="#f6eedb",
    wave="#a07c2a", grid="rgba(0,0,0,26)", transport_bg="#efe4c8", border="#dccb9c", empty_bg="#f2e9d3",
    clip_label="#10141a", dark=False, caption="#efe4c8",
)

THEMES = {t.name: t for t in (STEEL, DARK, TEAL, INDIGO, LIGHT, SAND)}
# (settings value, English label) for the theme picker; "system" follows Windows (dark or light)
THEME_CHOICES = [("steel", "Steel blue"), ("dark", "Dark"), ("teal", "Deep teal"), ("indigo", "Indigo"),
                 ("light", "Light"), ("sand", "Sand"), ("system", "As in Windows")]

_current: Theme = DARK


def current() -> Theme:
    return _current


def qcolor(value: str) -> QColor:
    if value.startswith("rgba("):
        r, g, b, a = (int(x) for x in value[5:-1].split(","))
        return QColor(r, g, b, a)
    return QColor(value)


def resolve(name: str, app=None) -> Theme:
    if name in THEMES:
        return THEMES[name]
    if name == "system" and app is not None:
        try:
            if app.styleHints().colorScheme() == Qt.ColorScheme.Light:
                return LIGHT
        except AttributeError:
            pass
    return DARK


def apply(app, name: str) -> Theme:
    """Fusion style + palette + small global stylesheet. Call once before creating windows."""
    global _current
    t = resolve(name, app)
    _current = t
    if name in THEMES:
        # Also the native window frames (Windows title bars of the main window and the dialogs);
        # "system" keeps following the system setting. Set before the palette, which it must not reset.
        try:
            app.styleHints().setColorScheme(Qt.ColorScheme.Dark if t.dark else Qt.ColorScheme.Light)
        except AttributeError:   # Qt < 6.8
            pass
    app.setStyle("Fusion")
    app.setPalette(palette(t))
    app.setStyleSheet(
        f"QToolTip {{ border: 1px solid {t.accent}; padding: 4px; color: {t.text}; background: {t.tooltip_bg}; }}"
        f"QDockWidget::title {{ padding: 4px; background: {t.ruler_bg}; }}"
        f"QPushButton#primary {{ background: {t.accent}; color: {t.accent_text}; font-weight: bold;"
        f" border: none; border-radius: 4px; padding: 6px 14px; }}"
        f"QPushButton#primary:hover {{ background: {qcolor(t.accent).lighter(115).name()}; }}"
        f"QPushButton#primary:disabled {{ background: {t.button}; color: {t.dim}; }}"
    )
    return t


def palette(t: Theme) -> QPalette:
    p = QPalette()
    C = qcolor
    p.setColor(QPalette.ColorRole.Window, C(t.window))
    p.setColor(QPalette.ColorRole.WindowText, C(t.text))
    p.setColor(QPalette.ColorRole.Base, C(t.base))
    p.setColor(QPalette.ColorRole.AlternateBase, C(t.alt_base))
    p.setColor(QPalette.ColorRole.ToolTipBase, C(t.tooltip_bg))
    p.setColor(QPalette.ColorRole.ToolTipText, C(t.text))
    p.setColor(QPalette.ColorRole.Text, C(t.text))
    p.setColor(QPalette.ColorRole.Button, C(t.button))
    p.setColor(QPalette.ColorRole.ButtonText, C(t.text))
    p.setColor(QPalette.ColorRole.Highlight, C(t.highlight))
    p.setColor(QPalette.ColorRole.HighlightedText, C("#ffffff"))
    p.setColor(QPalette.ColorRole.Link, C(t.accent))
    p.setColor(QPalette.ColorRole.PlaceholderText, C(t.dim))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, C(t.dim))
    return p


CAPTION_DELAY_MS = 60


def _caption_colors(w) -> None:
    """Windows 11: title bar, its text and the window border in the theme's colours (DWM attributes
    34-36; older Windows ignore them and keep the dark / light frame)."""
    t = _current
    cap = qcolor(t.caption or t.window)
    try:
        hwnd = int(w.winId())
        set_attr = ctypes.windll.dwmapi.DwmSetWindowAttribute
        for attr, c in ((35, cap), (34, cap), (36, qcolor(t.text))):
            ref = ctypes.c_uint(c.red() | c.green() << 8 | c.blue() << 16)
            set_attr(ctypes.c_void_p(hwnd), attr, ctypes.byref(ref), ctypes.sizeof(ref))
    except (AttributeError, OSError):
        pass


class _CaptionFilter(QObject):
    def eventFilter(self, obj, ev):  # noqa: N802 - Qt API
        if (ev.type() == QEvent.Type.Show and obj.isWidgetType() and obj.isWindow()
                and obj.windowType() in (Qt.WindowType.Window, Qt.WindowType.Dialog)):
            _caption_colors(obj)
            # Qt sets the dark / light frame itself while the window appears, over ours: again after it
            QTimer.singleShot(CAPTION_DELAY_MS, obj, lambda w=obj: _caption_colors(w))
        return False


def install_caption_colors(app) -> None:
    """Colour the title bar of every window and dialog when it is shown."""
    if sys.platform == "win32":
        app._acs_caption_filter = _CaptionFilter(app)
        app.installEventFilter(app._acs_caption_filter)


def css_dim() -> str:
    return f"color: {_current.dim};"


def css_info() -> str:
    return f"color: {_current.info};"
