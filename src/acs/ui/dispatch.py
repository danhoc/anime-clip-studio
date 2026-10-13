"""Thread-safe hand-off of results from worker threads to the GUI thread.

Worker threads never emit Qt signals carrying Python objects (numpy arrays, dataclasses): with
queued cross-thread connections PySide wraps them in PyObject holders whose release can race the
GIL and corrupt reference counts (crash inside python3xx.dll). Instead workers put a callable on a
plain queue.Queue and the GUI thread drains it with a QTimer.
"""

from __future__ import annotations

import logging
import queue
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer

from acs.core import trace

log = logging.getLogger("acs.dispatch")


class MainThreadDispatcher(QObject):
    def __init__(self, parent: QObject | None = None, interval_ms: int = 30):
        super().__init__(parent)
        self._q: queue.SimpleQueue[tuple[Callable[..., Any], tuple]] = queue.SimpleQueue()
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._drain)
        self._timer.start()

    def post(self, fn: Callable[..., Any], *args: Any) -> None:
        """Callable from any thread; `fn(*args)` runs later on the GUI thread."""
        self._q.put((fn, args))

    def _drain(self) -> None:
        for _ in range(200):  # bounded so a flood never freezes the UI
            try:
                fn, args = self._q.get_nowait()
            except queue.Empty:
                return
            name = getattr(fn, "__qualname__", repr(fn))
            trace.vlog("dispatch", f"-> {name}")
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001 - a failing callback must not stop the pump
                log.exception("dispatched callback failed")
                trace.log("dispatch", f"callback {name} raised {e!r}")
            trace.vlog("dispatch", f"<- {name}")

    def stop(self) -> None:
        self._timer.stop()
