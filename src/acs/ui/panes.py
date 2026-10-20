"""Splitter panes that can be hidden completely, by dragging their edge to the window border or from
the View menu, and brought back with their last size."""

from __future__ import annotations

from PySide6.QtCore import QObject, QPoint, Qt, Signal
from PySide6.QtWidgets import QSplitter, QSplitterHandle


def pixel_step(dpr: float) -> int:
    """Smallest step in logical px that is a whole number of device pixels (125 % -> 4, 150 % -> 2)."""
    for k in range(1, 9):
        if abs(k * dpr - round(k * dpr)) < 1e-3:
            return k
    return 1


class _SnapHandle(QSplitterHandle):
    """Drags its splitter in steps that land on whole device pixels.

    At fractional display scaling Qt places a widget's edge at a rounded device pixel but scales its
    size, so the far edge of a pane whose near edge is dragged (e.g. the right ends of the combo
    boxes in the side column) jumped back and forth by a pixel on every step."""

    def __init__(self, orientation, parent):
        super().__init__(orientation, parent)
        self._offset = 0

    def _pick(self, p: QPoint) -> int:
        return p.x() if self.orientation() == Qt.Orientation.Horizontal else p.y()

    def mousePressEvent(self, ev):  # noqa: N802 - Qt API
        super().mousePressEvent(ev)
        if ev.button() == Qt.MouseButton.LeftButton:
            self._offset = self._pick(ev.position().toPoint())

    def mouseMoveEvent(self, ev):  # noqa: N802 - Qt API
        s = self.splitter()
        if not (ev.buttons() & Qt.MouseButton.LeftButton) or not s.opaqueResize():
            super().mouseMoveEvent(ev)
            return
        pos = self._pick(s.mapFromGlobal(ev.globalPosition().toPoint())) - self._offset
        k = pixel_step(self.devicePixelRatioF())
        if k > 1:
            # snap the start of the pane after the handle, in window coordinates
            origin = self._pick(s.mapTo(s.window(), QPoint(0, 0))) + s.handleWidth()
            pos = round((pos + origin) / k) * k - origin
        self.moveSplitter(pos)


class SnapSplitter(QSplitter):
    def createHandle(self) -> QSplitterHandle:  # noqa: N802 - Qt API
        return _SnapHandle(self.orientation(), self)


class PaneToggle(QObject):
    """Open / close pane `index` of a collapsible splitter the way dragging its handle does."""

    changed = Signal(bool)   # True = the pane is open

    def __init__(self, splitter: QSplitter, index: int, default_size: int, parent=None):
        super().__init__(parent or splitter)
        self._splitter = splitter
        self._index = index
        self._neighbour = index - 1 if index > 0 else 1   # the pane that gets / gives back the space
        self.size = default_size                          # restored when the pane is opened again
        self._open: bool | None = None
        splitter.setCollapsible(index, True)
        splitter.splitterMoved.connect(lambda *_: self.sync())

    def is_open(self) -> bool:
        return self._splitter.sizes()[self._index] > 0

    def set_open(self, on: bool) -> None:
        sizes = self._splitter.sizes()
        cur = sizes[self._index]
        if on == (cur > 0):
            return
        nb = self._neighbour
        if on:
            hint = self._splitter.widget(nb).minimumSizeHint()
            spare = sizes[nb] - (hint.height() if self._splitter.orientation() == Qt.Orientation.Vertical
                                 else hint.width())
            want = max(1, min(self.size, spare))
            sizes[self._index], sizes[nb] = want, sizes[nb] - want
        else:
            self.size = cur
            sizes[self._index], sizes[nb] = 0, sizes[nb] + cur
        self._splitter.setSizes(sizes)
        self.sync()

    def toggle(self) -> None:
        self.set_open(not self.is_open())

    def sync(self) -> None:
        """Remember the size and report open / closed changes (after a drag or setSizes)."""
        s = self._splitter.sizes()[self._index]
        if s > 0:
            self.size = s
        if (s > 0) != self._open:
            self._open = s > 0
            self.changed.emit(self._open)
