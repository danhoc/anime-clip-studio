"""Layout that keeps its items in one row while they fit and wraps them onto more rows when not."""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem


class FlowLayout(QLayout):
    """Row of items that wraps instead of forcing a minimum width (e.g. the transport bar on
    1366 px screens). The last item stays right-aligned (free space goes before it), like after a
    stretch in a QHBoxLayout."""

    def __init__(self, parent=None, spacing: int = 4):
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._gap = spacing

    # ---- QLayout API ----
    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 - Qt API
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i: int) -> QLayoutItem | None:  # noqa: N802 - Qt API
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i: int) -> QLayoutItem | None:  # noqa: N802 - Qt API
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802 - Qt API
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt API
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt API
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt API
        super().setGeometry(rect)
        h = self._arrange(rect, apply=True)
        # Splitters ignore height-for-width: publish the wrapped height as the minimum so extra rows
        # are never clipped (it only depends on the width, so this settles after one pass).
        w = self.parentWidget()
        if w is not None and w.minimumHeight() != h:
            w.setMinimumHeight(h)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        items = self._visible()
        m = self.contentsMargins()
        w = sum(it.sizeHint().width() for it in items) + self._gap * max(0, len(items) - 1)
        h = max((it.sizeHint().height() for it in items), default=0)
        return QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    def minimumSize(self) -> QSize:  # noqa: N802 - Qt API
        items = self._visible()
        m = self.contentsMargins()
        w = max((it.minimumSize().width() for it in items), default=0)
        h = max((it.minimumSize().height() for it in items), default=0)
        return QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    # ---- layout ----
    def _visible(self) -> list[QLayoutItem]:
        return [it for it in self._items if not it.isEmpty()]

    def _arrange(self, rect: QRect, apply: bool) -> int:
        """Place the items (when `apply`) and return the height needed for `rect.width()`."""
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        rows: list[list[QLayoutItem]] = []
        row: list[QLayoutItem] = []
        used = 0
        for it in self._visible():
            w = it.sizeHint().width()
            if row and used + self._gap + w > area.width():
                rows.append(row)
                row, used = [], 0
            used += (self._gap if row else 0) + w
            row.append(it)
        if row:
            rows.append(row)
        y = area.y()
        for r in rows:
            h = max(it.sizeHint().height() for it in r)
            if apply:
                free = area.width() - sum(it.sizeHint().width() for it in r) - self._gap * (len(r) - 1)
                x = area.x()
                for k, it in enumerate(r):
                    if r is rows[-1] and k == len(r) - 1:
                        x += max(0, free)
                    s = it.sizeHint()
                    it.setGeometry(QRect(x, y + (h - s.height()) // 2, s.width(), s.height()))
                    x += s.width() + self._gap
            y += h + self._gap
        if rows:
            y -= self._gap
        return y - rect.y() + m.bottom()
