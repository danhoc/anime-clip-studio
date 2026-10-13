"""Dark / light colour themes (palette + colours used by custom-painted widgets)."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette

THEMES = ("dark", "light", "system")


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


DARK = Theme(
    name="dark", window="#23262c", base="#1e2127", alt_base="#262a31", text="#dfe3ea", dim="#8a909c",
    info="#9fb4d0", button="#2c3038", highlight="#3d6fb6", tooltip_bg="#2b3140", accent="#3d7be0",
    accent_text="#ffffff", timeline_bg="#1b1d22", ruler_bg="#24272e", thumbs_bg="#15171b", wave_bg="#191c20",
    wave="#4fb38f", grid="rgba(255,255,255,22)", transport_bg="#1d2026", border="#2c3038", empty_bg="#111317",
    clip_label="#ffffff",
)

LIGHT = Theme(
    name="light", window="#eef0f3", base="#ffffff", alt_base="#f4f6f8", text="#1d2128", dim="#6b717d",
    info="#3b5f8f", button="#e3e6eb", highlight="#3d7be0", tooltip_bg="#ffffff", accent="#2f6fd6",
    accent_text="#ffffff", timeline_bg="#f7f8fa", ruler_bg="#e6e9ee", thumbs_bg="#dfe3e8", wave_bg="#eef1f4",
    wave="#2f9a74", grid="rgba(0,0,0,26)", transport_bg="#e6e9ee", border="#c9ced6", empty_bg="#e9ecf0",
    clip_label="#10141a",
)

_current: Theme = DARK


def current() -> Theme:
    return _current


def qcolor(value: str) -> QColor:
    if value.startswith("rgba("):
        r, g, b, a = (int(x) for x in value[5:-1].split(","))
        return QColor(r, g, b, a)
    return QColor(value)


def resolve(name: str, app=None) -> Theme:
    if name == "light":
        return LIGHT
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
    app.setStyle("Fusion")
    p = QPalette()
    C = QColor
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
    app.setPalette(p)
    app.setStyleSheet(
        f"QToolTip {{ border: 1px solid {t.accent}; padding: 4px; color: {t.text}; background: {t.tooltip_bg}; }}"
        f"QDockWidget::title {{ padding: 4px; background: {t.ruler_bg}; }}"
        f"QPushButton#primary {{ background: {t.accent}; color: {t.accent_text}; font-weight: bold;"
        f" border: none; border-radius: 4px; padding: 6px 14px; }}"
        f"QPushButton#primary:hover {{ background: {qcolor(t.accent).lighter(115).name()}; }}"
        f"QPushButton#primary:disabled {{ background: {t.button}; color: {t.dim}; }}"
    )
    return t


def css_dim() -> str:
    return f"color: {_current.dim};"


def css_info() -> str:
    return f"color: {_current.info};"
