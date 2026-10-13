"""Shown after a crash: pick a snapshot / autosave to restore."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout)

from acs.core.recovery import RecoveryItem
from acs.ui import theme
from acs.i18n import tr

REASONS = {
    "autosave": "autosave",
    "before-export": "before export",
    "before-frames": "before frame extraction",
    "before-thumbs": "before thumbnails",
    "before-cropdetect": "before crop detection",
    "emergency": "emergency save on error",
}


class RecoveryDialog(QDialog):
    RESTORE, ORIGINAL, DISCARD = 1, 2, 3

    def __init__(self, items: list[RecoveryItem], crash_log: Path | None, lock: dict | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Recover your work"))
        self.resize(820, 420)
        self.items = items
        self.choice = self.DISCARD
        self.selected: RecoveryItem | None = None
        lay = QVBoxLayout(self)
        msg = tr("Anime Clip Studio did not close properly last time.") if lock else \
            tr("Saved work was found.")
        msg += " " + tr("You can restore a project from one of these automatic backups:")
        lab = QLabel(msg)
        lab.setWordWrap(True)
        lay.addWidget(lab)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("When"), tr("Type"), tr("Video"), tr("Clips"), tr("Markers")])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for it in items:
            row = QTreeWidgetItem([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(it.mtime)),
                                   tr(REASONS.get(it.reason, it.reason)), Path(it.video).name if it.video else "—",
                                   str(it.clips), str(it.markers)])
            row.setToolTip(2, it.video)
            row.setData(0, Qt.ItemDataRole.UserRole, it)
            self.tree.addTopLevelItem(row)
        if items:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        lay.addWidget(self.tree, 1)
        if crash_log is not None:
            cl = QLabel(tr("Crash details were saved to:") + f" {crash_log}")
            cl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            cl.setStyleSheet(theme.css_info())
            lay.addWidget(cl)
        row = QHBoxLayout()
        b_restore = QPushButton(tr("Restore selected"))
        b_restore.setDefault(True)
        self.b_orig = QPushButton(tr("Open the saved project file instead"))
        b_skip = QPushButton(tr("Start fresh"))
        row.addWidget(b_restore)
        row.addWidget(self.b_orig)
        row.addStretch(1)
        row.addWidget(b_skip)
        lay.addLayout(row)
        b_restore.clicked.connect(lambda: self._done(self.RESTORE))
        self.b_orig.clicked.connect(lambda: self._done(self.ORIGINAL))
        b_skip.clicked.connect(lambda: self._done(self.DISCARD))
        self.tree.currentItemChanged.connect(lambda *_: self._sel_changed())
        self.tree.itemDoubleClicked.connect(lambda *_: self._done(self.RESTORE))
        self._sel_changed()

    def _sel_changed(self) -> None:
        it = self.tree.currentItem()
        self.selected = it.data(0, Qt.ItemDataRole.UserRole) if it else None
        self.b_orig.setEnabled(bool(self.selected and self.selected.project_path
                                    and Path(self.selected.project_path).exists()))

    def _done(self, choice: int) -> None:
        self.choice = choice
        self.accept()
