"""Export / frames job queue view."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QProgressBar, QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from acs.core.jobs import CANCELLED, DONE, FAILED, JobQueue
from acs.i18n import tr
from acs.ui.texts import status_text, warning_text


class QueuePanel(QWidget):
    def __init__(self, queue: JobQueue, parent=None):
        super().__init__(parent)
        self.queue = queue
        self._items: dict[str, QTreeWidgetItem] = {}
        self._bars: dict[str, QProgressBar] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Job"), tr("Status"), tr("Progress"), tr("Details")])
        h = self.tree.header()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(0, 260)
        self.tree.setColumnWidth(2, 140)
        self.tree.setRootIsDecorated(False)
        self.tree.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        lay.addWidget(self.tree, 1)
        row = QHBoxLayout()
        self.b_cancel = QPushButton(tr("Cancel"))
        self.b_cancel.setToolTip(tr("Stop the selected job"))
        b_cancel_all = QPushButton(tr("Cancel all"))
        self.b_retry = QPushButton(tr("Retry"))
        self.b_retry.setToolTip(tr("Run a failed or cancelled job again"))
        b_clear = QPushButton(tr("Clear finished"))
        self.b_open = QPushButton(tr("Open folder"))
        self.b_open.setToolTip(tr("Show the output of the selected job in Explorer"))
        for b in (self.b_cancel, b_cancel_all, self.b_retry, b_clear):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.b_open)
        lay.addLayout(row)
        self.b_cancel.clicked.connect(self._cancel)
        b_cancel_all.clicked.connect(queue.cancel_all)
        self.b_retry.clicked.connect(self._retry)
        b_clear.clicked.connect(self._clear)
        self.b_open.clicked.connect(self._open)
        self.tree.itemDoubleClicked.connect(lambda *_: self._open())
        queue.job_added.connect(self._add)
        queue.job_updated.connect(self._update)
        queue.queue_changed.connect(self._rebuild)

    def _selected_id(self) -> str | None:
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def _add(self, job_id: str) -> None:
        job = self.queue.get(job_id)
        if job is None or job_id in self._items:
            return
        it = QTreeWidgetItem([job.title, "", "", ""])
        it.setData(0, Qt.ItemDataRole.UserRole, job_id)
        it.setToolTip(0, job.title)
        self.tree.addTopLevelItem(it)
        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setTextVisible(True)
        self.tree.setItemWidget(it, 2, bar)
        self._items[job_id] = it
        self._bars[job_id] = bar
        self._update(job_id)

    def _update(self, job_id: str) -> None:
        job = self.queue.get(job_id)
        it = self._items.get(job_id)
        if job is None or it is None:
            return
        it.setText(1, status_text(job.status))
        bar = self._bars[job_id]
        bar.setValue(int(job.progress * 1000))
        bar.setFormat(f"{job.progress * 100:.0f}%")
        details = job.message
        if job.status == FAILED:
            details = tr("Error") + ": " + job.message.splitlines()[-1] if job.message else tr("Error")
        warns = [warning_text(w) for w in job.warnings]
        extra = job.state.get("anim_params") or (f"{job.state['auto_height']}p" if job.state.get("auto_height")
                                                 else "")
        text = " · ".join(x for x in [details, extra] + warns if x)
        it.setText(3, text)
        it.setToolTip(3, "\n".join(x for x in [job.message, extra] + warns if x))
        color = {DONE: "#5cd17b", FAILED: "#ff6b6b", CANCELLED: "#a0a0a0"}.get(job.status)
        it.setForeground(1, QBrush(QColor(color)) if color else QBrush())  # a retried job loses its old colour
        if job.warnings and job.status == DONE:
            it.setText(1, status_text(job.status) + " ⚠")

    def _rebuild(self) -> None:
        live = {j.id for j in self.queue.jobs}
        for jid in list(self._items):
            if jid not in live:
                it = self._items.pop(jid)
                self._bars.pop(jid, None)
                idx = self.tree.indexOfTopLevelItem(it)
                if idx >= 0:
                    self.tree.takeTopLevelItem(idx)
        for j in self.queue.jobs:
            self._add(j.id)

    def _cancel(self) -> None:
        jid = self._selected_id()
        if jid:
            self.queue.cancel(jid)

    def _retry(self) -> None:
        jid = self._selected_id()
        if jid:
            self.queue.retry(jid)

    def _clear(self) -> None:
        self.queue.clear_finished()

    def _open(self) -> None:
        jid = self._selected_id()
        job = self.queue.get(jid) if jid else None
        if job is None:
            return
        target = job.output or job.spec.get("output") or job.spec.get("config", {}).get("out_dir", "")
        if not target:
            return
        p = Path(target)
        if p.is_file():
            subprocess.Popen(["explorer", "/select,", os.path.normpath(str(p))])
        elif p.is_dir():
            subprocess.Popen(["explorer", os.path.normpath(str(p))])
        elif p.parent.is_dir():
            subprocess.Popen(["explorer", os.path.normpath(str(p.parent))])