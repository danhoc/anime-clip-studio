"""Help: keyboard cheat sheet (F1) and the first-run tour."""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from acs.i18n import tr
from acs.ui.hotkeys import HOTKEYS, MOUSE_HINTS


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Keyboard shortcuts"))
        self.setModal(False)
        self.resize(560, 640)
        lay = QVBoxLayout(self)
        tree = QTreeWidget()
        tree.setHeaderLabels([tr("Keys"), tr("Action")])
        tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        groups: dict[str, QTreeWidgetItem] = {}
        for _aid, key, text, group in HOTKEYS:
            g = groups.get(group)
            if g is None:
                g = QTreeWidgetItem([tr(group), ""])
                f = g.font(0)
                f.setBold(True)
                g.setFont(0, f)
                tree.addTopLevelItem(g)
                groups[group] = g
            g.addChild(QTreeWidgetItem([key, tr(text)]))
        mouse = QTreeWidgetItem([tr("Mouse"), ""])
        f = mouse.font(0)
        f.setBold(True)
        mouse.setFont(0, f)
        tree.addTopLevelItem(mouse)
        for k, t in MOUSE_HINTS:
            mouse.addChild(QTreeWidgetItem([tr(k), tr(t)]))
        tree.expandAll()
        tree.resizeColumnToContents(0)
        lay.addWidget(tree)
        b = QPushButton(tr("Close"))
        b.clicked.connect(self.close)
        lay.addWidget(b)


TOUR_STEPS = [
    ("library", "Library", "Add folders with your episodes here. Double-click an episode to open it. "
                           "● marks episodes that already have saved clips."),
    ("player", "Player", "Space plays/pauses. ← / → step exactly one frame, Shift+arrows jump one second, "
                         "Ctrl+arrows jump between keyframes."),
    ("timeline", "Timeline", "Ctrl+wheel zooms down to single frames, the wheel scrolls. Press I to start a clip "
                             "and O to end it — you can mark as many clips as you like. M adds a marker. "
                             "Zoom and markers can be undone with Ctrl+Z."),
    ("clips", "Clips", "Every clip can have its own audio track, mute, burned-in subtitles, crop, resolution and "
                       "preset. ⓘ explains why a clip will be re-encoded."),
    ("export", "Export", "Ctrl+E exports all clips at once. 'Original (smart cut)' keeps the source quality; "
                         "other presets re-encode with x264, x265, SVT-AV1, NVENC… or make GIF/WebP."),
    ("frames", "Frames", "Ctrl+Shift+E saves many frames: every N seconds or one per scene, "
                         "optionally skipping duplicates."),
]


class TourOverlay(QWidget):
    """Top-level translucent overlay (stays above the native mpv window)."""

    finished = Signal(bool)   # True = do not show again

    def __init__(self, main_window, targets: dict[str, QWidget]):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.main = main_window
        self.targets = targets
        self.step = 0
        self.bubble = QWidget(self)
        self.bubble.setObjectName("bubble")
        self.bubble.setStyleSheet("#bubble { background: #2b3140; border: 1px solid #4f9dff; border-radius: 8px; }"
                                  "QLabel { color: #e8ecf3; }")
        bl = QVBoxLayout(self.bubble)
        self.title = QLabel()
        f = self.title.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 2)
        self.title.setFont(f)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setFixedWidth(360)
        self.never = QCheckBox(tr("Don't show again"))
        self.never.setChecked(True)
        row = QHBoxLayout()
        self.b_skip = QPushButton(tr("Skip"))
        self.b_next = QPushButton(tr("Next"))
        row.addWidget(self.never)
        row.addStretch(1)
        row.addWidget(self.b_skip)
        row.addWidget(self.b_next)
        bl.addWidget(self.title)
        bl.addWidget(self.text)
        bl.addLayout(row)
        self.b_skip.clicked.connect(self._finish)
        self.b_next.clicked.connect(self._next)
        self._show_step()

    def start(self) -> None:
        self.setGeometry(self.main.frameGeometry())
        self.show()
        self.raise_()
        self._show_step()

    def _target_rect(self) -> QRect:
        key = TOUR_STEPS[self.step][0]
        w = self.targets.get(key)
        if w is None or not w.isVisible():
            return QRect()
        top_left = w.mapToGlobal(w.rect().topLeft()) - self.mapToGlobal(self.rect().topLeft())
        return QRect(top_left, w.size()).adjusted(-4, -4, 4, 4)

    def _show_step(self) -> None:
        _, title, text = TOUR_STEPS[self.step]
        self.title.setText(f"{self.step + 1}/{len(TOUR_STEPS)} · {tr(title)}")
        self.text.setText(tr(text))
        self.b_next.setText(tr("Finish") if self.step == len(TOUR_STEPS) - 1 else tr("Next"))
        self.bubble.adjustSize()
        r = self._target_rect()
        if r.isNull():
            self.bubble.move((self.width() - self.bubble.width()) // 2, (self.height() - self.bubble.height()) // 2)
        else:
            x = min(max(10, r.center().x() - self.bubble.width() // 2), self.width() - self.bubble.width() - 10)
            y = r.bottom() + 12
            if y + self.bubble.height() > self.height() - 10:
                y = max(10, r.top() - self.bubble.height() - 12)
            self.bubble.move(x, y)
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRect(self.rect())
        r = self._target_rect()
        if not r.isNull():
            hole = QPainterPath()
            hole.addRoundedRect(r, 6, 6)
            path = path.subtracted(hole)
            p.setPen(QColor("#4f9dff"))
            p.drawRoundedRect(r, 6, 6)
        p.fillPath(path, QColor(0, 0, 0, 150))

    def _next(self) -> None:
        if self.step >= len(TOUR_STEPS) - 1:
            self._finish()
            return
        self.step += 1
        self._show_step()

    def _finish(self) -> None:
        self.finished.emit(self.never.isChecked())
        self.close()

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self._finish()
        elif ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Right, Qt.Key.Key_Space):
            self._next()
