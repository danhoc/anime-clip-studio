"""Vector icons painted in the current theme's text colour (Qt's built-in media icons are dark grey
and disappear on a dark UI)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF

from acs.ui import theme

SIZE = 64  # drawn large, Qt scales down crisply for any DPI


def _pen(c: QColor, width: float) -> QPen:
    pen = QPen(c, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _tri(x0: float, y0: float, x1: float, y1: float, right: bool = True) -> QPolygonF:
    mid = (y0 + y1) / 2
    if right:
        return QPolygonF([QPointF(x0, y0), QPointF(x1, mid), QPointF(x0, y1)])
    return QPolygonF([QPointF(x1, y0), QPointF(x0, mid), QPointF(x1, y1)])


def _paint(kind: str, p: QPainter, c: QColor) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c)
    if kind == "play":
        p.drawPolygon(_tri(18, 12, 52, 52))
    elif kind == "pause":
        p.drawRoundedRect(QRectF(16, 12, 11, 40), 2, 2)
        p.drawRoundedRect(QRectF(37, 12, 11, 40), 2, 2)
    elif kind == "frame_next":
        p.drawPolygon(_tri(14, 14, 42, 50))
        p.drawRect(QRectF(44, 14, 6, 36))
    elif kind == "frame_prev":
        p.drawRect(QRectF(14, 14, 6, 36))
        p.drawPolygon(_tri(22, 14, 50, 50, right=False))
    elif kind == "ep_next":
        p.drawPolygon(_tri(8, 16, 30, 48))
        p.drawPolygon(_tri(30, 16, 52, 48))
        p.drawRect(QRectF(52, 16, 5, 32))
    elif kind == "ep_prev":
        p.drawRect(QRectF(7, 16, 5, 32))
        p.drawPolygon(_tri(12, 16, 34, 48, right=False))
        p.drawPolygon(_tri(34, 16, 56, 48, right=False))
    elif kind == "volume":
        p.drawPolygon(QPolygonF([QPointF(8, 24), QPointF(20, 24), QPointF(34, 12), QPointF(34, 52),
                                 QPointF(20, 40), QPointF(8, 40)]))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(_pen(c, 4))
        p.drawArc(QRectF(30, 20, 14, 24), -60 * 16, 120 * 16)
        p.drawArc(QRectF(30, 12, 24, 40), -60 * 16, 120 * 16)
    elif kind == "subtitles":
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(c, 4))
        p.drawRoundedRect(QRectF(6, 14, 52, 36), 6, 6)
        p.setPen(_pen(c, 4))
        p.drawLine(QPointF(14, 28), QPointF(30, 28))
        p.drawLine(QPointF(36, 28), QPointF(50, 28))
        p.drawLine(QPointF(14, 38), QPointF(40, 38))
    elif kind == "export":
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(_pen(c, 5))
        path = QPainterPath()
        path.moveTo(14, 30)
        path.lineTo(14, 52)
        path.lineTo(50, 52)
        path.lineTo(50, 30)
        p.drawPath(path)
        p.drawLine(QPointF(32, 40), QPointF(32, 8))
        p.drawLine(QPointF(32, 8), QPointF(22, 18))
        p.drawLine(QPointF(32, 8), QPointF(42, 18))
    elif kind == "frames":
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(c, 4))
        p.drawRoundedRect(QRectF(8, 12, 48, 40), 5, 5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(c)
        p.drawEllipse(QPointF(22, 25), 5, 5)
        p.drawPolygon(QPolygonF([QPointF(12, 48), QPointF(28, 32), QPointF(36, 40), QPointF(44, 30),
                                 QPointF(52, 48)]))
    else:
        raise ValueError(kind)


_cache: dict[tuple[str, str], QIcon] = {}


def icon(kind: str, color: str | None = None) -> QIcon:
    """Cached: e.g. play / pause is swapped on every pause change."""
    key = (kind, color or theme.current().text)
    cached = _cache.get(key)
    if cached is None:
        cached = _cache[key] = _render(kind, key[1])
    return cached


def _render(kind: str, color: str) -> QIcon:
    c = QColor(color)
    pm = QPixmap(SIZE, SIZE)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    _paint(kind, p, c)
    p.end()
    return QIcon(pm)
