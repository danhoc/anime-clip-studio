"""Main window: wires player, timeline, clips, library, queue, autosave and recovery together."""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QFont, QFontMetrics, QKeySequence
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow,
                               QMenu, QMessageBox, QPushButton, QScrollArea, QSlider, QSplitter, QStackedWidget,
                               QTabWidget, QToolButton, QUndoView, QVBoxLayout, QWidget)

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
from acs.core.model import SHORTS_ASPECT, CropRect, Project
from acs.core.presets import CODECS, PresetStore
from acs.core.probe import ProbeError, probe, scan_packets
from acs.core.settings import DEFAULT_STEPS, parse_step
from acs.core import framing, shorts
from acs.core.project_io import (PROJECT_EXT, ProjectLoadError, find_autosave, load_project, remap_frames,
                                 resolve_video, save_project, write_autosave_data)
from acs.core.timebase import FrameIndex
from acs.core.tools import quick_ok
from acs.core.util import file_fingerprint, fmt_time
from acs.i18n import tr
from acs.core import uiscale
from acs.ui import icons, theme
from acs.ui.clip_panel import ClipPanel
from acs.ui.dispatch import MainThreadDispatcher
from acs.ui.export_dialog import ExportDialog
from acs.ui.flow_layout import FlowLayout
from acs.ui.frames_dialog import FramesDialog
from acs.ui.help import QuickHelpDialog, ShortcutsDialog, TourOverlay
from acs.ui.hotkeys import STEP_MODS, key_for, step_text, with_key
from acs.ui.library_panel import LibraryPanel
from acs.ui.panes import PaneToggle
from acs.ui.player import MpvPlayer
from acs.ui.preset_editor import PresetEditor
from acs.ui.queue_panel import QueuePanel
from acs.ui.recovery_dialog import RecoveryDialog
from acs.ui.settings_dialog import SettingsDialog
from acs.ui.frame_overlay import FrameOverlay, FrameTarget
from acs.ui.tools_panel import ToolsDialog
from acs.ui.timeline import SMALL_SCREEN_H, Timeline

log = logging.getLogger("acs.ui")

DOCK_STATE_VERSION = 3  # bump when the default dock arrangement changes (3: side panel is a splitter pane)
# Scrubbing: exact seeks (merged, one at a time) while they can keep up with the mouse; keyframe
# seeks only when the playhead crosses more than a whole GOP during one exact seek (fast sweeps on a
# zoomed-out timeline or a slow PC) — below that they would just repeat the same keyframe.
SCRUB_SETTLE_MS = 120       # the mouse rests this long -> exact seek
SCRUB_RELEASE = 0.5         # hysteresis: back to exact seeks below this share of the keyframe threshold


class TrackButton(QToolButton):
    """Compact track picker for the transport bar: icon + short label, the full list in a menu."""

    changed = Signal()

    def __init__(self, icon_kind: str, tip: str, parent=None):
        super().__init__(parent)
        self.setIcon(icons.icon(icon_kind))
        self.setIconSize(QSize(20, 20))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._tip = tr(tip)
        self.setToolTip(self._tip)
        self._menu = QMenu(self)
        self.setMenu(self._menu)
        self._group = QActionGroup(self)
        self._data = None

    def clear(self) -> None:
        for a in self._group.actions():
            self._group.removeAction(a)
        self._menu.clear()
        self._data = None
        self.setText("")
        self.setToolTip(self._tip)

    def add_item(self, label: str, data, short: str) -> None:
        a = self._menu.addAction(label)
        a.setCheckable(True)
        a.setData((data, short))
        self._group.addAction(a)
        a.triggered.connect(lambda _checked=False, act=a: self._pick(act, emit=True))

    def set_current(self, data) -> None:
        """Select the item with this data (the first item when there is none, like a combo box)."""
        acts = self._group.actions()
        a = next((x for x in acts if x.data()[0] == data), acts[0] if acts else None)
        if a is not None:
            self._pick(a, emit=False)

    def current_data(self):
        return self._data

    def _pick(self, a: QAction, emit: bool) -> None:
        a.setChecked(True)
        old = self._data
        self._data, short = a.data()
        self.setText(short)
        self.setToolTip(f"{self._tip}: {a.text()}")
        if emit and self._data != old:
            self.changed.emit()


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
        self._scrubbing = False
        self._scrub_speed = 0.0          # smoothed scrub speed, video seconds per second
        self._scrub_keyframes = False    # currently scrubbing with keyframe seeks
        self._scrub_last = (0.0, 0.0)    # (monotonic time, video time) of the previous scrub step
        self._last_seek: tuple[float, bool] | None = None   # (time, precise) of the newest seek request
        self._autosave_pending = False
        # One writer thread: autosaves (with fsync) never block the UI and stay in order.
        self._autosave_writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="autosave")
        self._shortcuts_dlg: ShortcutsDialog | None = None
        self._quick_help: QuickHelpDialog | None = None
        # The tour is a parentless top-level window: without this reference Python frees it (and Qt
        # deletes the window) as soon as show_tour returns, so neither the tour nor the quick help
        # that follows it would ever appear.
        self._tour: TourOverlay | None = None
        self.setWindowTitle(APP_TITLE)
        self.resize(1500, 950)
        self.setAcceptDrops(True)
        self._build_ui()
        self._build_actions()
        self._update_step_texts()
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
        self.empty = QLabel(tr("Add a folder with videos in the Library (right)\n"
                               "or drag a video / .acsproj file here.\n\n"
                               "F1 — keyboard shortcuts"))
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t = theme.current()
        self.empty.setStyleSheet(f"color: {t.dim}; font-size: 15px; background: {t.empty_bg};")
        self.player_stack.addWidget(self.empty)
        self.player: MpvPlayer | None = None
        self.frame_overlay: FrameOverlay | None = None
        try:
            self.player = MpvPlayer(self.settings.libmpv, self.settings.hwdec, self._mpv_log_file)
            self.player_stack.addWidget(self.player)
            self.frame_overlay = FrameOverlay(self.player)
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
                          "#transport QToolButton { padding: 4px 4px; }"
                          "#transport QToolButton#mark_in, #transport QToolButton#mark_out,"
                          "#transport QToolButton#mark_marker { padding: 4px 8px; border-radius: 4px; }"
                          f"#transport QToolButton#mark_in {{ color: {c_in}; border: 1px solid {c_in}; }}"
                          f"#transport QToolButton#mark_out {{ color: {c_out}; border: 1px solid {c_out}; }}"
                          f"#transport QToolButton#mark_marker {{ color: {c_mark}; border: 1px solid {c_mark}; }}")
        # Four groups (navigation, time, marking, tracks + export) in one row from 1920 px screens on
        # (library open, both languages); on narrower ones (1366 px) the bar wraps onto a second row
        # instead of widening the window.
        flow = FlowLayout(bar, spacing=8)
        flow.setContentsMargins(6, 4, 6, 4)

        def group() -> QHBoxLayout:
            g = QWidget()
            lay = QHBoxLayout(g)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(4)
            flow.addWidget(g)
            return lay

        bl = group()

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
        # tooltips name the arrow keys that do the same step (see _update_step_texts)
        self.b_sec_prev = tb("−1s", "Back 1 second")
        self.b_frame_prev = tb("", "Previous frame", "", "frame_prev")
        self.b_play = tb("", "Play / pause", "play", "play")
        self.b_play.setIconSize(QSize(30, 30))
        self.b_frame_next = tb("", "Next frame", "", "frame_next")
        self.b_sec_next = tb("+1s", "Forward 1 second")
        self.b_ep_next = tb("", "Next episode in folder", "ep_next", "ep_next")
        bl = group()
        mono = "font-family: Consolas, 'Cascadia Mono', monospace; font-size: 16px;"
        self.l_time = QLabel("--:--:--.--- / --:--:--")
        self.l_time.setStyleSheet(mono)
        self.l_time.setToolTip(tr("Current time / duration"))
        self.l_frame = QLabel("#—")
        self.l_frame.setStyleSheet(mono + f" color: {t.info};")
        self.l_frame.setToolTip(tr("Frame within the current second / frames in that second · "
                                   "#absolute frame number · K = keyframe"))
        bl.addWidget(self.l_time)
        bl.addSpacing(4)
        bl.addWidget(self.l_frame)
        # Fixed widths so the buttons on the right never move while the text changes
        # (e.g. when " K" appears on keyframes or the frame number gains a digit).
        mono_font = QFont()
        mono_font.setFamilies(["Consolas", "Cascadia Mono", "Courier New"])
        mono_font.setPixelSize(16)
        fm = self._mono_fm = QFontMetrics(mono_font)
        self._time_digits = 0
        self._fit_time_label(2)   # frames per second below 100; widened in _update_labels if needed
        self.l_frame.setFixedWidth(fm.horizontalAdvance("#0000000 K") + 6)   # 7 digits: 46 h at 60 fps
        for lab in (self.l_time, self.l_frame):
            lab.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        bl = group()
        self.b_in = tb("[ " + tr("Clip start") + " I", "Set clip In (starts a new clip)", "set_in",
                       bold=True, name="mark_in")
        self.b_out = tb(tr("Clip end") + " O ]", "Set clip Out (current frame included)", "set_out",
                        bold=True, name="mark_out")
        self.b_marker = tb("⚑ " + tr("Marker") + " M", "Add marker", "marker", bold=True, name="mark_marker")
        bl = group()
        self.l_speed = QLabel("")
        bl.addWidget(self.l_speed)
        # Compact pickers (icon + language, full track list in the menu) keep the bar narrow enough
        # for 1366 / 1600 px wide screens.
        self.c_aid = TrackButton("volume", "Audio track for preview")
        self.c_sid = TrackButton("subtitles", "Subtitles shown in the preview")
        bl.addWidget(self.c_aid)
        bl.addWidget(self.c_sid)
        self.b_mute = QToolButton()
        self.b_mute.setIcon(icons.icon("volume"))   # crossed out only while muted
        self.b_mute.setIconSize(QSize(20, 20))
        self.b_mute.setCheckable(True)
        self.b_mute.setAutoRaise(True)
        self.b_mute.setToolTip(tr("Mute"))
        self.b_mute.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        bl.addWidget(self.b_mute)
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 130)
        self.vol.setValue(80)
        self.vol.setFixedWidth(60)
        self.vol.setToolTip(tr("Preview volume"))
        self.vol.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        bl.addWidget(self.vol)
        bl.addSpacing(8)
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

        # ---- timeline (its zoom buttons sit in a strip on its right, never over the ruler) ----
        self.timeline = Timeline(self.doc)
        screen = self.screen() or QApplication.primaryScreen()
        screen_h = screen.availableGeometry().height() if screen is not None else 1080
        self.timeline.configure_for_screen(screen_h)
        self.l_keys = QLabel("")
        self.l_keys.setStyleSheet(theme.css_dim())

        self.clip_panel = ClipPanel(self.doc, self.presets, self.settings)

        # Selected-clip properties and the project defaults live in a side column next to the player:
        # the video keeps its aspect ratio anyway, so this uses what would otherwise be black bars.
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(self.clip_panel.props)
        sl.addWidget(self.clip_panel.defaults_box)
        sl.addStretch(1)
        props_scroll = self.props_scroll = QScrollArea()
        props_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        props_scroll.setWidget(side)
        props_scroll.setWidgetResizable(True)
        props_scroll.setFrameShape(QFrame.Shape.NoFrame)
        props_scroll.setMinimumWidth(320)
        self.top_split = QSplitter(Qt.Orientation.Horizontal)
        self.top_split.addWidget(self.player_stack)
        self.top_split.addWidget(props_scroll)
        self.top_split.setStretchFactor(0, 4)
        self.top_split.setStretchFactor(1, 1)
        self.top_split.setCollapsible(0, False)
        self.props_pane = PaneToggle(self.top_split, 1, 320)

        # Player / timeline / clip table. The timeline's thumbnail and waveform rows have a fixed
        # height and sit right under the transport bar; dragging the handle below the timeline only
        # grows or shrinks its clip-lane area, so no empty gap can open between video and timeline.
        self.clip_panel.setMinimumHeight(100 if screen_h < SMALL_SCREEN_H else 140)
        # The transport bar spans the player and the side column, so it has the whole width for its
        # single row (and wraps only when even that is not enough).
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        tv.setSpacing(0)
        tv.addWidget(self.top_split, 1)
        tv.addWidget(bar)
        tl_block = QWidget()
        th = QHBoxLayout(tl_block)
        th.setContentsMargins(0, 0, 0, 0)
        th.setSpacing(0)
        th.addWidget(self.timeline, 1)
        th.addWidget(self.timeline.zoom_bar)
        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.addWidget(top)
        self.split.addWidget(tl_block)
        self.split.addWidget(self.clip_panel)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 0)
        self.split.setStretchFactor(2, 1)
        for i in range(2):
            self.split.setCollapsible(i, False)
        # the clip list can be dragged away completely (the timeline then gets its height)
        self.clips_pane = PaneToggle(self.split, 2, self.clip_panel.minimumHeight() * 2)
        self._timeline_block = tl_block

        # ---- side panel: library, export queue and history as tabs (Ctrl+L toggles the library) ----
        # A splitter pane rather than dock widgets, so that, like the Selected clip column, dragging its
        # edge to the window border hides it completely.
        self.library = LibraryPanel(self.settings, self.dispatch)
        self.queue_panel = QueuePanel(self.queue)
        self.undo_view = QUndoView(self.doc.undo)
        self.undo_view.setEmptyLabel(tr("<opened>"))
        self.side = QTabWidget()
        self.side.setDocumentMode(True)
        # long tab names (Russian) must not set the panel's minimum width: they elide instead
        self.side.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        self.side.tabBar().setUsesScrollButtons(False)
        self.side.addTab(self.library, tr("Library"))
        self.side.addTab(self.queue_panel, tr("Export queue"))
        self.side.addTab(self.undo_view, tr("History"))
        self.outer = QSplitter(Qt.Orientation.Horizontal)
        self.outer.addWidget(self.split)
        self.outer.addWidget(self.side)
        self.outer.setStretchFactor(0, 1)
        self.outer.setStretchFactor(1, 0)
        self.outer.setCollapsible(0, False)
        # ~16 % of the screen width: 300 px on 1920 px screens leaves the transport bar its single row
        screen_w = screen.availableGeometry().width() if screen is not None else 1920
        self.side_pane = PaneToggle(self.outer, 1, min(420, max(280, round(screen_w * 0.156))))
        self.setCentralWidget(self.outer)
        QTimer.singleShot(0, self._initial_split_sizes)

        self.hint = QLabel("")
        self.statusBar().addWidget(self.hint, 1)
        self.statusBar().addPermanentWidget(self.l_keys)
        self.l_info = QLabel("")
        self.statusBar().addPermanentWidget(self.l_info)

        # timers
        self._scrub_settle = QTimer(self)
        self._scrub_settle.setSingleShot(True)
        self._scrub_settle.setInterval(SCRUB_SETTLE_MS)
        self._scrub_settle.timeout.connect(self._scrub_exact)
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(2000)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_safety = QTimer(self)
        self._autosave_safety.setInterval(60_000)
        self._autosave_safety.timeout.connect(lambda: self._autosave() if self._autosave_pending else None)
        self._autosave_safety.start()

    def _initial_split_sizes(self) -> None:
        """Proportional default sizes (or the ones saved last time, hidden panes included)."""
        saved = self.settings.splitter_sizes or {}
        for pane, key in ((self.side_pane, "side"), (self.clips_pane, "clips"), (self.props_pane, "props")):
            if isinstance(saved.get("last", {}).get(key), int) and saved["last"][key] > 0:
                pane.size = saved["last"][key]
        side = saved.get("side")
        total = self.outer.width()
        self.outer.setSizes(side if side and len(side) == 2 else [max(0, total - self.side_pane.size),
                                                                   self.side_pane.size])
        h, w = self.split.height(), self.outer.sizes()[0]
        v = saved.get("v")
        tl_h = self._timeline_block.minimumSizeHint().height()   # fixed rows + the default lanes
        rest = max(0, h - tl_h)
        table = max(self.clip_panel.minimumHeight(), int(rest * 0.22))
        self.split.setSizes(v if v and len(v) == 3 else [max(0, h - tl_h - table), tl_h, table])
        t = saved.get("h")
        self.top_split.setSizes(t if t and len(t) == 2 else [int(w * 0.74), int(w * 0.26)])
        for pane in (self.side_pane, self.clips_pane, self.props_pane):
            pane.sync()

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
        self.a_cancel_edge = self._act(m_edit, "cancel_edge", "Cancel changing a clip edge",
                                       self.doc.cancel_edge_edit)
        self.a_cancel_edge.setEnabled(False)   # Esc stays free for everything else
        self._act(m_edit, "marker", "Add marker", self.add_marker)
        self._act(m_edit, "delete", "Delete selected", self.delete_selected)

        m_play = mb.addMenu(tr("&Playback"))
        self._act(m_play, "play", "Play / pause", self.toggle_play)
        self._step_actions: dict[str, QAction] = {}
        for mod in STEP_MODS:
            for d, suffix in ((1, "next"), (-1, "prev")):
                aid = f"step_{mod}_{suffix}"
                self._step_actions[aid] = self._act(m_play, aid, step_text(*self._step_setting(mod), d),
                                                    lambda m=mod, d=d: self.step(m, d))
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
        self._act(m_view, "library", "Show / hide library", lambda: self.toggle_panel(self.library))
        self._act(m_view, "queue", "Show / hide export queue", lambda: self.toggle_panel(self.queue_panel))
        m_view.addSeparator()
        # panes that can also be hidden by dragging their edge to the window border
        for pane, text in ((self.side_pane, "Side panel (library, queue, history)"),
                           (self.clips_pane, "Clip list"), (self.props_pane, "Selected clip column")):
            a = self._act(m_view, "", text, lambda p=pane: p.toggle())
            a.setCheckable(True)
            a.setChecked(True)
            pane.changed.connect(a.setChecked)

        m_tools = mb.addMenu(tr("&Tools"))
        self.a_shorts = self._act(m_tools, "", "Shorts Mode", lambda: self.set_shorts_mode(self.a_shorts.isChecked()))
        self.a_shorts.setCheckable(True)
        self.a_shorts.setChecked(self.settings.shorts_mode)
        self.a_shorts.setToolTip(tr("Each clip gets a 9:16 YouTube Shorts frame over the video; "
                                    "only the picture inside the frame is exported"))
        m_tools.setToolTipsVisible(True)
        m_tools.addSeparator()
        self._act(m_tools, "", "Export presets…", self.edit_presets)
        self._act(m_tools, "", "External tools…", lambda: self.open_tools(auto=False))
        self._act(m_tools, "", "Settings…", self.open_settings)

        m_help = mb.addMenu(tr("&Help"))
        self._act(m_help, "", "Quick help", self.show_quick_help)
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
        self.c_aid.changed.connect(self._preview_aid_changed)
        self.c_sid.changed.connect(self._preview_sid_changed)
        self.vol.valueChanged.connect(lambda v: self.player and self.player.set_volume(v))
        self.b_mute.toggled.connect(lambda m: self.player and self.player.set_mute(m))
        self.b_mute.toggled.connect(lambda m: self.b_mute.setIcon(icons.icon("muted" if m else "volume")))
        self.timeline.zoom_to_clip_requested.connect(self._zoom_clip)
        self.timeline.seek_requested.connect(self.seek)
        self.timeline.scrub_moved.connect(self.scrub)
        self.timeline.scrub_finished.connect(self.scrub_end)
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
        self.doc.view_changed.connect(lambda: self._schedule_autosave(title=False))
        self.doc.dirty_changed.connect(lambda _d: self._update_title())
        self.doc.edge_edit_changed.connect(self._on_edge_edit)
        self.doc.selection_changed.connect(self._update_frame)
        self.doc.clips_changed.connect(self._update_frame)
        if self.frame_overlay is not None:
            self.frame_overlay.rect_edited.connect(self._on_frame_edited)
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
            self._autosave(wait=True)
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
        self._autosave(wait=True)
        self.settings.add_recent(path)
        self.settings.save()
        recovery.write_lock(path, p.video.path)
        self.statusBar().showMessage(tr("Project saved") + f": {path}", 5000)
        self._update_title()
        return True

    def _schedule_autosave(self, title: bool = True) -> None:
        if self.doc.has_video:
            self._autosave_pending = True
            self._autosave_timer.start()
            if title:   # zoom / scroll changes do not affect the title
                self._update_title()

    def _autosave(self, wait: bool = False) -> None:
        """Serialise now (GUI thread), write on the autosave thread; `wait` blocks until written."""
        if not self.doc.has_video:
            return
        p = self.doc.project
        p.view.position = self._pos
        if not p.video.path:
            return
        path, data, n_clips = p.video.path, p.to_dict(), len(p.clips)
        self._autosave_pending = False
        post, badge = self.dispatch.post, self.library.update_badge

        def write():
            try:
                write_autosave_data(path, data)
            except OSError as e:
                log.warning("autosave failed: %s", e)
                return
            post(badge, path, n_clips)

        try:
            fut = self._autosave_writer.submit(write)
        except RuntimeError:   # writer already shut down (window closing): write here
            write()
            return
        if wait:
            fut.result()

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
        if self._scrubbing or (self.player is not None and self.player.seek_busy):
            return  # mpv still reports the position before the seek; the requested frame is the truth
        self._pos = t
        self.timeline.set_position(t, follow=not self._paused)
        self._update_labels()
        self._update_frame()

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
        if width != self._time_digits:
            self._fit_time_label(width)
        self.l_time.setText(f"{fmt_time(self.doc.time_of(f))} | {pos:0{width}d}/{total}"
                            f" / {fmt_time(idx.end_time, ms=False)}")
        key = " K" if idx.exact and idx.is_keyframe(f) else ""
        self.l_frame.setText(f"#{f}{key}")

    def _fit_time_label(self, digits: int) -> None:
        """Fixed width for 'time | frame/frames / duration' with this many digits per frame count, so
        the buttons on the right never move while the text changes."""
        self._time_digits = digits
        n = "0" * max(2, digits)
        self.l_time.setFixedWidth(self._mono_fm.horizontalAdvance(f"00:00:00.000 | {n}/{n} / 00:00:00") + 6)

    def seek(self, t: float, precise: bool = True) -> None:
        if not self.doc.has_video or self.player is None:
            return
        f = self.doc.frame_at(max(0.0, t))
        t = self.doc.time_of(f)
        self._pos = t
        self._last_seek = (t, precise)
        self.player.seek(t, precise)
        self.timeline.set_position(t, follow=True, margin=0)
        self._update_labels()
        self._update_frame()

    def scrub(self, t: float) -> None:
        """Playhead dragged: the timeline follows the mouse at once; the picture uses exact seeks,
        or keyframe seeks while the mouse sweeps faster than exact seeks can follow."""
        now = time.monotonic()
        last_wall, last_t = self._scrub_last
        if self._scrubbing and now > last_wall:
            rate = abs(t - last_t) / (now - last_wall)
            self._scrub_speed = 0.6 * self._scrub_speed + 0.4 * rate
        else:
            self._scrub_speed = 0.0
            self._scrub_keyframes = False
        self._scrub_last = (now, t)
        self._scrubbing = True
        limit = self._keyframe_scrub_speed()
        if self._scrub_keyframes:
            self._scrub_keyframes = self._scrub_speed > limit * SCRUB_RELEASE
        else:
            self._scrub_keyframes = self._scrub_speed > limit
        self.seek(t, precise=not self._scrub_keyframes)
        self._scrub_settle.start()

    def _keyframe_scrub_speed(self) -> float:
        """Scrub speed (video seconds per second) above which keyframe seeks help: one exact seek
        takes `seek_latency`, so only when the playhead covers more than a typical GOP in that time
        would exact seeks fall behind. The zoom is part of it: the same mouse speed is more video
        seconds per second on a zoomed-out timeline."""
        gop = self.doc.index.typical_gop if self.doc.index is not None else None
        if not gop or self.player is None:
            return float("inf")   # keyframes unknown yet: exact seeks only
        return gop / max(0.01, self.player.seek_latency)

    def _scrub_exact(self) -> None:
        if self._scrubbing and self._last_seek != (self._pos, True):
            self.seek(self._pos)

    def scrub_end(self, t: float) -> None:
        """Mouse released after a click / drag on the timeline: pause on the frame under the mouse."""
        self._scrub_settle.stop()
        self._scrubbing = False
        was_playing = self.player is not None and not self._paused
        if self.player is not None:
            self.player.set_paused(True)
        # while playing, the picture has moved on since the last seek: go back to the released frame
        if was_playing or self._last_seek != (self.doc.time_of(self.doc.frame_at(max(0.0, t))), True):
            self.seek(t)

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

    def _step_setting(self, mod: str) -> tuple[str, float]:
        return parse_step(getattr(self.settings, f"step_{mod}"), DEFAULT_STEPS[mod])

    def step(self, mod: str, direction: int) -> None:
        """Arrow keys (mod = arrow / shift / ctrl): frames, seconds or a keyframe, as set in Settings."""
        kind, value = self._step_setting(mod)
        if kind == "keyframe":
            self.jump_keyframe(direction)
        elif kind == "seconds":
            self.jump_seconds(direction * value)
        elif value == 1:
            self.frame_next() if direction > 0 else self.frame_prev()   # mpv's exact frame step
        elif self.doc.has_video:
            self.seek_frame(self.doc.frame_at(self._pos) + direction * int(value))

    def _update_step_texts(self) -> None:
        """Menus, the cheat sheet and the transport tooltips follow the arrow steps set in Settings."""
        for aid, a in self._step_actions.items():
            _, mod, suffix = aid.split("_")
            text = step_text(*self._step_setting(mod), 1 if suffix == "next" else -1)
            a.setText(text)
            a.setToolTip(f"{text} ({key_for(aid)})")
        for b, tip, step, suffix in ((self.b_frame_prev, "Previous frame", ("frames", 1.0), "prev"),
                                     (self.b_frame_next, "Next frame", ("frames", 1.0), "next"),
                                     (self.b_sec_prev, "Back 1 second", ("seconds", 1.0), "prev"),
                                     (self.b_sec_next, "Forward 1 second", ("seconds", 1.0), "next")):
            mod = next((m for m in STEP_MODS if self._step_setting(m) == step), None)
            b.setToolTip(f"{tr(tip)} ({key_for(f'step_{mod}_{suffix}')})" if mod else tr(tip))
        if self._shortcuts_dlg is not None:   # rebuilt with the new texts when shown next
            self._shortcuts_dlg.deleteLater()
            self._shortcuts_dlg = None

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
            c.clear()
        self.c_sid.add_item(tr("off"), None, tr("off"))
        if info is not None:
            for a in info.audios:
                self.c_aid.add_item(a.label(), a.rel, f"#{a.rel + 1} {a.language}".strip())
            for s in info.subs:
                self.c_sid.add_item(s.label(), s.rel, f"#{s.rel + 1} {s.language}".strip())
        self.c_aid.set_current(aid)
        self.c_sid.set_current(sid)

    def _preview_aid_changed(self) -> None:
        rel = self.c_aid.current_data()
        if self.player is not None:
            self.player.set_aid(rel)
        self.doc.project.view.aid = rel
        if self.doc.has_video:
            self._start_waveform(self._token, self.doc.project.video.path, rel)

    def _preview_sid_changed(self) -> None:
        rel = self.c_sid.current_data()
        if self.player is not None:
            self.player.set_sid(rel)
        self.doc.project.view.sid = rel

    def _preview_crop(self, rect: CropRect | None) -> None:
        if self.player is not None:
            self.player.set_crop(rect.as_mpv() if rect is not None else None)
        self._update_frame()   # the crop rectangle is edited on the uncropped picture only

    # =========================================================================================
    # Editing
    # =========================================================================================
    def _cur_frame(self) -> int:
        return self.doc.frame_at(self._pos)

    def set_in(self) -> None:
        if self.doc.has_video:
            self._set_in_at(self._cur_frame())

    def set_out(self) -> None:
        if self.doc.has_video:
            self._set_out_at(self._cur_frame())

    def _set_in_at(self, f: int) -> None:
        editing = self.doc.edge_edit is not None and self.doc.edge_edit[1] == "in"
        if self.doc.set_in(f) is None:
            self.statusBar().showMessage(tr("The clip start must be before the clip end."), 4000)
        elif editing:
            self.statusBar().showMessage(tr("Clip start changed."), 3000)
        else:
            self.statusBar().showMessage(tr("In set. Move to the end and press O."), 3000)

    def _set_out_at(self, f: int) -> None:
        editing = self.doc.edge_edit is not None and self.doc.edge_edit[1] == "out"
        if self.doc.set_out(f) is None:
            self.statusBar().showMessage(tr("The clip end must be after the clip start.") if editing
                                         else tr("Press I first to start a clip (Out must be after In)."), 4000)
        elif editing:
            self.statusBar().showMessage(tr("Clip end changed."), 3000)

    def start_edge_edit(self, edge: str) -> None:
        """Right click on a clip edge: that edge follows the playhead until I / O sets it."""
        c = self.doc.selected_clip()
        if c is None or not self.doc.start_edge_edit(c.id, edge):
            return
        self.seek_frame(c.in_frame if edge == "in" else c.out_frame - 1)

    def _on_edge_edit(self) -> None:
        edit = self.doc.edge_edit
        self.a_cancel_edge.setEnabled(edit is not None)
        c = self.doc.project.clip_by_id(edit[0]) if edit is not None else None
        if c is None:
            self.statusBar().clearMessage()
        elif edit[1] == "in":
            self.statusBar().showMessage(tr("Changing the start of “{name}”: move the playhead and press I "
                                            "· Esc — cancel").format(name=c.name))
        else:
            self.statusBar().showMessage(tr("Changing the end of “{name}”: move the playhead and press O "
                                            "· Esc — cancel").format(name=c.name))

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
            self._set_in_at(f)
        elif action == "set_out":
            self._set_out_at(f)
        elif action in ("edit_in", "edit_out"):
            self.start_edge_edit(action[5:])
        elif action == "cancel_edge_edit":
            self.doc.cancel_edge_edit()
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
    # Shorts Mode
    # =========================================================================================
    def set_shorts_mode(self, on: bool) -> None:
        """Tools → Shorts Mode: each clip shows its 9:16 frame over the video and exports only the
        picture inside it; the crop tool is unavailable meanwhile (clip crops are kept, not used)."""
        trace.log("shorts", f"mode {'on' if on else 'off'}")
        self.settings.shorts_mode = on
        self.settings.save()
        self.a_shorts.setChecked(on)
        self.clip_panel.refresh()   # crop controls, sizes and modes follow the mode
        self._update_frame()
        self.statusBar().showMessage(
            tr("Shorts Mode: drag the frame to move it, drag a corner or turn the mouse wheel to resize it, "
               "double-click it to reset it.") if on else tr("Shorts Mode is off."), 8000 if on else 3000)

    def _frame_clip(self):
        """The clip whose Shorts frame / crop is shown: the one under the playhead (a selected one
        first), else the selected clip."""
        f = self._cur_frame()
        here = [c for c in self.doc.project.clips
                if c.in_frame <= f and (c.out_frame is None or f < c.out_frame)]
        sel = set(self.doc.selected_clips)
        return next((c for c in here if c.id in sel), here[0] if here else self.doc.selected_clip())

    def _update_frame(self) -> None:
        """Show the editable rectangle over the video: the clip's Shorts frame in Shorts Mode,
        otherwise its manual crop (unless the crop preview shows the cropped picture instead)."""
        if self.frame_overlay is None:
            return
        v = self.doc.info.video if self.doc.has_video else None
        c = self._frame_clip() if v is not None else None
        target = None
        if c is not None:
            dar = framing.display_aspect(v.width, v.height, v.sar)
            if self.settings.shorts_mode:
                target = FrameTarget("shorts", c.id, shorts.fractions(c.shorts, dar), dar,
                                     SHORTS_ASPECT, shorts.MIN_HEIGHT)
            elif (c.crop_mode == "manual" and c.crop is not None and c.crop.is_valid()
                  and not self.clip_panel.crop_preview.isChecked()):
                target = FrameTarget("crop", c.id, framing.from_crop(c.crop, v.width, v.height), dar,
                                     framing.parse_aspect(c.crop_aspect, dar))
        self.frame_overlay.set_target(target)

    def _on_frame_edited(self, kind: str, clip_id: str, rect) -> None:
        v = self.doc.info.video if self.doc.has_video else None
        c = self.doc.project.clip_by_id(clip_id)
        if v is None or c is None:
            return
        dar = framing.display_aspect(v.width, v.height, v.sar)
        if kind == "shorts":
            self.doc.set_clip_fields(clip_id, {"shorts": shorts.from_fractions(rect, dar)}, tr("Shorts frame"))
        else:
            crop = framing.to_crop(rect, v.width, v.height, v.sar, framing.parse_aspect(c.crop_aspect, dar))
            self.doc.set_clip_fields(clip_id, {"crop": crop, "crop_mode": "manual"}, tr("Crop"))

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
                                          hwaccel=self.settings.hwdec, settings=self.settings, info=info)
            codec = CODECS.get(s.preset.vcodec)
            gpu = bool(codec and codec.gpu and s.preset.kind == "video" and s.preset.mode == "encode")
            jobs.append(Job("export", f"{Path(src).stem} · {c.name} → {it.output.name}", spec, gpu=gpu))
        trace.log("export", f"enqueue {len(jobs)} jobs: " + "; ".join(
            f"{j.title} preset={j.spec['preset']['id']} gpu={j.gpu}" for j in jobs))
        self.queue.enqueue(jobs, before_start=lambda: self._snapshot("before-export"))
        self.show_panel(self.queue_panel)

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
        job = Job("frames", f"{Path(path).stem} · " + tr("Frames") + f" → {Path(cfg.out_dir).name}",
                  {"config": cfg.to_dict()})
        trace.log("frames", f"enqueue {job.title} config={cfg.to_dict()}")
        self.queue.enqueue([job], before_start=lambda: self._snapshot("before-frames"))
        self.show_panel(self.queue_panel)

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
            self._update_step_texts()
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
            self._shortcuts_dlg = ShortcutsDialog(self, {aid: a.text() for aid, a in self._step_actions.items()})
        self._shortcuts_dlg.show()
        self._shortcuts_dlg.raise_()

    def show_quick_help(self) -> None:
        if self._quick_help is not None:
            self._quick_help.show()
            self._quick_help.raise_()
            return
        dlg = QuickHelpDialog({m: self._step_setting(m) for m in STEP_MODS}, self.settings.show_quick_help, self)

        def closed(never: bool):
            self._quick_help = None
            if self.settings.show_quick_help == never:
                self.settings.show_quick_help = not never
                self.settings.save()

        dlg.closed.connect(closed)
        dlg.shortcuts_requested.connect(self.show_shortcuts)
        self._quick_help = dlg
        dlg.show()
        dlg.raise_()

    def show_tour(self, then_quick_help: bool = False) -> None:
        if self._tour is not None:
            self._tour.raise_()
            return
        trace.log("ui", f"tour started (then quick help: {then_quick_help})")
        targets = {"library": self.library, "player": self.player_stack, "timeline": self.timeline,
                   "clips": self.clip_panel, "export": self.b_export, "frames": self.b_frames,
                   "shorts": self.player_stack, "props": self.props_scroll,
                   "presets": self.clip_panel.defaults_box, "tools": self.menuBar(),
                   "mpv": self.menuBar(), "help": self.menuBar()}
        self.show_panel(self.library)
        tour = self._tour = TourOverlay(self, targets)

        def done(never: bool):
            self._tour = None
            trace.log("ui", f"tour finished (don't show again: {never})")
            self.settings.show_tour = not never
            self.settings.save()
            if then_quick_help:
                self.show_quick_help()

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
                self.show_panel(self.queue_panel)
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
        # First start: the tour, then the quick help; later starts: the quick help until it is turned off.
        if self.settings.show_tour:
            QTimer.singleShot(400, lambda: self.show_tour(then_quick_help=self.settings.show_quick_help))
        elif self.settings.show_quick_help:
            QTimer.singleShot(400, self.show_quick_help)

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

    def show_panel(self, page: QWidget) -> None:
        """Open the side panel (if hidden) on this tab."""
        self.side_pane.set_open(True)
        self.side.setCurrentWidget(page)

    def toggle_panel(self, page: QWidget) -> None:
        """Show + bring the tab to front, or hide the side panel if it is already the visible tab."""
        if self.side_pane.is_open() and self.side.currentWidget() is page:
            self.side_pane.set_open(False)
        else:
            self.show_panel(page)

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
        self._autosave(wait=True)
        self._autosave_writer.shutdown(wait=True)
        self.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        self.settings.window_state = bytes(self.saveState(DOCK_STATE_VERSION).toBase64()).decode()
        self.settings.splitter_sizes = {"v": self.split.sizes(), "h": self.top_split.sizes(),
                                        "side": self.outer.sizes(),
                                        "last": {"side": self.side_pane.size, "clips": self.clips_pane.size,
                                                 "props": self.props_pane.size}}
        self.settings.save()
        self.library.shutdown()
        if self.player is not None:
            self.player.shutdown()
        recovery.save_queue_state([j.to_dict() for j in self.queue.jobs if j.status in ACTIVE])
        recovery.release_lock()
        super().closeEvent(ev)
