"""Help: keyboard cheat sheet (F1), the quick help shown at start and the first-run tour."""

from __future__ import annotations

import html

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QScrollArea,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from acs.i18n import tr
from acs.ui import theme, tour_art
from acs.ui.hotkeys import HOTKEYS, MOUSE_HINTS, key_for, step_amount


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None, texts: dict[str, str] | None = None):
        """texts: action id -> text that replaces the generic one (the arrow steps set in Settings)."""
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
            g.addChild(QTreeWidgetItem([key, (texts or {}).get(_aid) or tr(text)]))
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


def _step_pair(kind: str, value: float) -> str:
    if kind == "keyframe":
        return tr("Previous / next keyframe")
    return tr("Back / forward by {step}").format(step=step_amount(kind, value))


def _menu_path(*items: str) -> str:
    return " → ".join(tr(i).replace("&", "").rstrip("…") for i in items)


class QuickHelpDialog(QDialog):
    """Small window with the main keys and the things worth knowing, shown at every start until
    "Don't show at start" is ticked; always available from the Help menu."""

    closed = Signal(bool)              # True = do not show at start any more
    shortcuts_requested = Signal()

    def __init__(self, steps: dict[str, tuple[str, float]], show_at_start: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Quick help"))
        self.setModal(False)
        lay = QVBoxLayout(self)
        t = theme.current()
        rows = [
            (key_for("play"), tr("Play / pause")),
            ("← / →", _step_pair(*steps["arrow"])),
            ("Shift + ← / →", _step_pair(*steps["shift"])),
            ("Ctrl + ← / →", _step_pair(*steps["ctrl"])),
            ("↑ / ↓", tr("Previous / next marker or clip boundary")),
            (f"{key_for('set_in')} / {key_for('set_out')}", tr("Clip start / end (the current frame is included)")),
            (key_for("marker"), tr("Add marker")),
            (key_for("goto"), tr("Go to a time or frame number")),
            (key_for("loop"), tr("Play the selected clip in a loop (again to stop)")),
            (key_for("snapshot"), tr("Save the current frame as an image")),
            (key_for("delete"), tr("Delete selected clips / markers")),
            (f"{key_for('undo')} / {key_for('redo')}", tr("Undo / redo")),
            (tr("Ctrl + wheel"), tr("Zoom timeline around the cursor")),
            (key_for("zoom_clip"), tr("Zoom to selected clip")),
            (key_for("export"), tr("Export clips")),
            (key_for("help"), tr("All keyboard shortcuts")),
        ]
        cells = "".join(f"<tr><td style='padding:2px 14px 2px 0; white-space:nowrap'><b>{html.escape(k)}</b></td>"
                        f"<td style='padding:2px 0'>{html.escape(v)}</td></tr>" for k, v in rows)
        settings = _menu_path("&Tools", "Settings…")
        notes = [
            tr("New clips are exported with the project's default preset (H.264 CRF 18 unless you change it in "
               "the “{box}” box next to the player). “Original (smart cut)” keeps the source quality: only the "
               "frames around the cuts are re-encoded.").format(box=tr("Project")),
            tr("Work is saved automatically: reopen an episode and its clips come back (● in the Library). "
               "Ctrl+S saves the project as a file."),
            tr("Drag the playhead to an edge of a zoomed timeline to scroll it. Alt while dragging a clip edge "
               "turns snapping off."),
            tr("Arrow step sizes, theme, interface size and preview quality: {settings}. Language, theme and "
               "interface size apply after a restart.").format(settings=settings),
            tr("Shorts Mode ({menu}): every clip gets a 9:16 YouTube Shorts frame over the video. Drag it to move "
               "it, drag a corner or turn the mouse wheel to resize it, double-click it to reset it. Only the picture "
               "inside the frame is exported; burned-in subtitles are scaled to it and placed at its bottom. The "
               "crop tool is off meanwhile.").format(menu=_menu_path("&Tools", "Shorts Mode")),
            tr("Manual crop: pick an aspect ratio under “{crop}” and adjust the frame over the video the same way "
               "(with “{preview}” off). The size of burned-in subtitles is set per clip.").format(
                crop=tr("Crop"), preview=tr("Preview")),
            tr("{key} saves the current frame to the “frames” folder inside the output folder, in the format last "
               "chosen in “{frames}”; subtitles are drawn when the player shows them.").format(
                key=key_for("snapshot"), frames=_menu_path("&File", "Extract frames…")),
            tr("{key} plays the selected clip in a loop; selecting another clip moves the loop to it.").format(
                key=key_for("loop")),
            tr("ffmpeg / mpv paths: {tools}.").format(tools=_menu_path("&Tools", "External tools…")),
            tr("Updates: {menu} looks for a newer version on GitHub and installs it only after you agree.")
            .format(menu=_menu_path("&Help", "Check for updates…")),
        ]
        items = "".join(f"<li style='margin-bottom:4px'>{html.escape(n)}</li>" for n in notes)
        body = QLabel(f"<b>{html.escape(tr('Main keys'))}</b><table style='margin:4px 0 10px 0'>{cells}</table>"
                      f"<b>{html.escape(tr('Good to know'))}</b>"
                      f"<ul style='margin:4px 0 0 -20px; color:{t.info}'>{items}</ul>")
        body.setTextFormat(Qt.TextFormat.RichText)
        body.setWordWrap(True)
        body.setFixedWidth(520)
        lay.addWidget(body)
        row = QHBoxLayout()
        self.never = QCheckBox(tr("Don't show at start"))
        self.never.setChecked(not show_at_start)
        self.never.setToolTip(tr("The quick help stays available in Help → Quick help"))
        all_keys = QPushButton(tr("All shortcuts…"))
        all_keys.clicked.connect(self.shortcuts_requested.emit)
        close = QPushButton(tr("Close"))
        close.setDefault(True)
        close.clicked.connect(self.close)
        row.addWidget(self.never)
        row.addStretch(1)
        row.addWidget(all_keys)
        row.addWidget(close)
        lay.addLayout(row)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # finished also fires for the window's × and Esc (QDialog rejects on close)
        self.finished.connect(lambda _r: self.closed.emit(self.never.isChecked()))


TOUR_STEPS = [
    ("library", "Library", "Add folders with your episodes here. Double-click an episode to open it. "
                           "● marks episodes that already have saved clips."),
    ("player", "Player", "Space plays/pauses. ← / → jump one second, Shift+arrows step exactly one frame, "
                         "Ctrl+arrows jump between keyframes (the steps can be changed in Settings)."),
    ("timeline", "Timeline", "Ctrl+wheel zooms down to single frames, the wheel scrolls. Press I to start a clip "
                             "and O to end it — you can mark as many clips as you like. M adds a marker. Drag a "
                             "clip edge to move it, or right-click it and set it again with I / O. Ctrl+Z undoes."),
    ("clips", "Clips", "All clips of the episode. Double-click a clip to jump to it, double-click its name to "
                       "rename it. ⓘ explains why a clip will be re-encoded."),
    ("export", "Export", "Ctrl+E exports all clips at once. 'Original (smart cut)' keeps the source quality; "
                         "other presets re-encode with x264, x265, SVT-AV1, NVENC… or make GIF/WebP."),
    ("frames", "Frames", "Ctrl+Shift+E saves many frames: every N seconds or one per scene, "
                         "optionally skipping duplicates."),
    ("shorts", "Shorts Mode", "Tools → Shorts Mode: every clip gets a 9:16 frame for YouTube Shorts over the video. "
                              "Drag it to move it, drag a corner or turn the wheel to resize it. Only the picture "
                              "inside the frame is exported; burned-in subtitles are scaled to it and placed at its "
                              "bottom."),
    ("props", "Selected clip", "Settings of the selected clip: audio, burned-in subtitles and their size, crop, "
                               "resolution and preset. For a manual crop pick an aspect ratio and adjust the frame "
                               "right over the video."),
    ("presets", "Presets and encoding", "The “Project” box sets the default preset for all clips; a clip can pick "
                                        "its own under “Selected clip”. 'Original (smart cut)' keeps the source "
                                        "quality and re-encodes only the clip edges; the others re-encode with H.264, "
                                        "HEVC, AV1, VP9 (NVENC on NVIDIA cards) at a CRF quality or to a target file "
                                        "size, or make GIF / WebP. “Presets…” creates your own: duplicate a built-in "
                                        "preset or start a new video, GIF or WebP preset."),
    ("tools", "External tools", "Required: FFmpeg (ffmpeg + ffprobe — export, frames, analysis) and libmpv (the "
                                "video player). Tools → External tools… checks them; “Auto-detect” finds them in the "
                                "usual places and links to the official downloads are there. Nothing is downloaded "
                                "or installed automatically."),
    ("mpv", "Marks from mpv", "Optional: mark while watching in your own mpv player. In Tools → External tools… "
                              "choose mpv.exe and press “Install marker script into mpv”: Alt+M adds a marker, "
                              "Alt+I / Alt+O a clip start / end (the keys can be changed). When you open the "
                              "episode here, the marks are offered for import (also File → Import marks from mpv…)."),
    ("help", "Help", "F1 lists every key, Help → Quick help repeats the main ones."),
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
        t = theme.current()   # the bubble and its pictures follow the dark / light theme
        self.bubble.setStyleSheet(f"#bubble {{ background: {t.tooltip_bg}; border: 1px solid {t.accent};"
                                  f" border-radius: 8px; }} QLabel {{ color: {t.text}; }}")
        bl = QVBoxLayout(self.bubble)
        self.title = QLabel()
        f = self.title.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 2)
        self.title.setFont(f)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setFixedWidth(tour_art.W)
        # an example picture: on first start the highlighted areas are still empty (no video loaded)
        self.picture = QLabel()
        self.picture.setFixedSize(tour_art.W, tour_art.H)
        self.picture.setStyleSheet(f"border: 1px solid {t.border}; border-radius: 4px;")
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
        bl.addWidget(self.picture)
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
        # only the part inside every parent: a widget in a scroll area may be partly scrolled away
        # (clipped by the parents only; the widget's own children must not count as hiding it)
        r = QRect(w.mapToGlobal(QPoint(0, 0)), w.size())
        parent = w.parentWidget()
        while parent is not None:
            r = r.intersected(QRect(parent.mapToGlobal(QPoint(0, 0)), parent.size()))
            parent = parent.parentWidget()
        if r.isEmpty():
            return QRect()
        return r.translated(-self.mapToGlobal(QPoint(0, 0))).adjusted(-4, -4, 4, 4)

    def _reveal_target(self) -> None:
        """Scroll the step's widget into view when it sits in a scroll area (e.g. the "Project" box
        under the clip settings on small screens)."""
        w = self.targets.get(TOUR_STEPS[self.step][0])
        parent = w.parentWidget() if w is not None else None
        while parent is not None:
            if isinstance(parent, QScrollArea):
                parent.ensureWidgetVisible(w, 0, 0)
                return
            parent = parent.parentWidget()

    def _show_step(self) -> None:
        key, title, text = TOUR_STEPS[self.step]
        self._reveal_target()
        self.title.setText(f"{self.step + 1}/{len(TOUR_STEPS)} · {tr(title)}")
        self.text.setText(tr(text))
        pm = tour_art.picture(key, self.devicePixelRatioF(), theme.current().name)
        self.picture.setVisible(pm is not None)
        if pm is not None:
            self.picture.setPixmap(pm)
        self.b_next.setText(tr("Finish") if self.step == len(TOUR_STEPS) - 1 else tr("Next"))
        self.bubble.adjustSize()
        self.bubble.move(self._bubble_pos(self._target_rect()))
        self.update()

    def _bubble_pos(self, r: QRect) -> QPoint:
        """Below the highlighted area, else above, beside it, or inside it (large areas); centred
        when there is nothing to highlight."""
        bw, bh, m = self.bubble.width(), self.bubble.height(), 12
        W, H = self.width(), self.height()
        if r.isNull():
            return QPoint((W - bw) // 2, (H - bh) // 2)
        cx = min(max(10, r.center().x() - bw // 2), W - bw - 10)
        cy = min(max(10, r.center().y() - bh // 2), H - bh - 10)
        for x, y in ((cx, r.bottom() + m), (cx, r.top() - bh - m), (r.right() + m, cy), (r.left() - bw - m, cy)):
            if 10 <= x <= W - bw - 10 and 10 <= y <= H - bh - 10:
                return QPoint(x, y)
        return QPoint(cx, cy)

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
            p.setPen(QColor(theme.current().accent))
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
