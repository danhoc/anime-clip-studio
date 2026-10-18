"""Small static example pictures for the introduction tour.

On first start nothing is loaded, so the areas the tour points at are empty; each step shows what
they look like in use. Drawn with QPainter (no image files): sharp at any display scale, and the few
words in them are translated like the rest of the interface. Colours suit the tour's dark bubble.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

from acs.i18n import tr

W, H = 360, 150          # logical size of every picture
# Interface colours of the pictures, one set per theme (set by picture(); the helpers read them).
# The video, the phone and the mpv window keep their own colours in both themes.
_PALETTES = {
    "dark": dict(BG="#1b1f27", PANEL="#252a35", LINE="#3a4152", TEXT="#e8ecf3", DIM="#8a93a6", ACCENT="#4f9dff",
                 SEL="#2f4f7f", FIELD="#1b1f27", BUTTON="#323a4a", OK="#5cd17b"),
    "light": dict(BG="#f4f6f9", PANEL="#ffffff", LINE="#cdd3dc", TEXT="#1d2128", DIM="#6b717d", ACCENT="#2f6fd6",
                  SEL="#cfe0fa", FIELD="#ffffff", BUTTON="#e3e6eb", OK="#23914f"),
}
BG = PANEL = LINE = TEXT = DIM = ACCENT = SEL = FIELD = BUTTON = OK = QColor()


def _use_palette(name: str) -> None:
    globals().update({k: QColor(v) for k, v in _PALETTES.get(name, _PALETTES["dark"]).items()})


_use_palette("dark")
CLIP_COLORS = [QColor("#4f9dff"), QColor("#ff7a59"), QColor("#5cd17b")]
MARKER = QColor("#f5c542")
PLAYHEAD = QColor("#ff5c5c")


# ---- building blocks ---------------------------------------------------------------------------
def _font(p: QPainter, px: float, bold: bool = False, mono: bool = False) -> None:
    f = QFont("Consolas") if mono else QFont(p.font())
    f.setPixelSize(int(px))
    f.setBold(bold)
    p.setFont(f)


def _text(p: QPainter, r: QRectF, s: str, px: float = 11, color: QColor = TEXT, bold: bool = False,
          align=Qt.AlignmentFlag.AlignCenter, mono: bool = False) -> None:
    _font(p, px, bold, mono)
    p.setPen(color)
    p.drawText(r, int(align), s)


def _box(p: QPainter, r: QRectF, fill: QColor, border: QColor | None = None, radius: float = 4) -> None:
    p.setPen(QPen(border, 1) if border is not None else Qt.PenStyle.NoPen)
    p.setBrush(fill)
    p.drawRoundedRect(r, radius, radius)


def _scene(p: QPainter, r: QRectF, shift: float = 0.0) -> None:
    """A stand-in anime frame: sky, sun, mountains, meadow, a tree and a small figure."""
    p.save()
    clip = QPainterPath()
    clip.addRect(r)
    p.setClipPath(clip, Qt.ClipOperation.IntersectClip)
    sky = QLinearGradient(r.topLeft(), r.bottomLeft())
    sky.setColorAt(0, QColor("#5b8fe0"))
    sky.setColorAt(0.65, QColor("#f2c3a0"))
    p.fillRect(r, sky)
    x, y, w, h = r.x(), r.y(), r.width(), r.height()
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#fff1c4"))
    p.drawEllipse(QPointF(x + w * (0.72 - shift), y + h * 0.3), h * 0.11, h * 0.11)
    for color, pts in ((QColor("#6d7fb3"), [(0, .62), (.18, .38), (.36, .58), (.55, .32), (.78, .6), (1, .45), (1, .75), (0, .75)]),
                       (QColor("#46578c"), [(0, .7), (.25, .5), (.45, .68), (.7, .48), (1, .66), (1, .8), (0, .8)])):
        p.setBrush(color)
        p.drawPolygon(QPolygonF([QPointF(x + w * ((a - shift) % 1.0 if 0 < a < 1 else a), y + h * b) for a, b in pts]))
    p.setBrush(QColor("#4c9a5e"))
    p.drawRect(QRectF(x, y + h * 0.76, w, h * 0.24))
    tx = x + w * (0.2 + shift)
    p.setBrush(QColor("#5a3d2b"))
    p.drawRect(QRectF(tx - w * 0.012, y + h * 0.56, w * 0.024, h * 0.24))
    p.setBrush(QColor("#2f7a46"))
    p.drawEllipse(QPointF(tx, y + h * 0.52), w * 0.06, h * 0.13)
    fx = x + w * (0.52 + shift / 2)
    p.setBrush(QColor("#2b2f3a"))
    p.drawEllipse(QPointF(fx, y + h * 0.6), h * 0.045, h * 0.045)
    p.drawRoundedRect(QRectF(fx - h * 0.04, y + h * 0.65, h * 0.08, h * 0.17), 3, 3)
    p.restore()


def _keycap(p: QPainter, x: float, y: float, s: str, w: float = 0) -> float:
    _font(p, 10, bold=True)
    w = w or max(20.0, p.fontMetrics().horizontalAdvance(s) + 12)
    _box(p, QRectF(x, y, w, 18), PANEL, DIM, 3)
    _text(p, QRectF(x, y, w, 18), s, 10, TEXT, bold=True)
    return x + w + 4


def _arrow(p: QPainter, x0: float, x1: float, y: float) -> None:
    p.setPen(QPen(DIM, 2))
    p.drawLine(QPointF(x0, y), QPointF(x1 - 4, y))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(DIM)
    p.drawPolygon(QPolygonF([QPointF(x1, y), QPointF(x1 - 7, y - 5), QPointF(x1 - 7, y + 5)]))


def _subtitle_bars(p: QPainter, cx: float, bottom: float, widths: list[float], th: float) -> None:
    """Lines of subtitle 'text' (white bars with a dark outline)."""
    y = bottom - len(widths) * (th + 3)
    for w in widths:
        _box(p, QRectF(cx - w / 2, y, w, th), QColor("#ffffff"), QColor("#111111"), th / 2)
        y += th + 3


def _frame(p: QPainter, video: QRectF, f: QRectF) -> None:
    """Dimmed picture around f, white border and corner handles (as over the real player)."""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(0, 0, 0, 140))
    path = QPainterPath()
    path.addRect(video)
    hole = QPainterPath()
    hole.addRect(f)
    p.drawPath(path.subtracted(hole))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor("#ffffff"), 1.6))
    p.drawRect(f)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#ffffff"))
    hs = 5
    for c in (f.topLeft(), f.topRight() - QPointF(hs, 0), f.bottomLeft() - QPointF(0, hs),
              f.bottomRight() - QPointF(hs, hs)):
        p.drawRect(QRectF(c, QPointF(c.x() + hs, c.y() + hs)))


# ---- one picture per tour step -------------------------------------------------------------------
def _library(p: QPainter) -> None:
    panel = QRectF(8, 8, 196, 134)
    _box(p, panel, PANEL, LINE)
    _text(p, QRectF(16, 12, 180, 18), "📁  D:\\Anime\\Show", 11, TEXT, True, Qt.AlignmentFlag.AlignLeft)
    rows = [(f"Show_E0{i}.mkv", n) for i, n in ((1, 3), (2, 0), (3, 1), (4, 0), (5, 0))]
    for i, (name, n) in enumerate(rows):
        r = QRectF(12, 34 + i * 21, 188, 19)
        if i == 2:
            _box(p, r, SEL, None, 3)
        _text(p, r.adjusted(8, 0, 0, 0), name, 11, TEXT, align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if n:
            _text(p, r.adjusted(0, 0, -8, 0), f"● {n}", 11, OK, True,
                  Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    _arrow(p, 210, 236, 75)
    _scene(p, QRectF(242, 44, 110, 62))
    _box(p, QRectF(242, 44, 110, 62), Qt.GlobalColor.transparent, LINE, 0)


def _player(p: QPainter) -> None:
    _scene(p, QRectF(8, 6, 344, 96))
    bar = QRectF(8, 104, 344, 40)
    _box(p, bar, PANEL, LINE, 0)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(TEXT)
    cx, cy = 34, 124
    p.drawPolygon(QPolygonF([QPointF(cx - 6, cy - 8), QPointF(cx - 6, cy + 8), QPointF(cx + 8, cy)]))
    _text(p, QRectF(52, 112, 150, 24), "00:12:34.567 | 13/24", 12, TEXT, mono=True,
          align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    x = 210
    for k in ("Space", "← →"):
        x = _keycap(p, x, 115, k)


def _timeline(p: QPainter) -> None:
    _box(p, QRectF(8, 8, 344, 134), PANEL, LINE, 3)
    p.setPen(QPen(DIM, 1))
    for i in range(0, 34):
        x = 14 + i * 10
        p.drawLine(QPointF(x, 12), QPointF(x, 16 if i % 5 else 22))
    for i in range(6):   # thumbnails
        _scene(p, QRectF(14 + i * 56, 26, 54, 30), shift=i * 0.07)
    p.setPen(QPen(QColor("#6f87b5"), 1))   # waveform
    for i in range(0, 336, 3):
        a = 7 * abs(((i * 37) % 23) / 23 - 0.5) + 1
        p.drawLine(QPointF(14 + i, 68 - a), QPointF(14 + i, 68 + a))
    for (x0, x1), c, name in (((40, 130), CLIP_COLORS[0], "clip01"), ((180, 300), CLIP_COLORS[1], "clip02")):
        _box(p, QRectF(x0, 82, x1 - x0, 22), c, None, 3)
        _text(p, QRectF(x0, 82, x1 - x0, 22), name, 10, QColor("#10131a"), True)
    for x in (150, 320):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(MARKER)
        p.drawPolygon(QPolygonF([QPointF(x, 110), QPointF(x + 9, 113), QPointF(x, 116)]))
        p.setPen(QPen(MARKER, 1.5))
        p.drawLine(QPointF(x, 110), QPointF(x, 124))
    p.setPen(QPen(PLAYHEAD, 2))
    p.drawLine(QPointF(236, 10), QPointF(236, 126))
    x = 14
    for k in ("I", "O", "M", tr("Ctrl + wheel")):
        x = _keycap(p, x, 122, k)


def _clips(p: QPainter) -> None:
    _box(p, QRectF(8, 8, 344, 134), PANEL, LINE, 3)
    cols = [(14, "#"), (40, tr("Name")), (110, tr("In")), (180, tr("Out")), (250, tr("Mode"))]
    for x, t in cols:
        _text(p, QRectF(x, 12, 70, 18), t, 10, DIM, True, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    rows = [("clip01", "00:01:12", "00:01:20", tr("smart cut")), ("clip02", "00:04:03", "00:04:11", "ⓘ " + tr("encode")),
            ("clip03", "00:15:40", "00:15:52", "GIF")]
    for i, (name, a, b, mode) in enumerate(rows):
        y = 34 + i * 24
        if i == 1:
            _box(p, QRectF(12, y - 2, 336, 22), SEL, None, 3)
        _box(p, QRectF(14, y, 18, 18), CLIP_COLORS[i], None, 3)
        _text(p, QRectF(14, y, 18, 18), str(i + 1), 10, QColor("#10131a"), True)
        for x, t in ((40, name), (110, a), (180, b), (250, mode)):
            _text(p, QRectF(x, y, 100, 18), t, 11, TEXT, align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    _text(p, QRectF(14, 112, 330, 24), tr("Double-click clip") + " → " + tr("Jump to clip start"), 10, DIM,
          align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)


def _export(p: QPainter) -> None:
    b = QRectF(14, 54, 118, 32)
    _box(p, b, ACCENT, None, 5)
    _text(p, b, tr("Export clips"), 12, QColor("#ffffff"), True)
    _arrow(p, 140, 166, 70)
    rows = [("clip01.mp4", 1.0, QColor("#5cd17b")), ("clip02.mkv", 0.55, ACCENT), ("clip03.gif", 0.0, DIM)]
    for i, (name, frac, c) in enumerate(rows):
        y = 26 + i * 34
        _text(p, QRectF(174, y, 180, 14), name, 10, TEXT, align=Qt.AlignmentFlag.AlignLeft)
        _box(p, QRectF(174, y + 16, 172, 8), LINE, None, 4)
        if frac:
            _box(p, QRectF(174, y + 16, 172 * frac, 8), c, None, 4)


def _frames(p: QPainter) -> None:
    strip = QRectF(8, 30, 150, 90)
    _box(p, strip, QColor("#101217"), None, 2)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#3a4152"))
    for i in range(9):
        p.drawRect(QRectF(12 + i * 16, 33, 8, 5))
        p.drawRect(QRectF(12 + i * 16, 112, 8, 5))
    for i in range(3):
        _scene(p, QRectF(12 + i * 48, 42, 46, 66), shift=i * 0.12)
    _arrow(p, 166, 192, 75)
    for i in range(6):
        r = QRectF(200 + (i % 3) * 52, 32 + (i // 3) * 46, 48, 40)
        _scene(p, r, shift=i * 0.06)
        _box(p, r, Qt.GlobalColor.transparent, LINE, 0)
    _text(p, QRectF(200, 124, 156, 16), "PNG · JPEG · WebP", 10, DIM)


def _shorts(p: QPainter) -> None:
    video = QRectF(8, 18, 200, 112)
    _scene(p, video)
    f = QRectF(70, 18, 63, 112)    # 9:16 inside the 16:9 picture
    _frame(p, video, f)
    _subtitle_bars(p, f.center().x(), f.bottom() - 6, [50, 34], 5)
    _text(p, QRectF(8, 2, 200, 14), "16:9", 10, DIM)
    _arrow(p, 216, 250, 74)
    phone = QRectF(262, 6, 76, 138)
    _box(p, phone, QColor("#0d0f14"), DIM, 9)
    screen = QRectF(267, 12, 66, 117)
    p.save()
    p.setClipRect(screen)
    p.translate(screen.x() - f.x() * screen.width() / f.width(), screen.y())
    p.scale(screen.width() / f.width(), screen.height() / f.height())
    _scene(p, QRectF(video.x(), 0, video.width(), video.height()))
    p.restore()
    _subtitle_bars(p, screen.center().x(), screen.bottom() - 7, [54, 38], 5)
    _text(p, QRectF(262, 130, 76, 14), "9:16", 10, DIM)


def _crop(p: QPainter) -> None:
    video = QRectF(8, 8, 200, 112)
    _scene(p, video)
    _frame(p, video, QRectF(52, 18, 100, 100))   # 1:1
    x = 8
    for i, k in enumerate((tr("Free"), "16:9", "1:1", "9:16")):
        _font(p, 10, True)
        w = p.fontMetrics().horizontalAdvance(k) + 12
        _box(p, QRectF(x, 126, w, 18), ACCENT if i == 2 else PANEL, None if i == 2 else DIM, 9)
        _text(p, QRectF(x, 126, w, 18), k, 10, QColor("#ffffff") if i == 2 else TEXT, True)
        x += w + 4
    _box(p, QRectF(220, 8, 132, 136), PANEL, LINE, 4)
    _text(p, QRectF(226, 12, 120, 16), tr("Subtitle size"), 10, DIM, True)
    for y, px, pct in ((34, 13, "80 %"), (72, 20, "100 %"), (106, 28, "150 %")):
        _text(p, QRectF(230, y, 60, 34), "Aa", px, TEXT, True, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        _text(p, QRectF(290, y, 56, 34), pct, 11, DIM, align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)


def _help(p: QPainter) -> None:
    x = _keycap(p, 60, 66, "F1")
    _arrow(p, x + 6, 150, 75)
    win = QRectF(160, 12, 176, 126)
    _box(p, win, PANEL, LINE, 5)
    _text(p, QRectF(win.x() + 8, 16, 160, 16), tr("Quick help"), 11, TEXT, True, Qt.AlignmentFlag.AlignLeft)
    for i in range(6):
        _box(p, QRectF(win.x() + 10, 40 + i * 15, 26, 8), DIM, None, 2)
        _box(p, QRectF(win.x() + 44, 40 + i * 15, 110 - (i * 13) % 40, 8), LINE, None, 2)


def _elided(p: QPainter, r: QRectF, s: str, px: float, color: QColor, bold: bool = False) -> None:
    _font(p, px, bold)
    p.setPen(color)
    p.drawText(r, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
               p.fontMetrics().elidedText(s, Qt.TextElideMode.ElideRight, int(r.width())))


def _presets(p: QPainter) -> None:
    box = QRectF(8, 6, 344, 46)       # the "Project" box next to the player
    _box(p, box, PANEL, LINE, 4)
    _elided(p, QRectF(16, 8, 200, 14), tr("Project"), 10, DIM, True)
    _elided(p, QRectF(16, 26, 122, 20), tr("Default preset"), 10, TEXT)
    combo = QRectF(140, 26, 126, 20)
    _box(p, combo, FIELD, DIM, 3)
    _elided(p, combo.adjusted(6, 0, -14, 0), tr("H.264 x264 CRF 18 (max compatibility)"), 10, TEXT)
    _text(p, QRectF(combo.right() - 14, combo.y(), 12, 20), "▾", 10, DIM)
    b = QRectF(272, 26, 74, 20)
    _box(p, b, BUTTON, DIM, 3)
    _text(p, b, tr("Presets…"), 10, TEXT, True)
    p.setPen(QPen(DIM, 2))             # from the button down to the editor it opens
    p.drawLine(QPointF(309, 47), QPointF(309, 53))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(DIM)
    p.drawPolygon(QPolygonF([QPointF(309, 59), QPointF(305, 53), QPointF(313, 53)]))
    ed = QRectF(8, 60, 344, 84)        # the preset editor
    _box(p, ed, PANEL, LINE, 4)
    names = [tr("Original (smart cut, lossless middle)"), tr("HEVC x265 10-bit CRF 18"),
             tr("Discord 10 MB (x264, 2-pass)"), tr("GIF Medium (480p 15fps 128c)"), tr("My video preset")]
    for i, n in enumerate(names):
        r = QRectF(12, 64 + i * 15, 168, 14)
        if i == len(names) - 1:
            _box(p, r, SEL, None, 2)
        _elided(p, r.adjusted(4, 0, -2, 0), n, 9, TEXT if i == len(names) - 1 else DIM)
    fields = [(tr("Video codec"), "libx265"), (tr("Quality (CRF/CQ)"), "18"), (tr("Fit into size"), "10 MB")]
    for i, (label, value) in enumerate(fields):
        y = 66 + i * 22
        _elided(p, QRectF(188, y, 92, 18), label, 9, DIM)
        _box(p, QRectF(282, y, 64, 18), FIELD, LINE, 3)
        _elided(p, QRectF(287, y, 58, 18), value, 10, TEXT)


def _tools(p: QPainter) -> None:
    _box(p, QRectF(8, 8, 344, 134), PANEL, LINE, 4)
    rows = [("ffmpeg.exe", r"C:\FFmpeg\bin\…", True), ("ffprobe.exe", r"C:\FFmpeg\bin\…", True),
            ("libmpv-2.dll", r"vendor\…", True), (f"mpv.exe ({tr('optional')})", "", None)]
    for i, (name, path, ok) in enumerate(rows):
        y = 16 + i * 25
        _text(p, QRectF(16, y, 130, 20), name, 11, TEXT if ok else DIM, ok is not None,
              Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        _box(p, QRectF(150, y + 1, 110, 18), FIELD, LINE, 3)
        _text(p, QRectF(155, y + 1, 104, 18), path, 9, DIM, align=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        status, color = ("✔ OK", OK) if ok else ("–", DIM)
        _text(p, QRectF(266, y, 80, 20), status, 11, color, True, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    b = QRectF(16, 116, 130, 20)
    _box(p, b, BUTTON, DIM, 4)
    _text(p, b, tr("Auto-detect"), 11, TEXT, True)
    _text(p, QRectF(154, 116, 192, 20), "ffmpeg.org · mpv.io", 10, ACCENT,
          align=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)


def _mpv(p: QPainter) -> None:
    win = QRectF(8, 8, 150, 110)
    _box(p, win, QColor("#0d0f14"), LINE, 4)
    _scene(p, QRectF(12, 22, 142, 80))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#6b3fa0"))
    p.drawEllipse(QPointF(19, 15), 5, 5)
    _text(p, QRectF(28, 9, 120, 12), "mpv · acs_markers.lua", 9, DIM, align=Qt.AlignmentFlag.AlignLeft)
    x = 8
    for k in ("Alt+M", "Alt+I", "Alt+O"):
        x = _keycap(p, x, 124, k)
    _arrow(p, 166, 194, 62)
    tl = QRectF(200, 30, 152, 64)
    _box(p, tl, PANEL, LINE, 3)
    _box(p, QRectF(222, 56, 70, 18), CLIP_COLORS[0], None, 3)
    _text(p, QRectF(222, 56, 70, 18), "clip01", 10, QColor("#10131a"), True)
    for xx in (310, 330):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(MARKER)
        p.drawPolygon(QPolygonF([QPointF(xx, 44), QPointF(xx + 8, 47), QPointF(xx, 50)]))
        p.setPen(QPen(MARKER, 1.5))
        p.drawLine(QPointF(xx, 44), QPointF(xx, 86))
    _text(p, QRectF(200, 98, 152, 16), "Anime Clip Studio", 10, DIM)


ART = {"library": _library, "player": _player, "timeline": _timeline, "clips": _clips, "export": _export,
       "frames": _frames, "shorts": _shorts, "props": _crop, "presets": _presets, "tools": _tools, "mpv": _mpv, "help": _help}


def picture(key: str, dpr: float = 1.0, theme_name: str = "dark") -> QPixmap | None:
    draw = ART.get(key)
    if draw is None:
        return None
    _use_palette(theme_name)
    pm = QPixmap(int(W * dpr), int(H * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(BG)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    draw(p)
    p.end()
    return pm
