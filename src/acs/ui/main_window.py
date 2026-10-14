"""Main window: wires player, timeline, clips, library, queue, autosave and recovery together."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QFont, QFontMetrics, QKeySequence
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDockWidget, QFileDialog, QFrame, QHBoxLayout,
                               QLabel, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSlider, QSplitter,
                               QStackedWidget, QStyle, QToolButton, QUndoView, QVBoxLayout, QWidget)

from acs import APP_TITLE, __version__
from acs.core import mpv_markers, recovery, trace
from acs.core.atomic import read_json
from acs.core.clipinfo import effective_audio, effective_sub, pick_sub_track, pick_track
from acs.core.cropdetect import detect_crop
from acs.core.document import ProjectDocument
from acs.core.export import build_export_spec_dict, run_export
from acs.core.frames import run_frames
from acs.core.jobs import ACTIVE, Job, JobQueue
from acs.core.library import episode_neighbours
from acs.core.model import CropRect, Project
from acs.core.presets import CODECS, PresetStore
from acs.core.probe import ProbeError, probe, scan_packets
from acs.core.project_io import (PROJECT_EXT, ProjectLoadError, find_autosave, load_project, remap_frames,
                                 resolve_video, save_project, write_autosave)
from acs.core.timebase import FrameIndex
from acs.core.tools import quick_ok
from acs.core.util import file_fingerprint, fmt_time
from acs.i18n import tr
from acs.core import uiscale
from acs.ui import icons, theme
from acs.ui.clip_panel import ClipPanel
from acs.ui.dispatch import MainThreadDispatcher
from acs.ui.export_dialog import ExportDialog
from acs.ui.frames_dialog import FramesDialog
from acs.ui.help import ShortcutsDialog, TourOverlay
from acs.ui.hotkeys import key_for, with_key
from acs.ui.library_panel import LibraryPanel
from acs.ui.player import MpvPlayer
from acs.ui.preset_editor import PresetEditor
from acs.ui.queue_panel import QueuePanel
from acs.ui.recovery_dialog import RecoveryDialog
from acs.ui.settings_dialog import SettingsDialog
from acs.ui.tools_panel import ToolsDialog
from acs.ui.timeline import Timeline

log = logging.getLogger("acs.ui")

DOCK_STATE_VERSION = 2  # bump when the default dock arrangement changes


class MainWindow(QMainWindow):
    def __init__(self, settings, presets: PresetStore, mpv_log_file: str | None = None):
        super().__init__()
        self.settings = settings
        self.presets = presets
        self._mpv_log_file = mpv_log_file
        self.restart_requested = False
        self.doc = ProjectDocument(self)
        self.dispatch = MainThreadDispatcher(self)
        self.queue = JobQueue(settings, {"export": run_export, "frames": run_frames}, self,
                              post=self.dispatch.post)
        self._token = 0
        self._load_cancel: threading.Event | None = None
        self._wave_cancel: threading.Event | None = None
        self._opening = False
        self._pos = 0.0
        self._paused = True
        self._autosave_pending = False
        self._shortcuts_dlg: ShortcutsDialog | None = None
        self.setWindowTitle(APP_TITLE)
        self.resize(1500, 950)
        self.setAcceptDrops(True)
        self._build_ui()
        self._build_actions()
        self._wire()
        self._restore_geometry()
        self._update_title()
        self._update_labels()

    # =========================================================================================
    # UI construction
    # =========================================================================================
    def _build_ui(self) -> None:
        # ---- player + empty state ----
        self.player_stack = QStackedWidget()
        self.empty = QLabel(tr("Add a folder with videos in the Library (left)\n"
                               "or drag a video / .acsproj file here.\n\n"
                               "F1 — keyboard shortcuts"))
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t = theme.current()
        self.empty.setStyleSheet(f"color: {t.dim}; font-size: 15px; background: {t.empty_bg};")
        self.player_stack.addWidget(self.empty)
        self.player: MpvPlayer | None = None
        try:
            self.player = MpvPlayer(self.settings.libmpv, self.settings.hwdec, self._mpv_log_file)
            self.player_stack.addWidget(self.player)
        except Exception as e:  # noqa: BLE001 - show the reason instead of crashing
            log.exception("libmpv init failed")
            self.empty.setText(tr("The video player (libmpv) could not be started:") + f"\n{e}\n\n"
                               + tr("Check the libmpv-2.dll path in Settings."))

        # ---- transport bar ----
        bar = QWidget()
        bar.setObjectName("transport")
        c_in, c_out, c_mark = (("#3fb36b", "#e5534b", "#d4a72c") if t.name == "dark"
                               else ("#1f8a4c", "#c0392b", "#9a6a00"))
        bar.setStyleSheet(f"#transport {{ background: {t.transport_bg}; border-top: 1px solid {t.border}; }}"
                          "#transport QToolButton { padding: 4px 6px; }"
                          "#transport QToolButton#mark_in, #transport QToolButton#mark_out,"
                          "#transport QToolButton#mark_marker { padding: 4px 10px; border-radius: 4px; }"
                          f"#transport QToolButton#mark_in {{ color: {c_in}; border: 1px solid {c_in}; }}"
                          f"#transport QToolButton#mark_out {{ color: {c_out}; border: 1px solid {c_out}; }}"
                          f"#transport QToolButton#mark_marker {{ color: {c_mark}; border: 1px solid {c_mark}; }}")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(6, 4, 6, 4)
        bl.setSpacing(4)

        def tb(text: str, tip: str, action_id: str = "", icon_kind: str | None = None, bold: bool = False,
               name: str = "") -> QToolButton:
            b = QToolButton()
            if name:
                b.setObjectName(name)
            if icon_kind is not None:
                b.setIcon(icons.icon(icon_kind))
                b.setIconSize(QSize(22, 22))
            else:
                b.setText(text)
                f = b.font()
                f.setPointSize(f.pointSize() + 1)
                f.setBold(bold)
                b.setFont(f)
            b.setToolTip(with_key(tip, action_id) if action_id else tr(tip))
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setAutoRaise(True)
            bl.addWidget(b)
            return b

        self.b_ep_prev = tb("", "Previous episode in folder", "ep_prev", "ep_prev")
        self.b_sec_prev = tb("−1s", "Back 1 second", "sec_prev")
        self.b_frame_prev = tb("", "Previous frame", "frame_prev", "frame_prev")
        self.b_play = tb("", "Play / pause", "play", "play")
        self.b_play.setIconSize(QSize(30, 30))
        self.b_frame_next = tb("", "Next frame", "frame_next", "frame_next")
        self.b_sec_next = tb("+1s", "Forward 1 second", "sec_next")
        self.b_ep_next = tb("", "Next episode in folder", "ep_next", "ep_next")
        bl.addSpacing(12)
        mono = "font-family: Consolas, 'Cascadia Mono', monospace; font-size: 17px;"
        self.l_time = QLabel("--:--:--.--- / --:--:--")
        self.l_time.setStyleSheet(mono)
        self.l_time.setToolTip(tr("Current time / duration"))
        self.l_frame = QLabel("#—")
        self.l_frame.setStyleSheet(mono + f" color: {t.info};")
        self.l_frame.setToolTip(tr("Frame within the current second / frames in that second · "
                                   "#absolute frame number · K = keyframe"))
        bl.addWidget(self.l_time)
        bl.addSpacing(6)
        bl.addWidget(self.l_frame)
        # Fixed widths so the buttons on the right never move while the text changes
        # (e.g. when " K" appears on keyframes or the frame number gains a digit).
        mono_font = QFont()
        mono_font.setFamilies(["Consolas", "Cascadia Mono", "Courier New"])
        mono_font.setPixelSize(17)
        fm = QFontMetrics(mono_font)
        self.l_time.setFixedWidth(fm.horizontalAdvance("00:00:00.000 | 000/000  /  00:00:00") + 6)
        self.l_frame.setFixedWidth(fm.horizontalAdvance("#00000000 K") + 6)
        for lab in (self.l_time, self.l_frame):
            lab.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        bl.addSpacing(16)
        self.b_in = tb("[  " + tr("Clip start") + "  I", "Set clip In (starts a new clip)", "set_in",
                       bold=True, name="mark_in")
        self.b_out = tb(tr("Clip end") + "  O  ]", "Set clip Out (current frame included)", "set_out",
                        bold=True, name="mark_out")
        self.b_marker = tb("⚑ " + tr("Marker") + "  M", "Add marker", "marker", bold=True, name="mark_marker")
        bl.addStretch(1)
        self.l_speed = QLabel("")
        bl.addWidget(self.l_speed)
        self.c_aid = QComboBox()
        self.c_aid.setToolTip(tr("Audio track for preview"))
        self.c_aid.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.c_sid = QComboBox()
        self.c_sid.setToolTip(tr("Subtitles shown in the preview"))
        self.c_sid.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

        def icon_label(kind: str, tip: str) -> QLabel:
            lab = QLabel()
            lab.setPixmap(icons.icon(kind).pixmap(QSize(20, 20)))
            lab.setToolTip(tr(tip))
            return lab

        bl.addWidget(icon_label("volume", "Audio track for preview"))
        bl.addWidget(self.c_aid)
        bl.addWidget(icon_label("subtitles", "Subtitles shown in the preview"))
        bl.addWidget(self.c_sid)
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 130)
        self.vol.setValue(80)
        self.vol.setFixedWidth(90)
        self.vol.setToolTip(tr("Preview volume"))
        self.vol.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        bl.addWidget(self.vol)
        bl.addSpacing(14)
        self.b_frames = QPushButton(icons.icon("frames"), tr("Frames…"))
        self.b_frames.setToolTip(with_key("Extract frames", "frames"))
        self.b_frames.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        bl.addWidget(self.b_frames)
        self.b_export = QPushButton(icons.icon("export", t.accent_text), tr("Export clips"))
        self.b_export.setObjectName("primary")
        self.b_export.setToolTip(with_key("Export clips", "export"))
        self.b_export.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.b_export.setIconSize(QSize(18, 18))
        bl.addWidget(self.b_export)

        player_area = QWidget()
        tl = QVBoxLayout(player_area)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(0)
        tl.addWidget(self.player_stack, 1)
        tl.addWidget(bar)

        # ---- timeline + zoom bar ----
        self.timeline = Timeline(self.doc)
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            self.timeline.configure_for_screen(screen.availableGeometry().height())
        tlw = QWidget()
        tll = QVBoxLayout(tlw)
        tll.setContentsMargins(0, 0, 0, 0)
        tll.setSpacing(0)
        zb = QHBoxLayout()
        zb.setContentsMargins(4, 0, 4, 0)
        for text, tip, aid, fn in (("−", "Zoom timeline out", "zoom_out", lambda: self.timeline.zoom_at(1 / 1.5)),
                                   ("+", "Zoom timeline in (also Ctrl+wheel)", "zoom_in",
                                    lambda: self.timeline.zoom_at(1.5)),
                                   ("⤢", "Fit whole video", "zoom_fit", self.timeline.zoom_fit),
                                   ("◫", "Zoom to selected clip", "zoom_clip", self._zoom_clip)):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(with_key(tip, aid))
            b.setAutoRaise(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(fn)
            zb.addWidget(b)
        self.l_keys = QLabel("")
        self.l_keys.setStyleSheet(theme.css_dim())
        zb.addWidget(self.l_keys)
        zb.addStretch(1)
        tll.addLayout(zb)
        tll.addWidget(self.timeline)

        self.clip_panel = ClipPanel(self.doc, self.presets, self.settings)

        # Selected-clip properties live in a side column next to the player: the video keeps its
        # aspect ratio anyway, so this uses what would otherwise be black bars.
        props_scroll = QScrollArea()
        props_scroll.setWidget(self.clip_panel.props)
        props_scroll.setWidgetResizable(True)
        props_scroll.setFrameShape(QFrame.Shape.NoFrame)
        props_scroll.setMinimumWidth(320)
        self.top_split = QSplitter(Qt.Orientation.Horizontal)
        self.top_split.addWidget(player_area)
        self.top_split.addWidget(props_scroll)
        self.top_split.setStretchFactor(0, 4)
        self.top_split.setStretchFactor(1, 1)
        self.top_split.setCollapsible(0, False)

        # Player / timeline / clip table. The timeline's thumbnail and waveform rows have a fixed
        # height and sit right under the transport bar; dragging the handle below the timeline only
        # grows or shrinks its clip-lane area, so no empty gap can open between video and timeline.
        self.clip_panel.setMinimumHeight(140)
        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.addWidget(self.top_split)
        self.split.addWidget(tlw)
        self.split.addWidget(self.clip_panel)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 0)
        self.split.setStretchFactor(2, 1)
        for i in range(3):
            self.split.setCollapsible(i, False)
        self._timeline_block = tlw
        self.setCentralWidget(self.split)
        QTimer.singleShot(0, self._initial_split_sizes)

        # ---- docks ----
        self.library = LibraryPanel(self.settings)
        self.d_library = QDockWidget(tr("Library"), self)
        self.d_library.setObjectName("dock_library")
        self.d_library.setWidget(self.library)
        # Library, export queue and history share one tabbed side panel (Ctrl+L toggles the library).
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.d_library)
        self.queue_panel = QueuePanel(self.queue)
        self.d_queue = QDockWidget(tr("Export queue"), self)
        self.d_queue.setObjectName("dock_queue")
        self.d_queue.setWidget(self.queue_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.d_queue)
        self.undo_view = QUndoView(self.doc.undo)
        self.undo_view.setEmptyLabel(tr("<opened>"))
        self.d_history = QDockWidget(tr("History"), self)
        self.d_history.setObjectName("dock_history")
        self.d_history.setWidget(self.undo_view)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.d_history)
        self.tabifyDockWidget(self.d_library, self.d_queue)
        self.tabifyDockWidget(self.d_queue, self.d_history)
        self.d_library.raise_()
        self.resizeDocks([self.d_library], [460], Qt.Orientation.Horizontal)

        self.hint = QLabel("")
        self.statusBar().addWidget(self.hint, 1)
        self.l_info = QLabel("")
        self.statusBar().addPermanentWidget(self.l_info)

        # timers
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(2000)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_safety = QTimer(self)
        self._autosave_safety.setInterval(60_000)
        self._autosave_safety.timeout.connect(lambda: self._autosave() if self._autosave_pending else None)
        self._autosave_safety.start()

    def _initial_split_sizes(self) -> None:
        """Proportional default sizes (or the ones saved last time)."""
        saved = self.settings.splitter_sizes or {}
        h, w = self.split.height(), self.top_split.width()
        v = saved.get("v")
        tl_h = self._timeline_block.minimumSizeHint().height()   # fixed rows + the default lanes
        rest = max(0, h - tl_h)
        table = max(self.clip_panel.minimumHeight(), int(rest * 0.25))
        self.split.setSizes(v if v and len(v) == 3 else [max(0, h - tl_h - table), tl_h, table])
        t = saved.get("h")
        self.top_split.setSizes(t if t and len(t) == 2 else [int(w * 0.78), int(w * 0.22)])

    def _act(self, menu, action_id: str, text: str, fn, extra_keys: tuple[str, ...] = ()) -> QAction:
        a = QAction(tr(text), self)
        keys = [k for k in [key_for(action_id), *extra_keys] if k]
        if keys:
            a.setShortcuts([QKeySequence(k) for k in keys])
            a.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        a.setToolTip(with_key(text, action_id))
        def run(_checked=False, _fn=fn, _name=action_id or text):
            trace.log("action", _name)
            _fn()

        a.triggered.connect(run)
        if menu is not None:
            menu.addAction(a)
        self.addAction(a)
        return a

    def _build_actions(self) -> None:
        mb = self.menuBar()
        m_file = mb.addMenu(tr("&File"))
        self._act(m_file, "open_video", "Open video…", self._open_video_dialog)
        self._act(m_file, "open_project", "Open project…", self._open_project_dialog)
        self.m_recent = m_file.addMenu(tr("Recent projects"))
        self.m_recent.aboutToShow.connect(self._fill_recent)
        m_file.addSeparator()
        self._act(m_file, "save", "Save project", lambda: self.save_project(False))
        self._act(m_file, "save_as", "Save project as…", lambda: self.save_project(True))
        m_file.addSeparator()
        self._act(m_file, "export", "Export clips…", self.export_clips)
        self._act(m_file, "frames", "Extract frames…", lambda: self.extract_frames(None))
        m_file.addSeparator()
        self._act(m_file, "", "Import marks from mpv…", lambda: self.offer_mpv_import(auto=False))
        self._act(m_file, "", "Clear mpv marks for this video…", self.clear_mpv_marks)
        m_file.addSeparator()
        self._act(m_file, "", "Exit", self.close)

        m_edit = mb.addMenu(tr("&Edit"))
        undo = self.doc.undo.createUndoAction(self, tr("Undo"))
        undo.setShortcuts([QKeySequence("Ctrl+Z")])
        redo = self.doc.undo.createRedoAction(self, tr("Redo"))
        redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        m_edit.addAction(undo)
        m_edit.addAction(redo)
        self.addAction(undo)
        self.addAction(redo)
        m_edit.addSeparator()
        self._act(m_edit, "set_in", "Set clip In", self.set_in)
        self._act(m_edit, "set_out", "Set clip Out", self.set_out)
        self._act(m_edit, "marker", "Add marker", self.add_marker)
        self._act(m_edit, "delete", "Delete selected", self.delete_selected)

        m_play = mb.addMenu(tr("&Playback"))
        self._act(m_play, "play", "Play / pause", self.toggle_play)
        self._act(m_play, "frame_next", "Next frame", self.frame_next)
        self._act(m_play, "frame_prev", "Previous frame", self.frame_prev)
        self._act(m_play, "sec_next", "Forward 1 second", lambda: self.jump_seconds(1.0))
        self._act(m_play, "sec_prev", "Back 1 second", lambda: self.jump_seconds(-1.0))
        self._act(m_play, "key_next", "Next keyframe", lambda: self.jump_keyframe(1))
        self._act(m_play, "key_prev", "Previous keyframe", lambda: self.jump_keyframe(-1))
        self._act(m_play, "mark_next", "Next marker / clip boundary", lambda: self.jump_mark(1))
        self._act(m_play, "mark_prev", "Previous marker / clip boundary", lambda: self.jump_mark(-1))
        self._act(m_play, "shuttle_back", "Shuttle: back 1 second", self.shuttle_back)
        self._act(m_play, "shuttle_stop", "Shuttle: pause", self.shuttle_stop)
        self._act(m_play, "shuttle_fwd", "Shuttle: play / faster", self.shuttle_fwd)
        m_play.addSeparator()
        self._act(m_play, "ep_prev", "Previous episode in folder", lambda: self.open_neighbour(-1))
        self._act(m_play, "ep_next", "Next episode in folder", lambda: self.open_neighbour(1))

        m_view = mb.addMenu(tr("&View"))
        self._act(m_view, "zoom_in", "Zoom timeline in", lambda: self.timeline.zoom_at(1.5), ("Ctrl++",))
        self._act(m_view, "zoom_out", "Zoom timeline out", lambda: self.timeline.zoom_at(1 / 1.5))
        self._act(m_view, "zoom_fit", "Fit whole video", self.timeline.zoom_fit)
        self._act(m_view, "zoom_clip", "Zoom to selected clip", self._zoom_clip)
        m_view.addSeparator()
        self._act(m_view, "library", "Show / hide library", lambda: self.toggle_panel(self.d_library))
        self._act(m_view, "queue", "Show / hide export queue", lambda: self.toggle_panel(self.d_queue))
        m_view.addSeparator()
        for d in (self.d_library, self.d_queue, self.d_history):
            m_view.addAction(d.toggleViewAction())

        m_tools = mb.addMenu(tr("&Tools"))
        self._act(m_tools, "", "Export presets…", self.edit_presets)
        self._act(m_tools, "", "External tools…", lambda: self.open_tools(auto=False))
        self._act(m_tools, "", "Settings…", self.open_settings)

        m_help = mb.addMenu(tr("&Help"))
        self._act(m_help, "help", "Keyboard shortcuts", self.show_shortcuts)
        self._act(m_help, "", "Show introduction tour", self.show_tour)
        self._act(m_help, "", "About", self._about)

    def _wire(self) -> None:
        if self.player is not None:
            self.player.time_changed.connect(self._on_time)
            self.player.pause_changed.connect(self._on_pause)
        self.b_play.clicked.connect(self.toggle_play)
        self.b_frame_next.clicked.connect(self.frame_next)
        self.b_frame_prev.clicked.connect(self.frame_prev)
        self.b_sec_next.clicked.connect(lambda: self.jump_seconds(1.0))
        self.b_sec_prev.clicked.connect(lambda: self.jump_seconds(-1.0))
        self.b_ep_prev.clicked.connect(lambda: self.open_neighbour(-1))
        self.b_ep_next.clicked.connect(lambda: self.open_neighbour(1))
        self.b_in.clicked.connect(self.set_in)
        self.b_out.clicked.connect(self.set_out)
        self.b_marker.clicked.connect(self.add_marker)
        self.b_export.clicked.connect(self.export_clips)
        self.b_frames.clicked.connect(lambda: self.extract_frames(None))
        self.c_aid.currentIndexChanged.connect(self._preview_aid_changed)
        self.c_sid.currentIndexChanged.connect(self._preview_sid_changed)
        self.vol.valueChanged.connect(lambda v: self.player and self.player.set_volume(v))
        self.timeline.seek_requested.connect(self.seek)
        self.timeline.hint.connect(self.hint.setText)
        self.timeline.context_action.connect(self._timeline_action)
        self.clip_panel.seek_requested.connect(self.seek)
        self.clip_panel.detect_crop_requested.connect(self.detect_crop)
        self.clip_panel.preview_crop.connect(self._preview_crop)
        self.clip_panel.edit_presets_requested.connect(self.edit_presets)
        self.library.open_requested.connect(lambda p: self.open_video(p))
        self.library.frames_requested.connect(self.extract_frames)
        self.library.folders_changed.connect(lambda _f: self.settings.save())
        self.doc.project_changed.connect(self._schedule_autosave)
        self.doc.view_changed.connect(self._schedule_autosave)
        self.doc.dirty_changed.connect(lambda _d: self._update_title())
        self.queue.job_finished.connect(self._job_finished)
        self.doc.undo.indexChanged.connect(self._trace_undo)
        self.queue.job_updated.connect(self._trace_job)
        self._job_status: dict[str, str] = {}

    def _trace_undo(self, index: int) -> None:
        u = self.doc.undo
        text = u.text(index - 1) if index > 0 else "<start>"
        trace.log("undo", f"index={index}/{u.count()} current='{text}'")

    def _trace_job(self, job_id: str) -> None:
        job = self.queue.get(job_id)
        if job is not None and self._job_status.get(job_id) != job.status:
            self._job_status[job_id] = job.status
            trace.log("job", f"{job.status} '{job.title}' {job.message[-200:]!r} warnings={job.warnings}")

    # =========================================================================================
    # Opening / saving
    # =========================================================================================
    def _confirm_discard(self) -> bool:
        if not self.doc.has_video or not self.doc.dirty or not self.doc.project_path:
            return True  # unsaved-but-never-saved work lives on in the autosave
        r = QMessageBox.question(self, APP_TITLE, tr("Save changes to the project?"),
                                 QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                                 | QMessageBox.StandardButton.Cancel)
        if r == QMessageBox.StandardButton.Cancel:
            return False
        if r == QMessageBox.StandardButton.Save:
            return self.save_project(False)
        return True

    def _open_video_dialog(self) -> None:
        exts = " ".join(f"*.{e}" for e in self.settings.video_exts)
        path, _ = QFileDialog.getOpenFileName(self, tr("Open video"), self.settings.last_open_dir,
                                              tr("Video") + f" ({exts});;" + tr("All files") + " (*)")
        if path:
            self.open_video(path)

    def _open_project_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, tr("Open project"), self.settings.last_open_dir,
                                              tr("Anime Clip Studio project") + f" (*{PROJECT_EXT})")
        if path:
            self.open_project(path)

    def _fill_recent(self) -> None:
        self.m_recent.clear()
        for p in self.settings.recent_projects:
            a = self.m_recent.addAction(p)
            a.triggered.connect(lambda _c=False, path=p: self.open_project(path))
        if not self.settings.recent_projects:
            a = self.m_recent.addAction(tr("(empty)"))
            a.setEnabled(False)

    def open_project(self, path: str, restored_from: str | None = None) -> None:
        """Open a .acsproj. `restored_from` = snapshot/autosave file loaded in place of `path`."""
        if not self._confirm_discard():
            return
        try:
            project = load_project(restored_from or path)
        except ProjectLoadError as e:
            QMessageBox.warning(self, APP_TITLE, tr("Cannot open project:") + f"\n{e}")
            return
        project_path = path if path and Path(path).suffix.lower() == PROJECT_EXT and Path(path).exists() else None
        video, status = resolve_video(project, project_path)
        if video is None:
            QMessageBox.information(self, APP_TITLE, tr("The project's video was not found:") +
                                    f"\n{project.video.path}\n\n" + tr("Please locate it."))
            video, _ = QFileDialog.getOpenFileName(self, tr("Locate video"), str(Path(project.video.path).parent))
            if not video:
                return
            try:
                if project.video.fingerprint and file_fingerprint(video) != project.video.fingerprint:
                    status = "mismatch"
            except OSError:
                pass
        if status == "mismatch":
            QMessageBox.warning(self, APP_TITLE, tr("This file differs from the one the project was made with. "
                                                    "Markers and clips may not line up."))
        project.video.path = video
        if project_path:
            self.settings.add_recent(project_path)
            self.settings.save()
        self.open_video(video, project=project, project_path=project_path, confirm=False,
                        restored=restored_from is not None)

    def open_video(self, path: str, project: Project | None = None, project_path: str | None = None,
                   confirm: bool = True, restored: bool = False) -> None:
        # Re-entrancy guard: message boxes below run a nested event loop, so a second double-click
        # (or Enter) in the library could otherwise start loading while this load is half done.
        if self._opening:
            return
        if (project is None and not restored and self.doc.has_video
                and os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(self.doc.project.video.path))):
            self.library.highlight(path)
            return  # already open
        self._opening = True
        try:
            self._open_video_impl(path, project, project_path, confirm, restored)
        finally:
            self._opening = False

    def _open_video_impl(self, path: str, project: Project | None, project_path: str | None,
                         confirm: bool, restored: bool) -> None:
        trace.log("open", f"open_video {path!r} project={project_path!r} restored={restored}")
        if confirm and not self._confirm_discard():
            trace.log("open", "cancelled by user (unsaved changes)")
            return
        if self.player is None:
            QMessageBox.warning(self, APP_TITLE, tr("The video player is not available. Check Settings."))
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            info = probe(path, self.settings.ffprobe)
            trace.log("open", f"probe ok: {info.summary()} dur={info.duration:.3f} audio={len(info.audios)} "
                              f"subs={len(info.subs)} start={info.start_time}")
        except (ProbeError, OSError) as e:
            trace.log("open", f"probe failed: {e}")
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_TITLE, tr("Cannot read the video:") + f"\n{e}")
            return
        finally:
            if QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
        if info.video is None:
            QMessageBox.warning(self, APP_TITLE, tr("This file has no video stream."))
            return
        if self.doc.has_video:
            self._autosave()
        if project is None:
            auto = find_autosave(path)
            if auto is not None:
                data = read_json(auto, {}) or {}
                n_clips, n_marks = len(data.get("clips", [])), len(data.get("markers", []))
                if n_clips or n_marks:
                    r = QMessageBox.question(
                        self, APP_TITLE,
                        tr("This video has saved work ({clips} clips, {markers} markers). Restore it?").format(
                            clips=n_clips, markers=n_marks))
                    if r == QMessageBox.StandardButton.Yes:
                        try:
                            project = load_project(auto)
                            restored = False
                        except ProjectLoadError:
                            project = None
        if project is None:
            project = Project()
        v = project.video
        v.path = path
        v.duration = info.duration
        try:
            st = os.stat(path)
            if v.size != st.st_size or not v.fingerprint:
                v.size = st.st_size
                v.fingerprint = file_fingerprint(path)
        except OSError:
            pass
        index = FrameIndex.cfr(info.fps, info.duration, info.video.start_time)
        remap_frames(project, info.fps, index.nearest_frame)
        v.fps = str(info.fps)
        if project.default_audio_track is None:
            project.default_audio_track = pick_track(self.settings.preferred_audio_langs, info.audios)
        if project.default_sub_track is None:
            project.default_sub_track = pick_sub_track(self.settings.preferred_sub_langs, info.subs)
        self._token += 1
        token = self._token
        if self._load_cancel is not None:
            self._load_cancel.set()          # stop background work of the previous video
        self._load_cancel = threading.Event()
        trace.log("open", f"doc.load token={token} clips={len(project.clips)} markers={len(project.markers)}")
        self.doc.load(project, info, index, project_path)
        if restored:
            self.doc.undo.resetClean()
        self._pos = project.view.position
        aid = project.view.aid if project.view.aid is not None else project.default_audio_track
        sid = project.view.sid
        self._fill_preview_tracks(aid, sid)
        self.player.load(path, start=self._pos, aid=aid, sid=sid)
        self.player_stack.setCurrentWidget(self.player)
        self.settings.last_open_dir = str(Path(path).parent)
        self.library.highlight(path)
        recovery.write_lock(project_path or "", path)
        self.l_info.setText(info.summary() + f" · {Path(path).name}")
        self._update_title()
        self._update_labels()
        self._start_background(token, path, info, aid)
        trace.log("open", "open_video done")
        QTimer.singleShot(400, lambda tok=token: self.offer_mpv_import(auto=True) if tok == self._token else None)

    @staticmethod
    def _thread(name: str, fn) -> None:
        """Start a daemon worker that records start / end / duration / errors in the trace."""
        def run():
            t0 = time.monotonic()
            trace.log("bg", f"{name} started")
            try:
                fn()
                trace.log("bg", f"{name} finished in {time.monotonic() - t0:.2f}s")
            except Exception as e:  # noqa: BLE001
                trace.log("bg", f"{name} FAILED after {time.monotonic() - t0:.2f}s: {e!r}")
                raise

        threading.Thread(target=run, daemon=True, name=name).start()

    # Worker threads below only touch plain Python values captured in closures, a threading.Event
    # for cancellation and the dispatcher queue — never Qt objects or Qt signals.
    def _start_background(self, token: int, path: str, info, aid) -> None:
        ffprobe, ffmpeg = self.settings.ffprobe, self.settings.ffmpeg
        hw = self.settings.hwdec
        thumb_h = self.preview_quality()[0]
        cancel = self._load_cancel
        post = self.dispatch.post
        on_index, on_thumbs = self._on_index, self._on_thumbs
        trace.log("open", f"preview quality: thumbnails {thumb_h}px, waveform {self.preview_quality()[1]}/s "
                          f"(dpr {self._dpr():g})")

        def scan():
            try:
                pk = scan_packets(path, info.start_time, ffprobe, cancel=cancel.is_set)
                if not cancel.is_set() and pk.pts:
                    post(on_index, token, FrameIndex.from_scan(pk, info.fps))
            except Exception as e:  # noqa: BLE001
                log.warning("packet scan failed: %s", e)

        def thumbs():
            try:
                from acs.core import thumbs as th
                v = info.video
                res = th.generate(path, v.width, v.height, v.sar, info.start_time, ffmpeg,
                                  cancel=cancel.is_set, hwaccel=hw, thumb_h=thumb_h)
                if res is not None and not cancel.is_set():
                    post(on_thumbs, token, res[0], res[1])
            except Exception as e:  # noqa: BLE001
                log.warning("thumbnails failed: %s", e)

        self._thread("scan", scan)
        if self.doc.project.clips:
            self._snapshot("before-thumbs")
        self._thread("thumbs", thumbs)
        self._start_waveform(token, path, aid)

    def _start_waveform(self, token: int, path: str, track: int | None) -> None:
        if self._wave_cancel is not None:
            self._wave_cancel.set()
        if track is None or self.doc.info is None or not self.doc.info.audios:
            self.timeline.set_waveform(None)
            return
        ffmpeg = self.settings.ffmpeg
        bps = self.preview_quality()[1]
        load_cancel = self._load_cancel
        wave_cancel = self._wave_cancel = threading.Event()
        post, on_wave = self.dispatch.post, self._on_wave

        def cancelled() -> bool:
            return wave_cancel.is_set() or (load_cancel is not None and load_cancel.is_set())

        def wave():
            try:
                from acs.core import waveform as wf
                peaks = wf.generate(path, track, ffmpeg, cancel=cancelled, bps=bps)
                if peaks is not None and not cancelled():
                    post(on_wave, token, bps, peaks)
            except Exception as e:  # noqa: BLE001
                log.warning("waveform failed: %s", e)

        self._thread("wave", wave)

    def _on_index(self, token: int, fi: FrameIndex) -> None:
        trace.log("open", f"index ready token={token} current={self._token} frames={fi.frame_count} "
                          f"keys={len(fi.keyframes)}")
        if token != self._token:
            return
        self.doc.set_index(fi)
        self.l_keys.setText(tr("{n} keyframes").format(n=len(fi.keyframes)))
        self._update_labels()

    def _on_thumbs(self, token: int, times, images) -> None:
        trace.log("open", f"thumbnails ready token={token} current={self._token} n={len(times)}")
        if token == self._token:
            self.timeline.set_thumbnails(times, images)

    def _on_wave(self, token: int, bps: int, peaks) -> None:
        if token == self._token:
            self.timeline.set_waveform(peaks, bps)

    def _dpr(self) -> float:
        screen = self.screen() or QApplication.primaryScreen()
        return float(screen.devicePixelRatio()) if screen is not None else 1.0

    def preview_quality(self) -> tuple[int, int]:
        """(thumbnail height px, waveform points per second) from Settings or auto for this screen."""
        dpr = self._dpr()
        return (uiscale.thumb_height(self.settings.thumb_quality, dpr),
                uiscale.wave_bps(self.settings.wave_quality, dpr))

    def save_project(self, save_as: bool) -> bool:
        if not self.doc.has_video:
            return False
        path = self.doc.project_path
        if save_as or not path:
            default = str(Path(self.doc.project.video.path).with_suffix(PROJECT_EXT))
            path, _ = QFileDialog.getSaveFileName(self, tr("Save project"), path or default,
                                                  tr("Anime Clip Studio project") + f" (*{PROJECT_EXT})")
            if not path:
                return False
            if not path.lower().endswith(PROJECT_EXT):
                path += PROJECT_EXT
        p = self.doc.project
        p.view.position = self._pos
        used = {c.preset_id for c in p.clips if c.preset_id} | {p.default_preset_id}
        p.presets_snapshot = {u.id: u.to_dict() for u in self.presets.user if u.id in used}
        try:
            save_project(p, path)
        except OSError as e:
            QMessageBox.warning(self, APP_TITLE, tr("Cannot save the project:") + f"\n{e}")
            return False
        trace.log("project", f"saved {path!r} clips={len(p.clips)} markers={len(p.markers)}")
        self.doc.project_path = path
        self.doc.mark_saved()
        write_autosave(p)
        self.settings.add_recent(path)
        self.settings.save()
        recovery.write_lock(path, p.video.path)
        self.statusBar().showMessage(tr("Project saved") + f": {path}", 5000)
        self._update_title()
        return True

    def _schedule_autosave(self) -> None:
        if self.doc.has_video:
            self._autosave_pending = True
            self._autosave_timer.start()
            self._update_title()

    def _autosave(self) -> None:
        if not self.doc.has_video:
            return
        self.doc.project.view.position = self._pos
        try:
            write_autosave(self.doc.project)
            self._autosave_pending = False
            self.library.update_badge(self.doc.project.video.path)
        except OSError as e:
            log.warning("autosave failed: %s", e)

    def _snapshot(self, reason: str) -> None:
        if not self.doc.has_video:
            return
        try:
            self.doc.project.view.position = self._pos
            recovery.snapshot(self.doc.project, self.doc.project_path, reason)
        except OSError as e:
            log.warning("snapshot failed: %s", e)

    def emergency_save(self) -> None:
        if self.doc.has_video:
            recovery.snapshot(self.doc.project, self.doc.project_path, "emergency")

    def _update_title(self) -> None:
        name = ""
        if self.doc.project_path:
            name = Path(self.doc.project_path).name
        elif self.doc.has_video:
            name = Path(self.doc.project.video.path).name
        star = "*" if self.doc.has_video and self.doc.dirty and self.doc.project_path else ""
        self.setWindowTitle(f"{name}{star} — {APP_TITLE}" if name else APP_TITLE)

    # =========================================================================================
    # Playback & navigation
    # =========================================================================================
    def _on_time(self, t: float) -> None:
        self._pos = t
        self.timeline.set_position(t, follow=not self._paused)
        self._update_labels()

    def _on_pause(self, paused: bool) -> None:
        self._paused = paused
        self.b_play.setIcon(icons.icon("play" if paused else "pause"))
        if paused and self.player is not None and abs(self.player.speed - 1.0) > 1e-3:
            self.player.set_speed(1.0)
            self.l_speed.setText("")

    def _update_labels(self) -> None:
        if not self.doc.has_video:
            self.l_time.setText("--:--:--.--- / --:--:--")
            self.l_frame.setText("#—")
            return
        f = self.doc.frame_at(self._pos)
        idx = self.doc.index
        pos, total = idx.frame_in_second(f)
        width = len(str(total))
        self.l_time.setText(f"{fmt_time(self.doc.time_of(f))} | {pos:0{width}d}/{total}"
                            f"  /  {fmt_time(idx.end_time, ms=False)}")
        key = " K" if idx.exact and idx.is_keyframe(f) else ""
        self.l_frame.setText(f"#{f}{key}")

    def seek(self, t: float) -> None:
        if not self.doc.has_video or self.player is None:
            return
        f = self.doc.frame_at(max(0.0, t))
        t = self.doc.time_of(f)
        self._pos = t
        self.player.seek(t)
        self.timeline.set_position(t, follow=True)
        self._update_labels()

    def seek_frame(self, f: int) -> None:
        if self.doc.has_video:
            self.seek(self.doc.time_of(self.doc.clamp_frame(f)))

    def toggle_play(self) -> None:
        if self.player is not None and self.doc.has_video:
            self.player.toggle_pause()

    def frame_next(self) -> None:
        if self.player is not None and self.doc.has_video:
            self.player.frame_step()

    def frame_prev(self) -> None:
        if self.player is not None and self.doc.has_video:
            self.player.frame_back_step()

    def jump_seconds(self, dt: float) -> None:
        self.seek(self._pos + dt)

    def jump_keyframe(self, direction: int) -> None:
        if not self.doc.has_video:
            return
        f = self.doc.frame_at(self._pos)
        k = self.doc.index.next_keyframe(f) if direction > 0 else self.doc.index.prev_keyframe(f)
        if k is not None:
            self.seek_frame(k)

    def jump_mark(self, direction: int) -> None:
        if not self.doc.has_video:
            return
        p = self.doc.project
        frames = {m.frame for m in p.markers}
        for c in p.clips:
            frames.add(c.in_frame)
            if c.out_frame is not None:
                frames.add(c.out_frame - 1)
        f = self.doc.frame_at(self._pos)
        cands = sorted(x for x in frames if (x > f if direction > 0 else x < f))
        if cands:
            self.seek_frame(cands[0] if direction > 0 else cands[-1])

    def shuttle_back(self) -> None:
        if self.player is not None and self.doc.has_video:
            self.player.set_paused(True)
            self.jump_seconds(-1.0)

    def shuttle_stop(self) -> None:
        if self.player is not None:
            self.player.set_paused(True)
            self.player.set_speed(1.0)
            self.l_speed.setText("")

    def shuttle_fwd(self) -> None:
        if self.player is None or not self.doc.has_video:
            return
        if self._paused:
            self.player.set_speed(1.0)
            self.player.set_paused(False)
            self.l_speed.setText("")
        else:
            sp = min(4.0, self.player.speed * 2)
            self.player.set_speed(sp)
            self.l_speed.setText(f"×{sp:g}")

    def open_neighbour(self, direction: int) -> None:
        if not self.doc.has_video:
            return
        prev, nxt = episode_neighbours(self.doc.project.video.path, self.settings.video_exts)
        target = nxt if direction > 0 else prev
        if target:
            self.open_video(target)
        else:
            self.statusBar().showMessage(tr("No more episodes in this folder"), 3000)

    def _fill_preview_tracks(self, aid, sid) -> None:
        info = self.doc.info
        for c in (self.c_aid, self.c_sid):
            c.blockSignals(True)
            c.clear()
        self.c_sid.addItem(tr("off"), None)
        if info is not None:
            for a in info.audios:
                self.c_aid.addItem(a.label(), a.rel)
            for s in info.subs:
                self.c_sid.addItem(s.label(), s.rel)
        i = self.c_aid.findData(aid)
        self.c_aid.setCurrentIndex(max(0, i))
        i = self.c_sid.findData(sid)
        self.c_sid.setCurrentIndex(max(0, i))
        for c in (self.c_aid, self.c_sid):
            c.blockSignals(False)

    def _preview_aid_changed(self) -> None:
        rel = self.c_aid.currentData()
        if self.player is not None:
            self.player.set_aid(rel)
        self.doc.project.view.aid = rel
        if self.doc.has_video:
            self._start_waveform(self._token, self.doc.project.video.path, rel)

    def _preview_sid_changed(self) -> None:
        rel = self.c_sid.currentData()
        if self.player is not None:
            self.player.set_sid(rel)
        self.doc.project.view.sid = rel

    def _preview_crop(self, rect: CropRect | None) -> None:
        if self.player is not None:
            self.player.set_crop(rect.as_mpv() if rect is not None else None)

    # =========================================================================================
    # Editing
    # =========================================================================================
    def _cur_frame(self) -> int:
        return self.doc.frame_at(self._pos)

    def set_in(self) -> None:
        if self.doc.has_video:
            self.doc.set_in(self._cur_frame())
            self.statusBar().showMessage(tr("In set. Move to the end and press O."), 3000)

    def set_out(self) -> None:
        if not self.doc.has_video:
            return
        if self.doc.set_out(self._cur_frame()) is None:
            self.statusBar().showMessage(tr("Press I first to start a clip (Out must be after In)."), 4000)

    def add_marker(self) -> None:
        if self.doc.has_video:
            self.doc.add_marker(self._cur_frame())

    def delete_selected(self) -> None:
        self.doc.delete_selected()

    def _zoom_clip(self) -> None:
        c = self.doc.selected_clip()
        if c is not None and c.out_time is not None:
            self.timeline.zoom_range(c.in_time, c.out_time)

    def _timeline_action(self, action: str, t: float) -> None:
        f = self.doc.frame_at(t)
        if action == "marker":
            self.doc.add_marker(f)
        elif action == "set_in":
            self.doc.set_in(f)
        elif action == "set_out":
            if self.doc.set_out(f) is None:
                self.statusBar().showMessage(tr("Press I first to start a clip (Out must be after In)."), 4000)
        elif action == "zoom_clip":
            self._zoom_clip()
        elif action == "delete":
            self.doc.delete_selected()
        elif action == "zoom_fit":
            self.timeline.zoom_fit()

    def detect_crop(self, clip_ids: list) -> None:
        if not self.doc.has_video or not clip_ids:
            return
        info = self.doc.info
        v = info.video
        clips = [c for c in (self.doc.project.clip_by_id(i) for i in clip_ids) if c is not None]
        if not clips:
            return
        self._snapshot("before-cropdetect")
        t0 = min(c.in_time for c in clips)
        t1 = max((c.out_time or c.in_time + 5) for c in clips)
        path, ffmpeg = self.doc.project.video.path, self.settings.ffmpeg
        self.statusBar().showMessage(tr("Detecting black bars…"))
        post, on_crop, ids = self.dispatch.post, self._on_crop, list(clip_ids)

        def work():
            try:
                rect = detect_crop(path, t0, t1, v.width, v.height, ffmpeg)
            except Exception as e:  # noqa: BLE001
                log.warning("cropdetect failed: %s", e)
                rect = None
            post(on_crop, ids, rect)

        self._thread("cropdetect", work)

    def _on_crop(self, clip_ids: list, rect) -> None:
        trace.log("crop", f"result for {clip_ids}: {rect}")
        if rect is None:
            self.statusBar().showMessage(tr("No black bars found."), 5000)
            self.doc.update_clips(clip_ids, {"crop": None, "crop_mode": "none"}, tr("Crop"))
            return
        self.statusBar().showMessage(tr("Crop detected") + f": {rect.w}x{rect.h}+{rect.x}+{rect.y}", 5000)
        self.doc.update_clips(clip_ids, {"crop": rect, "crop_mode": "auto"}, tr("Detect crop"))

    # =========================================================================================
    # Export / frames
    # =========================================================================================
    # =========================================================================================
    # Marks made in the stand-alone mpv player (acs_markers.lua)
    # =========================================================================================
    def offer_mpv_import(self, auto: bool) -> None:
        if not self.doc.has_video:
            return
        path = self.doc.project.video.path
        try:
            marks = mpv_markers.read_marks(path)
        except OSError as e:
            log.warning("cannot read mpv marks: %s", e)
            return
        project = self.doc.project
        if auto and project.mpv_offer_disabled:
            trace.log("mpv-marks", "auto offer disabled for this video")
            return
        seen = set(project.mpv_seen_ids)
        new = [m for m in marks if m.id not in seen]
        already = len(marks) - len(new)
        trace.log("mpv-marks", f"{len(marks)} marks for this video, {len(new)} new (auto={auto})")
        if not new:
            if not auto:
                QMessageBox.information(self, APP_TITLE, tr("No new marks from mpv for this video.")
                                        + (("\n" + tr("Already imported:") + f" {already}") if already else ""))
                if project.mpv_offer_disabled:
                    project.mpv_offer_disabled = False
                    self.doc._changed()
            return
        markers, clips, unpaired = mpv_markers.split_marks(new)
        text = tr("Marks made in mpv were found for this video:") + "\n\n" + \
            tr("{m} markers, {c} clips").format(m=len(markers), c=len(clips))
        if unpaired:
            text += "\n" + tr("{u} clip starts/ends without a pair (imported as markers)").format(u=len(unpaired))
        if already:
            text += "\n" + tr("{n} marks imported earlier are skipped (no duplicates).").format(n=already)
        box = QMessageBox(QMessageBox.Icon.Question, APP_TITLE, text, parent=self)
        b_import = box.addButton(tr("Import"), QMessageBox.ButtonRole.AcceptRole)
        b_import_clear = box.addButton(tr("Import and clear in mpv"), QMessageBox.ButtonRole.AcceptRole)
        b_later = box.addButton(tr("Not now"), QMessageBox.ButtonRole.RejectRole)
        b_clear = box.addButton(tr("Clear without importing"), QMessageBox.ButtonRole.DestructiveRole)
        never = QCheckBox(tr("Don't offer marks from this video again"))
        never.setToolTip(tr("They can still be imported later via File → Import marks from mpv…"))
        never.setChecked(project.mpv_offer_disabled)
        box.setCheckBox(never)
        box.setDefaultButton(b_import)
        box.exec()
        clicked = box.clickedButton()
        if never.isChecked() != project.mpv_offer_disabled:
            project.mpv_offer_disabled = never.isChecked()
            trace.log("mpv-marks", f"offer disabled for this video: {project.mpv_offer_disabled}")
            self.doc._changed()
        if clicked in (b_import, b_import_clear):
            self._import_mpv_marks(markers, clips, unpaired)
            project.mpv_seen_ids = sorted(seen | {m.id for m in new})  # never import the same mark twice
            self.doc._changed()
        if clicked in (b_import_clear, b_clear):
            removed = mpv_markers.clear_marks(path)
            trace.log("mpv-marks", f"cleared {removed} records")
            self.statusBar().showMessage(tr("mpv marks for this video were cleared."), 5000)

    def _import_mpv_marks(self, markers, clips, unpaired) -> None:
        doc = self.doc
        doc.undo.beginMacro(tr("Import marks from mpv"))
        try:
            for m in markers:
                doc.add_marker(doc.frame_at(m.time), label="mpv")
            for m in unpaired:
                doc.add_marker(doc.frame_at(m.time), label=tr("mpv start") if m.kind == "in" else tr("mpv end"))
            for a, b in clips:
                in_f = doc.frame_at(a.time)
                out_f = min(doc.frame_at(b.time) + 1, doc.frame_count)  # the marked frame is included
                if out_f > in_f:
                    doc.add_clip(in_f, out_f)
        finally:
            doc.undo.endMacro()
        trace.log("mpv-marks", f"imported {len(markers)} markers, {len(clips)} clips, {len(unpaired)} unpaired")
        self.statusBar().showMessage(tr("Imported from mpv:") + " " +
                                     tr("{m} markers, {c} clips").format(m=len(markers) + len(unpaired),
                                                                         c=len(clips)), 6000)

    def clear_mpv_marks(self) -> None:
        if not self.doc.has_video:
            return
        path = self.doc.project.video.path
        n = len(mpv_markers.read_marks(path))
        if n == 0:
            QMessageBox.information(self, APP_TITLE, tr("No marks from mpv for this video."))
            return
        r = QMessageBox.question(self, APP_TITLE, tr("Delete {n} marks made in mpv for this video? "
                                                    "Markers already imported into the project stay.").format(n=n))
        if r == QMessageBox.StandardButton.Yes:
            mpv_markers.clear_marks(path)
            self.statusBar().showMessage(tr("mpv marks for this video were cleared."), 5000)

    def export_clips(self) -> None:
        if not self.doc.has_video:
            return
        if not any(c.is_complete for c in self.doc.project.clips):
            QMessageBox.information(self, APP_TITLE, tr("There are no finished clips yet. Mark a clip with I and O."))
            return
        dlg = ExportDialog(self.doc, self.presets, self.settings, self)
        if not dlg.exec():
            return
        items = dlg.items()
        if not items:
            return
        self.doc.set_project_fields({"output_dir": dlg.output_dir(), "name_template": dlg.name_template()},
                                    tr("Export settings"))
        p = self.doc.project
        info = self.doc.info
        src = p.video.path
        jobs = []
        for it in items:
            c, s = it.clip, it.summary
            spec = build_export_spec_dict(src=src, clip=c, in_time=c.in_time, out_time=c.out_time,
                                          audio_rel=effective_audio(p, info, c), sub_rel=effective_sub(p, info, c),
                                          preset=s.preset, output=str(it.output), height_req=s.height_req,
                                          hwaccel=self.settings.hwdec, settings=self.settings)
            codec = CODECS.get(s.preset.vcodec)
            gpu = bool(codec and codec.gpu and s.preset.kind == "video" and s.preset.mode == "encode")
            jobs.append(Job("export", f"{Path(src).stem} · {c.name} → {it.output.name}", spec, gpu=gpu))
        trace.log("export", f"enqueue {len(jobs)} jobs: " + "; ".join(
            f"{j.title} preset={j.spec['preset']['id']} gpu={j.gpu}" for j in jobs))
        self.queue.enqueue(jobs, before_start=lambda: self._snapshot("before-export"))
        self.d_queue.show()
        self.d_queue.raise_()

    def extract_frames(self, path: str | None) -> None:
        current = self.doc.project.video.path if self.doc.has_video else None
        if path is None:
            path = current
        if not path:
            QMessageBox.information(self, APP_TITLE, tr("Open a video first (or right-click an episode in the Library)."))
            return
        same = current is not None and os.path.normcase(path) == os.path.normcase(current)
        if same:
            info = self.doc.info
            dlg = FramesDialog(path, info, self.settings, self.doc.project.markers, self.doc.project.clips,
                               self.doc.selected_clips, self.doc.index.end_time, self)
        else:
            try:
                info = probe(path, self.settings.ffprobe)
            except (ProbeError, OSError) as e:
                QMessageBox.warning(self, APP_TITLE, tr("Cannot read the video:") + f"\n{e}")
                return
            dlg = FramesDialog(path, info, self.settings, parent=self)
        if not dlg.exec():
            return
        cfg = dlg.config()
        if not cfg.out_dir:
            return
        self.settings.frames_last = dlg.remember()
        self.settings.frames_template = cfg.template
        self.settings.save()
        job = Job("frames", f"{Path(path).stem} · " + tr("frames") + f" → {Path(cfg.out_dir).name}",
                  {"config": cfg.to_dict()})
        trace.log("frames", f"enqueue {job.title} config={cfg.to_dict()}")
        self.queue.enqueue([job], before_start=lambda: self._snapshot("before-frames"))
        self.d_queue.show()
        self.d_queue.raise_()

    def _job_finished(self, job_id: str) -> None:
        job = self.queue.get(job_id)
        if job is not None and not self.queue.has_active():
            self.statusBar().showMessage(tr("All jobs finished."), 6000)

    # =========================================================================================
    # Dialogs
    # =========================================================================================
    def edit_presets(self) -> None:
        PresetEditor(self.presets, self).exec()
        self.clip_panel.reload_sources()

    RESTART_FIELDS = ("ui_scale", "theme", "language")

    def open_settings(self) -> None:
        before = {k: getattr(self.settings, k) for k in self.RESTART_FIELDS}
        dlg = SettingsDialog(self.settings, self)
        if dlg.exec():
            self.library.refresh()
            self.statusBar().showMessage(tr("Settings saved."), 4000)
            changed = [k for k in self.RESTART_FIELDS if getattr(self.settings, k) != before[k]]
            if changed or getattr(dlg, "libmpv_changed", False):
                trace.log("settings", f"restart-only settings changed: {changed}")
                self._restart_note()

    def restart_app(self) -> None:
        """Close normally (unsaved-changes prompt, autosave, settings) and start a new instance."""
        self.restart_requested = True
        if not self.close():
            self.restart_requested = False

    def open_tools(self, auto: bool) -> None:
        dlg = ToolsDialog(self.settings, self, auto=auto)
        if dlg.exec():
            self.statusBar().showMessage(tr("Settings saved."), 4000)
            if dlg.libmpv_changed or (self.player is None and quick_ok("libmpv", self.settings.libmpv)):
                self._restart_note()

    def _restart_note(self) -> None:
        r = QMessageBox.question(
            self, APP_TITLE,
            tr("Some changes (interface size, theme, language or video player library) take effect "
               "only after a restart.\n\nRestart Anime Clip Studio now?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
        if r == QMessageBox.StandardButton.Yes:
            self.restart_app()

    def show_shortcuts(self) -> None:
        if self._shortcuts_dlg is None:
            self._shortcuts_dlg = ShortcutsDialog(self)
        self._shortcuts_dlg.show()
        self._shortcuts_dlg.raise_()

    def show_tour(self) -> None:
        targets = {"library": self.library, "player": self.player_stack, "timeline": self.timeline,
                   "clips": self.clip_panel, "export": self.queue_panel, "frames": self.queue_panel}
        self.d_library.show()
        tour = TourOverlay(self, targets)

        def done(never: bool):
            self.settings.show_tour = not never
            self.settings.save()

        tour.finished.connect(done)
        tour.start()

    def _about(self) -> None:
        QMessageBox.about(self, APP_TITLE, f"<b>{APP_TITLE}</b> {__version__}<br>"
                          + tr("Frame-accurate anime clipping with mpv + ffmpeg.") + "<br><br>"
                          + tr("Data folder:") + f" {recovery.AppDirs.data}")

    # =========================================================================================
    # Startup recovery
    # =========================================================================================
    def startup_checks(self, stale_lock: dict | None) -> None:
        missing = [k for k in ("ffmpeg", "ffprobe", "libmpv") if not quick_ok(k, getattr(self.settings, k))]
        if missing or self.player is None:
            trace.log("tools", f"missing or broken at startup: {missing} player={self.player is not None}")
            self.open_tools(auto=True)
        if stale_lock is not None:
            items = recovery.list_recovery_items()
            if items:
                dlg = RecoveryDialog(items, recovery.latest_crash_log(), stale_lock, self)
                dlg.exec()
                sel = dlg.selected
                if dlg.choice == RecoveryDialog.RESTORE and sel is not None:
                    self.open_project(sel.project_path or str(sel.path), restored_from=str(sel.path))
                elif dlg.choice == RecoveryDialog.ORIGINAL and sel is not None and sel.project_path:
                    self.open_project(sel.project_path)
        saved = recovery.load_queue_state()
        pending = self.queue.restore(saved)
        if pending:
            r = QMessageBox.question(self, APP_TITLE,
                                     tr("{n} export / frame jobs were not finished last time. Resume them?").format(
                                         n=len(pending)))
            if r == QMessageBox.StandardButton.Yes:
                self.queue.enqueue(pending)
                self.d_queue.show()
                self.d_queue.raise_()
            else:
                recovery.save_queue_state([])
        recovery.cleanup_work_dirs()
        out_dirs = [self.settings.output_dir] + [str(Path(j.get("spec", {}).get("output", "")).parent)
                                                 for j in saved if j.get("spec", {}).get("output")]
        orphans = recovery.find_orphans(out_dirs)
        if orphans:
            names = "\n".join(str(o) for o in orphans[:10])
            r = QMessageBox.question(self, APP_TITLE, tr("Unfinished output files from an interrupted export were "
                                                         "found:") + f"\n{names}\n\n" + tr("Delete them?"))
            if r == QMessageBox.StandardButton.Yes:
                for o in orphans:
                    try:
                        o.unlink()
                    except OSError:
                        pass
        if self.settings.show_tour:
            QTimer.singleShot(400, self.show_tour)

    # =========================================================================================
    # Window events
    # =========================================================================================
    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        urls = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if not urls:
            return
        path = urls[0]
        if path.lower().endswith(PROJECT_EXT):
            self.open_project(path)
        elif os.path.isfile(path):
            self.open_video(path)

    def _restore_geometry(self) -> None:
        s = self.settings
        if s.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(s.window_geometry.encode()))
        if s.window_state:
            # A state saved by an older dock layout (library on the left) is ignored.
            self.restoreState(QByteArray.fromBase64(s.window_state.encode()), DOCK_STATE_VERSION)

    def toggle_panel(self, dock: QDockWidget) -> None:
        """Show + bring the tab to front, or hide it if it is already the visible tab."""
        if dock.isVisible() and not dock.visibleRegion().isEmpty():
            dock.hide()
        else:
            dock.show()
            dock.raise_()

    def closeEvent(self, ev):
        trace.log("ui", "main window close requested")
        jobs_running = any(j.status in ACTIVE for j in self.queue.jobs)
        if jobs_running:
            r = QMessageBox.question(self, APP_TITLE, tr("Jobs are still running. Cancel them and quit?"))
            if r != QMessageBox.StandardButton.Yes:
                ev.ignore()
                return
        if not self._confirm_discard():
            ev.ignore()
            return  # nothing cancelled yet: the jobs keep running
        if jobs_running:
            self.queue.cancel_all()
        self._autosave()
        self.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        self.settings.window_state = bytes(self.saveState(DOCK_STATE_VERSION).toBase64()).decode()
        self.settings.splitter_sizes = {"v": self.split.sizes(), "h": self.top_split.sizes()}
        self.settings.save()
        self.library.shutdown()
        if self.player is not None:
            self.player.shutdown()
        recovery.save_queue_state([j.to_dict() for j in self.queue.jobs if j.status in ACTIVE])
        recovery.release_lock()
        super().closeEvent(ev)
