"""Editable rectangle over the player: the Shorts frame (Shorts Mode) or the manual crop.

mpv draws it (an ASS drawing in an OSD overlay), so it is composited into the video like subtitles:
outside the rectangle the picture is dimmed, inside it stays as it is. The mouse is handled on the
player widget (mpv's own window lets mouse input through to it): drag inside to move, drag a corner
or an edge or turn the wheel to resize (a locked aspect ratio is kept), double-click to reset.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtWidgets import QWidget

from acs.core import framing

HANDLE_PX = 12          # logical px around a corner / an edge that grab it for resizing
HANDLE_DRAW_PX = 9      # drawn size of the corner handles
BORDER_PX = 2
WHEEL_STEP = 1.06       # size factor per wheel notch
WHEEL_COMMIT_MS = 500   # a burst of wheel steps becomes one undo entry
REDRAW_MS = 15          # mouse moves are merged into at most one OSD update per this interval
DIM = "&H000000&"       # ASS colours are &HBBGGRR&
DIM_ALPHA = "&H5A&"     # ASS alpha: &H00& opaque … &HFF& invisible
LINE = "&HFFFFFF&"
OPAQUE = "&H00&"

# hit areas: corners 0-3 (TL, TR, BR, BL), edges 4-7 (top, right, bottom, left)
_CURSORS = {0: Qt.CursorShape.SizeFDiagCursor, 2: Qt.CursorShape.SizeFDiagCursor,
            1: Qt.CursorShape.SizeBDiagCursor, 3: Qt.CursorShape.SizeBDiagCursor,
            4: Qt.CursorShape.SizeVerCursor, 6: Qt.CursorShape.SizeVerCursor,
            5: Qt.CursorShape.SizeHorCursor, 7: Qt.CursorShape.SizeHorCursor}


@dataclass(frozen=True)
class FrameTarget:
    kind: str                    # "shorts" | "crop"
    clip_id: str
    rect: framing.Rect           # x, y, w, h as fractions of the picture
    dar: float                   # display aspect of the picture
    aspect: float | None = None  # locked display aspect of the rectangle, None = free
    min_size: float = 0.05       # smallest size, fraction of the picture


@dataclass
class _Drag:
    hit: int                     # -1 = move, else a corner / edge (see above)
    press: QPointF
    start: QRectF                # rectangle in widget pixels at the press


def _box(x0: float, y0: float, x1: float, y1: float, color: str, alpha: str) -> str:
    x0, y0, x1, y1 = (int(round(v)) for v in (x0, y0, x1, y1))
    if x1 <= x0 or y1 <= y0:
        return ""
    return (rf"{{\an7\pos(0,0)\bord0\shad0\1c{color}\1a{alpha}\p1}}"
            f"m {x0} {y0} l {x1} {y0} l {x1} {y1} l {x0} {y1}" r"{\p0}")


class FrameOverlay(QObject):
    rect_edited = Signal(str, str, object)   # kind, clip id, Rect: a drag / wheel burst / reset finished

    def __init__(self, player: QWidget):
        super().__init__(player)
        self.player = player
        self._t: FrameTarget | None = None
        self._rect: framing.Rect | None = None   # current rectangle (differs from _t.rect while editing)
        self._drag: _Drag | None = None
        self._drawn = False
        self._redraw = QTimer(self)
        self._redraw.setSingleShot(True)
        self._redraw.setInterval(REDRAW_MS)
        self._redraw.timeout.connect(self._draw)
        self._wheel = QTimer(self)
        self._wheel.setSingleShot(True)
        self._wheel.setInterval(WHEEL_COMMIT_MS)
        self._wheel.timeout.connect(self._commit)
        player.installEventFilter(self)

    # ---- state from the main window --------------------------------------------------------
    def set_target(self, t: FrameTarget | None) -> None:
        """The rectangle to show and edit now (None: nothing)."""
        if self._editing():
            if t is not None and self._t is not None and (t.kind, t.clip_id) == (self._t.kind, self._t.clip_id):
                return   # the edit in progress is newer than the stored rectangle
            self._finish()
        if t == self._t:
            return
        self._t = t
        self._rect = t.rect if t is not None else None
        self.player.setMouseTracking(t is not None)
        self.player.unsetCursor()
        self._schedule()

    # ---- geometry (logical widget pixels) -------------------------------------------------
    def _video_rect(self) -> QRectF:
        w, h = self.player.width(), self.player.height()
        dar = self._t.dar if self._t is not None else 16 / 9
        if w <= 0 or h <= 0:
            return QRectF()
        if w / h > dar:
            vw, vh = h * dar, float(h)
        else:
            vw, vh = float(w), w / dar
        return QRectF((w - vw) / 2, (h - vh) / 2, vw, vh)

    def _px(self, r: framing.Rect) -> QRectF:
        v = self._video_rect()
        x, y, w, h = r
        return QRectF(v.x() + x * v.width(), v.y() + y * v.height(), w * v.width(), h * v.height())

    def _frac(self, r: QRectF) -> framing.Rect:
        v = self._video_rect()
        rect = ((r.x() - v.x()) / v.width(), (r.y() - v.y()) / v.height(), r.width() / v.width(),
                r.height() / v.height())
        return framing.clamp_rect(rect, self._t.dar, self._t.aspect, self._t.min_size)

    def _hit(self, pos: QPointF) -> int | None:
        r = self._px(self._rect)
        corners = [r.topLeft(), r.topRight(), r.bottomRight(), r.bottomLeft()]
        for i, c in enumerate(corners):
            if abs(pos.x() - c.x()) <= HANDLE_PX and abs(pos.y() - c.y()) <= HANDLE_PX:
                return i
        in_x = r.left() <= pos.x() <= r.right()
        in_y = r.top() <= pos.y() <= r.bottom()
        for i, near in ((4, in_x and abs(pos.y() - r.top()) <= HANDLE_PX / 2),
                        (5, in_y and abs(pos.x() - r.right()) <= HANDLE_PX / 2),
                        (6, in_x and abs(pos.y() - r.bottom()) <= HANDLE_PX / 2),
                        (7, in_y and abs(pos.x() - r.left()) <= HANDLE_PX / 2)):
            if near:
                return i
        return -1 if r.contains(pos) else None

    # ---- mouse ----------------------------------------------------------------------------
    def eventFilter(self, obj, ev) -> bool:
        if obj is not self.player:
            return False
        t = ev.type()
        if t == QEvent.Type.Resize:
            self._schedule()
            return False
        if self._t is None or self._rect is None:
            return False
        if t == QEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
            return self._press(ev.position())
        if t == QEvent.Type.MouseMove:
            if self._drag is not None:
                self._move(ev.position())
                return True
            self._hover(ev.position())
            return False
        if t == QEvent.Type.MouseButtonRelease and ev.button() == Qt.MouseButton.LeftButton:
            if self._drag is not None:
                self._move(ev.position())
                self._finish()
                return True
            return False
        if t == QEvent.Type.MouseButtonDblClick and ev.button() == Qt.MouseButton.LeftButton:
            if self._hit(ev.position()) is not None:
                self._rect = framing.centered_max(self._t.aspect, self._t.dar)
                self._schedule()
                self._commit()
                return True
            return False
        if t == QEvent.Type.Wheel and self._video_rect().contains(ev.position()):
            steps = ev.angleDelta().y() / 120
            if steps:
                self._zoom(WHEEL_STEP ** steps)
                self._wheel.start()
            return True
        return False

    def _press(self, pos: QPointF) -> bool:
        hit = self._hit(pos)
        if hit is None:
            return False
        if self._wheel.isActive():
            self._wheel.stop()
            self._commit()
        self._drag = _Drag(hit, pos, self._px(self._rect))
        if hit == -1:
            self.player.setCursor(Qt.CursorShape.ClosedHandCursor)
        return True

    def _move(self, pos: QPointF) -> None:
        d, v = self._drag, self._video_rect()
        if v.isEmpty():
            return
        s = d.start
        if d.hit == -1:
            self._rect = self._frac(s.translated(pos - d.press))
        elif d.hit < 4:
            self._rect = self._frac(self._resize_corner(d.hit, s, pos, v))
        else:
            self._rect = self._frac(self._resize_edge(d.hit, s, pos, v))
        self._schedule()

    def _min_px(self, v: QRectF) -> tuple[float, float]:
        return self._t.min_size * v.width(), self._t.min_size * v.height()

    def _resize_corner(self, i: int, s: QRectF, pos: QPointF, v: QRectF) -> QRectF:
        corners = [s.topLeft(), s.topRight(), s.bottomRight(), s.bottomLeft()]
        a, g = corners[(i + 2) % 4], corners[i]   # fixed opposite corner, grabbed corner
        sx = 1 if g.x() >= a.x() else -1
        sy = 1 if g.y() >= a.y() else -1
        dx, dy = (pos.x() - a.x()) * sx, (pos.y() - a.y()) * sy        # outward distances
        room_x = (v.right() - a.x()) if sx > 0 else (a.x() - v.left())
        room_y = (v.bottom() - a.y()) if sy > 0 else (a.y() - v.top())
        min_w, min_h = self._min_px(v)
        asp = self._t.aspect
        if asp:
            # the mouse projected onto the rectangle's diagonal through the anchor, as a height
            fh = (dx * asp + dy) / (asp * asp + 1)
            fh = max(min_h, min(fh, room_y, room_x / asp))
            fw = fh * asp
        else:
            fw = max(min_w, min(dx, room_x))
            fh = max(min_h, min(dy, room_y))
        return QRectF(a.x() if sx > 0 else a.x() - fw, a.y() if sy > 0 else a.y() - fh, fw, fh)

    def _resize_edge(self, e: int, s: QRectF, pos: QPointF, v: QRectF) -> QRectF:
        min_w, min_h = self._min_px(v)
        asp = self._t.aspect
        if e in (4, 6):   # top / bottom: the opposite edge stays
            fixed = s.bottom() if e == 4 else s.top()
            room = (fixed - v.top()) if e == 4 else (v.bottom() - fixed)
            fh = max(min_h, min(abs(pos.y() - fixed), room))
            fw = s.width()
            if asp:
                fh = min(fh, v.width() / asp)
                fw = fh * asp
            top = fixed - fh if e == 4 else fixed
            return QRectF(s.center().x() - fw / 2, top, fw, fh)
        fixed = s.right() if e == 7 else s.left()   # left / right
        room = (fixed - v.left()) if e == 7 else (v.right() - fixed)
        fw = max(min_w, min(abs(pos.x() - fixed), room))
        fh = s.height()
        if asp:
            fw = min(fw, v.height() * asp)
            fh = fw / asp
        left = fixed - fw if e == 7 else fixed
        return QRectF(left, s.center().y() - fh / 2, fw, fh)

    def _zoom(self, factor: float) -> None:
        x, y, w, h = self._rect
        mw, mh = framing.max_size(self._t.aspect, self._t.dar)
        f = min(factor, mw / w, mh / h)   # the whole rectangle grows evenly until one side is full
        nw, nh = w * f, h * f
        self._rect = framing.clamp_rect((x + (w - nw) / 2, y + (h - nh) / 2, nw, nh), self._t.dar,
                                        self._t.aspect, self._t.min_size)
        self._schedule()

    def _hover(self, pos: QPointF) -> None:
        hit = self._hit(pos)
        if hit is None:
            self.player.unsetCursor()
        else:
            self.player.setCursor(_CURSORS.get(hit, Qt.CursorShape.OpenHandCursor))

    # ---- committing -----------------------------------------------------------------------
    def _editing(self) -> bool:
        return self._drag is not None or self._wheel.isActive()

    def _finish(self) -> None:
        """End a drag or a wheel burst and report the rectangle."""
        drag, self._drag = self._drag, None
        if drag is not None:
            self.player.unsetCursor()
        moved = drag is not None and self._px(self._rect) != drag.start
        if moved or self._wheel.isActive():
            self._wheel.stop()
            self._commit()

    def _commit(self) -> None:
        if self._t is not None and self._rect is not None:
            self.rect_edited.emit(self._t.kind, self._t.clip_id, self._rect)

    # ---- drawing --------------------------------------------------------------------------
    def _schedule(self) -> None:
        if not self._redraw.isActive():
            self._redraw.start()

    def _draw(self) -> None:
        if self._t is None or self._rect is None:
            if self._drawn:
                self.player.set_osd_overlay(None)
                self._drawn = False
            return
        dpr = self.player.devicePixelRatioF()
        res_x, res_y = round(self.player.width() * dpr), round(self.player.height() * dpr)
        v, r = self._video_rect(), self._px(self._rect)
        vx0, vy0, vx1, vy1 = (c * dpr for c in (v.left(), v.top(), v.right(), v.bottom()))
        fx0, fy0, fx1, fy1 = (c * dpr for c in (r.left(), r.top(), r.right(), r.bottom()))
        b, hs = max(1.0, BORDER_PX * dpr), HANDLE_DRAW_PX * dpr
        parts = [
            # dimmed picture around the rectangle
            _box(vx0, vy0, vx1, fy0, DIM, DIM_ALPHA), _box(vx0, fy1, vx1, vy1, DIM, DIM_ALPHA),
            _box(vx0, fy0, fx0, fy1, DIM, DIM_ALPHA), _box(fx1, fy0, vx1, fy1, DIM, DIM_ALPHA),
            # border just inside the rectangle (always on screen, even at the picture edge)
            _box(fx0, fy0, fx1, fy0 + b, LINE, OPAQUE), _box(fx0, fy1 - b, fx1, fy1, LINE, OPAQUE),
            _box(fx0, fy0, fx0 + b, fy1, LINE, OPAQUE), _box(fx1 - b, fy0, fx1, fy1, LINE, OPAQUE),
            # corner handles
            _box(fx0, fy0, fx0 + hs, fy0 + hs, LINE, OPAQUE), _box(fx1 - hs, fy0, fx1, fy0 + hs, LINE, OPAQUE),
            _box(fx1 - hs, fy1 - hs, fx1, fy1, LINE, OPAQUE), _box(fx0, fy1 - hs, fx0 + hs, fy1, LINE, OPAQUE),
        ]
        self.player.set_osd_overlay("\n".join(p for p in parts if p), res_x, res_y)
        self._drawn = True
