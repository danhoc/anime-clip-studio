"""Application-wide event filter that writes user input to the session trace."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QAbstractButton, QAbstractItemView, QDialog, QLabel, QMessageBox, QWidget

from acs.core import trace

_MOUSE = {QEvent.Type.MouseButtonPress: "press", QEvent.Type.MouseButtonRelease: "release",
          QEvent.Type.MouseButtonDblClick: "dblclick"}


def describe(w: QObject) -> str:
    if not isinstance(w, QWidget):
        return type(w).__name__
    parts = [type(w).__name__]
    if w.objectName():
        parts.append(f"#{w.objectName()}")
    if isinstance(w, QAbstractButton) and w.text():
        parts.append(f"'{w.text()}'")
    elif isinstance(w, QLabel) and w.text():
        parts.append(f"'{w.text()[:40]}'")
    win = w.window()
    if win is not None and win is not w and win.windowTitle():
        parts.append(f"in '{win.windowTitle()[:50]}'")
    return " ".join(parts)


class EventTracer(QObject):
    """Logs each input event once (Qt re-delivers unhandled input to parent widgets)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last: tuple | None = None

    def _once(self, ev) -> bool:
        key = (int(ev.type()), getattr(ev, "timestamp", lambda: 0)())
        if key == self._last:
            return False
        self._last = key
        return True

    def eventFilter(self, obj, ev):  # noqa: N802 - Qt API
        try:
            t = ev.type()
            if t in _MOUSE and isinstance(obj, QWidget) and self._once(ev):
                p = ev.position()
                extra = ""
                vp = obj.parent()
                if isinstance(vp, QAbstractItemView):
                    idx = vp.indexAt(p.toPoint())
                    if idx.isValid():
                        extra = f" item='{str(idx.data())[:60]}' row={idx.row()} col={idx.column()}"
                trace.log("input", f"mouse {_MOUSE[t]} {ev.button().name} at ({p.x():.0f},{p.y():.0f}) "
                                   f"mods={ev.modifiers().name} on {describe(obj)}{extra}")
            elif t == QEvent.Type.Wheel and isinstance(obj, QWidget) and self._once(ev):
                d = ev.angleDelta()
                trace.log("input", f"wheel dx={d.x()} dy={d.y()} mods={ev.modifiers().name} on {describe(obj)}")
            elif t == QEvent.Type.KeyPress and isinstance(obj, QWidget) and self._once(ev):
                seq = QKeySequence(ev.keyCombination()).toString()
                trace.log("input", f"key {seq or ev.text()!r}{' (repeat)' if ev.isAutoRepeat() else ''} "
                                   f"on {describe(obj)}")
            elif t == QEvent.Type.ContextMenu and isinstance(obj, QWidget) and self._once(ev):
                trace.log("input", f"context menu on {describe(obj)}")
            elif t == QEvent.Type.Drop and isinstance(obj, QWidget):
                urls = [u.toLocalFile() for u in ev.mimeData().urls()] if ev.mimeData().hasUrls() else []
                trace.log("input", f"drop {urls} on {describe(obj)}")
            elif t == QEvent.Type.Show and isinstance(obj, (QDialog, QMessageBox)):
                text = obj.text()[:200] if isinstance(obj, QMessageBox) else ""
                trace.log("ui", f"dialog shown: {type(obj).__name__} '{obj.windowTitle()}' {text!r}")
            elif t == QEvent.Type.Hide and isinstance(obj, (QDialog, QMessageBox)):
                trace.log("ui", f"dialog closed: {type(obj).__name__} '{obj.windowTitle()}'")
        except Exception as e:  # noqa: BLE001 - tracing must never break event delivery
            trace.log("trace", f"event filter error: {e!r}")
        return False
