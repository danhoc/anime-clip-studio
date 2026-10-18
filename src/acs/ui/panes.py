"""Splitter panes that can be hidden completely, by dragging their edge to the window border or from
the View menu, and brought back with their last size."""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QSplitter


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
