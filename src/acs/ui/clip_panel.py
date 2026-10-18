"""Clip table + properties of the selected clip(s) + project defaults."""

from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from acs.core.clipinfo import effective_audio, effective_crop, summarize
from acs.core import framing
from acs.core.document import ProjectDocument
from acs.core.model import CropRect, ShortsFrame
from acs.core.ffmpeg_cmd import available_heights, scaled_width
from acs.core.presets import PresetStore
from acs.core.util import fmt_duration, fmt_time
from acs.ui import theme
from acs.i18n import tr
from acs.ui.texts import mode_text, reason_text, warning_text

COLS = ["#", "Name", "In", "Out", "Duration", "Audio", "Subs", "Size", "Preset", "Mode"]


def _combo_set(combo: QComboBox, data) -> None:
    i = combo.findData(data)
    combo.blockSignals(True)
    combo.setCurrentIndex(i if i >= 0 else 0)
    combo.blockSignals(False)


class ClipPanel(QWidget):
    seek_requested = Signal(float)
    detect_crop_requested = Signal(list)
    preview_crop = Signal(object)          # CropRect | None
    edit_presets_requested = Signal()

    def __init__(self, doc: ProjectDocument, presets: PresetStore, settings, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.presets = presets
        self.settings = settings
        self._updating = False

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        # ---- project defaults (placed by the main window in the side column, under the clip) ----
        self.def_preset = QComboBox()
        self.def_preset.setToolTip(tr("Preset used by clips that do not override it"))
        self.def_audio = QComboBox()
        self.def_audio.setToolTip(tr("Audio track used by clips that do not override it"))
        self.def_sub = QComboBox()
        self.def_sub.setToolTip(tr("Subtitle track burned in when a clip enables subtitles"))
        self.out_dir = QLineEdit()
        self.out_dir.setPlaceholderText(tr("Output folder"))
        self.out_dir.setToolTip(tr("Where exported clips are saved"))
        browse = QPushButton("…")
        browse.setFixedWidth(28)
        browse.setToolTip(tr("Choose output folder"))
        edit_presets = QPushButton(tr("Presets…"))
        edit_presets.setToolTip(tr("Create and edit your own export presets"))
        # Long preset / track names must not widen the side column: the combos shrink and elide.
        for combo in (self.def_preset, self.def_audio, self.def_sub):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(8)
        self.defaults_box = QGroupBox(tr("Project"))
        dform = QFormLayout(self.defaults_box)
        dform.setVerticalSpacing(4)
        preset_row = QHBoxLayout()
        preset_row.addWidget(self.def_preset, 1)
        preset_row.addWidget(edit_presets)
        out_row = QHBoxLayout()
        out_row.addWidget(self.out_dir, 1)
        out_row.addWidget(browse)
        dform.addRow(tr("Default preset"), preset_row)
        dform.addRow(tr("Audio"), self.def_audio)
        dform.addRow(tr("Subtitles"), self.def_sub)
        dform.addRow(tr("Output"), out_row)

        # ---- table ----
        self.table = QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels([tr(c) for c in COLS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)
        self.table.setColumnWidth(1, 110)
        self.table.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.empty_hint = QLabel(tr("No clips yet. Press I to set In, O to set Out — repeat for more clips."))
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setStyleSheet(theme.css_dim())
        tw = QWidget()
        tl = QVBoxLayout(tw)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(self.table)
        tl.addWidget(self.empty_hint)
        root.addWidget(tw, 1)

        # ---- properties ----
        self.props = QGroupBox(tr("Selected clip"))
        form = QFormLayout(self.props)
        form.setVerticalSpacing(4)   # the whole column fits next to the player on 1080 px screens
        self.mute = QCheckBox(tr("Mute audio"))
        self.mute.setToolTip(tr("Export this clip without any audio track"))
        self.audio = QComboBox()
        self.audio.setToolTip(tr("Audio track for this clip"))
        self.burn = QCheckBox(tr("Burn in subtitles"))
        self.burn.setToolTip(tr("Render the subtitles into the picture (requires re-encoding)"))
        self.sub = QComboBox()
        self.sub.setToolTip(tr("Subtitle track to burn in"))
        self.sub_size = QSpinBox()
        self.sub_size.setRange(25, 400)
        self.sub_size.setSingleStep(10)
        self.sub_size.setSuffix(" %")
        self.sub_size.setValue(100)   # shown while no clip is selected
        self.sub_size.setToolTip(tr("Size of the burned-in dialogue (signs placed in the picture keep their size). "
                                    "In Shorts Mode the subtitles are also scaled to the frame and placed at its "
                                    "bottom automatically."))
        self.crop_mode = QComboBox()
        for key, label in (("none", "No crop"), ("auto", "Auto (detect black bars)"), ("manual", "Manual")):
            self.crop_mode.addItem(tr(label), key)
        self.crop_mode.setToolTip(tr("Remove black bars. Cropping requires re-encoding."))
        self.detect = QPushButton(tr("Detect"))
        self.detect.setToolTip(tr("Analyse ~10 frames of the clip and suggest a crop"))
        self.crop_preview = QCheckBox(tr("Preview"))
        self.crop_preview.setToolTip(tr("Show the crop in the player"))
        crop_row = QHBoxLayout()
        crop_row.addWidget(self.crop_mode, 1)
        crop_row.addWidget(self.detect)
        crop_row.addWidget(self.crop_preview)
        self.crop_aspect = QComboBox()
        names = {"free": tr("Free"), "source": tr("Original")}
        for key in framing.CROP_ASPECTS:
            self.crop_aspect.addItem(names.get(key, key), key)
        self.crop_aspect.setToolTip(tr("Aspect ratio of the manual crop. Over the video: drag the frame to move it, "
                                       "drag a corner or an edge to resize it, double-click it to reset it "
                                       "(Preview off)."))
        self.shorts_size = QLabel()
        self.shorts_size.setToolTip(tr("Size of the exported picture"))
        self.shorts_reset = QPushButton(tr("Reset frame"))
        self.shorts_reset.setToolTip(tr("Full height, in the centre of the picture"))
        self.shorts_row = QHBoxLayout()
        self.shorts_row.addWidget(self.shorts_size, 1)
        self.shorts_row.addWidget(self.shorts_reset)
        self.crop_spins = []
        spin_row = self.spin_row = QHBoxLayout()
        for name in ("x", "y", "w", "h"):
            sp = QSpinBox()
            sp.setRange(0, 8192)
            sp.setSingleStep(2)
            sp.setPrefix(f"{name} ")
            sp.setToolTip(tr("Crop rectangle in source pixels"))
            self.crop_spins.append(sp)
            spin_row.addWidget(sp)
        self.res_combo = QComboBox()
        self.res_combo.setToolTip(tr("Output height. Only downscaling is possible; it forces re-encoding."))
        self.custom_h = QSpinBox()
        self.custom_h.setRange(64, 4320)
        self.custom_h.setSingleStep(2)
        self.custom_h.setToolTip(tr("Custom height (must be below the source height)"))
        self.custom_h.setVisible(False)   # only for the "Custom…" resolution
        h_row = QHBoxLayout()
        h_row.addWidget(self.res_combo, 1)
        h_row.addWidget(self.custom_h)
        self.preset = QComboBox()
        self.preset.setToolTip(tr("Export preset for this clip"))
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setStyleSheet(theme.css_info())
        form.addRow(self.mute)
        form.addRow(tr("Audio track"), self.audio)
        form.addRow(self.burn)
        form.addRow(tr("Subtitle track"), self.sub)
        form.addRow(tr("Subtitle size"), self.sub_size)
        form.addRow(tr("Shorts frame"), self.shorts_row)
        form.addRow(tr("Crop"), crop_row)
        form.addRow(tr("Aspect ratio"), self.crop_aspect)
        form.addRow("", spin_row)
        self._form = form
        form.addRow(tr("Resolution"), h_row)
        form.addRow(tr("Preset"), self.preset)
        form.addRow(self.info)
        # The properties box is not part of this panel's layout: the main window places it in the
        # side column next to the player (more room for the video and the timeline).
        self.props.setMinimumWidth(300)

        # ---- signals ----
        doc.clips_changed.connect(self.refresh)
        doc.selection_changed.connect(self._sync_selection)
        doc.loaded.connect(self.reload_sources)
        self.table.itemSelectionChanged.connect(self._table_selection)
        self.table.itemChanged.connect(self._item_edited)
        self.table.cellDoubleClicked.connect(self._double_clicked)
        self.def_preset.currentIndexChanged.connect(
            lambda: self.doc.set_project_fields({"default_preset_id": self.def_preset.currentData()},
                                                tr("Default preset")))
        self.def_audio.currentIndexChanged.connect(
            lambda: self.doc.set_project_fields({"default_audio_track": self.def_audio.currentData()},
                                                tr("Default audio track")))
        self.def_sub.currentIndexChanged.connect(
            lambda: self.doc.set_project_fields({"default_sub_track": self.def_sub.currentData()},
                                                tr("Default subtitle track")))
        self.out_dir.editingFinished.connect(
            lambda: self.doc.set_project_fields({"output_dir": self.out_dir.text().strip()}, tr("Output folder")))
        browse.clicked.connect(self._browse)
        edit_presets.clicked.connect(self.edit_presets_requested.emit)
        self.mute.toggled.connect(lambda v: self._apply({"muted": v}, tr("Mute")))
        self.audio.currentIndexChanged.connect(lambda: self._apply({"audio_track": self.audio.currentData()},
                                                                   tr("Audio track")))
        self.burn.toggled.connect(lambda v: self._apply({"burn_subs": v}, tr("Burn subtitles")))
        self.sub.currentIndexChanged.connect(lambda: self._apply({"sub_track": self.sub.currentData()},
                                                                 tr("Subtitle track")))
        self.sub_size.editingFinished.connect(lambda: self._apply({"sub_size": self.sub_size.value()},
                                                                  tr("Subtitle size")))
        self.shorts_reset.clicked.connect(lambda: self._apply({"shorts": ShortsFrame()}, tr("Reset Shorts frame")))
        self.crop_mode.currentIndexChanged.connect(self._crop_mode_changed)
        self.crop_aspect.currentIndexChanged.connect(self._crop_aspect_changed)
        self.detect.clicked.connect(lambda: self.detect_crop_requested.emit(list(self.doc.selected_clips)))
        self.crop_preview.toggled.connect(lambda _v: self._emit_preview())
        for sp in self.crop_spins:
            sp.editingFinished.connect(self._crop_spins_changed)
        self.res_combo.currentIndexChanged.connect(self._height_changed)
        self.custom_h.editingFinished.connect(self._height_changed)
        self.preset.currentIndexChanged.connect(lambda: self._apply({"preset_id": self.preset.currentData()},
                                                                    tr("Clip preset")))
        self.reload_sources()

    # ---- sources (presets / tracks) -----------------------------------------------------
    def reload_sources(self) -> None:
        self._updating = True
        info = self.doc.info
        p = self.doc.project
        for combo in (self.def_preset, self.preset):
            combo.blockSignals(True)
            combo.clear()
        self.preset.addItem(tr("Project default"), None)
        for pr in self.presets.all():
            label = tr(pr.name) if pr.builtin else pr.name
            self.def_preset.addItem(label, pr.id)
            self.preset.addItem(label, pr.id)
        for combo in (self.def_preset, self.preset):
            combo.blockSignals(False)
        _combo_set(self.def_preset, p.default_preset_id)

        for combo in (self.def_audio, self.audio):
            combo.blockSignals(True)
            combo.clear()
        self.audio.addItem(tr("Project default"), None)
        self.def_sub.blockSignals(True)
        self.sub.blockSignals(True)
        self.def_sub.clear()
        self.sub.clear()
        self.sub.addItem(tr("Project default"), None)
        if info is not None:
            for a in info.audios:
                self.def_audio.addItem(a.label(), a.rel)
                self.audio.addItem(a.label(), a.rel)
            for s in info.subs:
                lab = s.label() + (f" ({tr('bitmap, cannot burn')})" if s.is_bitmap else "")
                self.def_sub.addItem(lab, s.rel)
                self.sub.addItem(lab, s.rel)
        for combo in (self.def_audio, self.audio, self.def_sub, self.sub):
            combo.blockSignals(False)
        _combo_set(self.def_audio, p.default_audio_track)
        _combo_set(self.def_sub, p.default_sub_track)
        self.out_dir.setText(p.output_dir or self.settings.output_dir)

        self.res_combo.blockSignals(True)
        self.res_combo.clear()
        self.res_combo.addItem(tr("As preset"), None)
        self.res_combo.addItem(tr("Original (no scaling)"), 0)
        src_h = info.video.height if info and info.video else 99999
        for h in available_heights(src_h):
            self.res_combo.addItem(f"{h}p", h)
        self.res_combo.addItem(tr("Custom…"), -1)
        self.res_combo.blockSignals(False)
        self._updating = False
        self.refresh()

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, tr("Output folder"), self.out_dir.text())
        if d:
            self.out_dir.setText(d)
            self.doc.set_project_fields({"output_dir": d}, tr("Output folder"))

    # ---- table --------------------------------------------------------------------------
    def refresh(self) -> None:
        self._updating = True
        p = self.doc.project
        info = self.doc.info
        self.table.blockSignals(True)
        # Columns resize to their contents: every changed cell makes the header re-measure the column,
        # so unchanged cells are left alone and repainting waits until the table is filled.
        self.table.setUpdatesEnabled(False)
        self.table.setRowCount(len(p.clips))
        for row, c in enumerate(p.clips):
            s = summarize(p, self.presets, info, c, self.settings)
            a = effective_audio(p, info, c)
            audio = tr("muted") if c.muted else (f"#{a + 1}" if a is not None else "—")
            subs = ("CC" if c.sub_size == 100 else f"CC {c.sub_size}%") if c.burn_subs else ""
            size = f"{s.height}p" if s.height else tr("source")
            if self.settings.shorts_mode:
                size += " 9:16"
            elif c.crop_mode != "none":
                size += " ✂"
            mode = mode_text(s.mode)
            tip = ""
            if s.reasons:
                mode = "ⓘ " + mode
                tip = tr("Re-encoded because of: ") + ", ".join(reason_text(r) for r in s.reasons)
            if s.notes:
                mode = "⚠ " + mode
                tip = "\n".join([tip] + [warning_text(n) for n in s.notes]).strip()
            preset_name = tr(s.preset.name) if s.preset.builtin else s.preset.name
            if c.preset_id is None:
                preset_name = "· " + preset_name
            vals = [str(row + 1), c.name, fmt_time(c.in_time),
                    fmt_time(c.out_time) if c.out_time is not None else "…",
                    fmt_duration((c.out_time or 0) - c.in_time) if c.out_time is not None else "",
                    audio, subs, size, preset_name, mode]
            for col, v in enumerate(vals):
                it = self.table.item(row, col)
                if it is None:
                    it = QTableWidgetItem()
                    flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                    if col == 1:
                        flags |= Qt.ItemFlag.ItemIsEditable
                    it.setFlags(flags)
                    if col == 0:
                        it.setForeground(Qt.GlobalColor.white)
                    self.table.setItem(row, col, it)
                if it.text() != v:
                    it.setText(v)
                if it.data(Qt.ItemDataRole.UserRole) != c.id:
                    it.setData(Qt.ItemDataRole.UserRole, c.id)
                if col == 0:
                    bg = QColor(c.color or "#4f9dff")
                    if it.background().color() != bg:
                        it.setBackground(bg)
                if col == len(vals) - 1 and it.toolTip() != tip:
                    it.setToolTip(tip)
        self.table.setUpdatesEnabled(True)
        self.table.blockSignals(False)
        self.table.setVisible(bool(p.clips))
        self.empty_hint.setVisible(not p.clips)
        self._updating = False
        self._sync_selection()

    def _sync_selection(self) -> None:
        ids = set(self.doc.selected_clips)
        self.table.blockSignals(True)
        self.table.clearSelection()
        for row in range(self.table.rowCount()):
            it = self.table.item(row, 0)
            if it is not None and it.data(Qt.ItemDataRole.UserRole) in ids:
                self._select_row_add(row)
        self.table.blockSignals(False)
        self._load_props()

    def _select_row_add(self, row: int) -> None:
        sm = self.table.selectionModel()
        idx = self.table.model().index(row, 0)
        sm.select(idx, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)

    def _table_selection(self) -> None:
        ids = []
        for idx in self.table.selectionModel().selectedRows():
            it = self.table.item(idx.row(), 0)
            if it is not None:
                ids.append(it.data(Qt.ItemDataRole.UserRole))
        self.doc.select_clips(ids)

    def _item_edited(self, item: QTableWidgetItem) -> None:
        if self._updating or item.column() != 1:
            return
        cid = item.data(Qt.ItemDataRole.UserRole)
        name = item.text().strip()
        if name:
            self.doc.set_clip_fields(cid, {"name": name}, tr("Rename clip"))

    def _double_clicked(self, row: int, col: int) -> None:
        if col == 1:
            return
        it = self.table.item(row, 0)
        c = self.doc.project.clip_by_id(it.data(Qt.ItemDataRole.UserRole)) if it else None
        if c is not None:
            self.seek_requested.emit(c.in_time)

    # ---- properties ---------------------------------------------------------------------
    def _selected(self):
        return [c for c in (self.doc.project.clip_by_id(i) for i in self.doc.selected_clips) if c is not None]

    def _load_props(self) -> None:
        shorts = self.settings.shorts_mode
        self._form.setRowVisible(self.shorts_row, shorts)
        clips = self._selected()
        # The ratio and the rectangle only take room while a crop is on (fits 1080 px screens without
        # scrolling); auto mode shows the detected rectangle, only manual mode has a ratio.
        mode = clips[0].crop_mode if clips and not shorts else "none"
        self._form.setRowVisible(self.crop_aspect, mode == "manual")
        self._form.setRowVisible(self.spin_row, mode != "none")
        self.props.setEnabled(bool(clips))
        self.props.setTitle(tr("Selected clip") if len(clips) <= 1 else
                            tr("Selected clips") + f" ({len(clips)})")
        if not clips:
            self.info.setText(tr("Select a clip in the table or on the timeline."))
            self.preview_crop.emit(None)
            return
        c = clips[0]
        self._updating = True
        for w in (self.mute, self.burn):
            w.blockSignals(True)
        self.mute.setChecked(c.muted)
        self.burn.setChecked(c.burn_subs)
        for w in (self.mute, self.burn):
            w.blockSignals(False)
        _combo_set(self.audio, c.audio_track)
        _combo_set(self.sub, c.sub_track)
        self.sub_size.blockSignals(True)
        self.sub_size.setValue(c.sub_size)
        self.sub_size.blockSignals(False)
        _combo_set(self.crop_mode, c.crop_mode)
        rect = c.crop or CropRect()
        for sp, v in zip(self.crop_spins, (rect.x, rect.y, rect.w, rect.h)):
            sp.blockSignals(True)
            sp.setValue(v)
            sp.setEnabled(c.crop_mode == "manual" and not shorts)
            sp.blockSignals(False)
        # Shorts Mode: the Shorts frame decides the picture, the crop tool is off (crops are kept)
        self.crop_mode.setEnabled(not shorts)
        self.crop_mode.setToolTip(tr("Not available in Shorts Mode: the Shorts frame sets the exported picture.")
                                  if shorts else tr("Remove black bars. Cropping requires re-encoding."))
        self.crop_preview.setEnabled(not shorts)
        _combo_set(self.crop_aspect, c.crop_aspect)
        self.crop_aspect.setEnabled(c.crop_mode == "manual" and not shorts)
        self.detect.setEnabled(c.crop_mode != "none" and not shorts)
        if c.target_height is None or c.target_height == 0 or self.res_combo.findData(c.target_height) >= 0:
            _combo_set(self.res_combo, c.target_height)
            self.custom_h.setVisible(False)
        else:
            _combo_set(self.res_combo, -1)
            self.custom_h.setVisible(True)
            self.custom_h.blockSignals(True)
            self.custom_h.setValue(c.target_height)
            self.custom_h.blockSignals(False)
        _combo_set(self.preset, c.preset_id)
        self.audio.setEnabled(not c.muted)
        self.sub.setEnabled(c.burn_subs)
        self.sub_size.setEnabled(c.burn_subs)
        self._updating = False
        s = summarize(self.doc.project, self.presets, self.doc.info, c, self.settings)
        frame = effective_crop(c, self.doc.info, True) if shorts else None
        if frame is not None:
            w, h = frame.w, frame.h
            if s.height:
                w, h = scaled_width(s.height, w, h), s.height
            self.shorts_size.setText(f"{w}×{h}")
        lines = [f"{tr('Mode')}: {mode_text(s.mode)}"]
        if s.reasons:
            lines.append("ⓘ " + tr("Re-encoded because of: ") + ", ".join(reason_text(r) for r in s.reasons))
        if s.preset.kind == "video" and s.preset.target_size_mb:
            lines.append(tr("Target size") + f": {s.preset.target_size_mb:g} MB → "
                         + (f"{s.height}p" if s.height else tr("source resolution")))
        for n in s.notes:
            lines.append("⚠ " + warning_text(n))
        self.info.setText("\n".join(lines))
        self._emit_preview()

    def _apply(self, fields: dict, text: str) -> None:
        if self._updating:
            return
        self.doc.update_clips(list(self.doc.selected_clips), fields, text)

    def _crop_mode_changed(self) -> None:
        if self._updating:
            return
        mode = self.crop_mode.currentData()
        fields = {"crop_mode": mode}
        clips = self._selected()
        if mode == "manual" and clips and not (clips[0].crop and clips[0].crop.is_valid()) and self.doc.info:
            v = self.doc.info.video
            fields["crop"] = CropRect(0, 0, v.width, v.height)
        self._apply(fields, tr("Crop mode"))
        if mode == "auto" and clips and not (clips[0].crop and clips[0].crop.is_valid()):
            self.detect_crop_requested.emit(list(self.doc.selected_clips))

    def _crop_spins_changed(self) -> None:
        if self._updating:
            return
        x, y, w, h = (sp.value() // 2 * 2 for sp in self.crop_spins)
        clips = self._selected()
        if self.doc.info and self.doc.info.video:
            v = self.doc.info.video
            dar = framing.display_aspect(v.width, v.height, v.sar)
            aspect = framing.parse_aspect(clips[0].crop_aspect, dar) if clips else None
            old = clips[0].crop if clips else None
            if aspect and old is not None and old.is_valid():
                # a locked ratio: the other side follows the one that was typed in
                sar = framing.parse_sar(v.sar)
                if h != old.h and w == old.w:
                    w = h * aspect / sar
                else:
                    h = w * sar / aspect
                r = framing.clamp_rect((x / v.width, y / v.height, w / v.width, h / v.height), dar, aspect)
                self._apply({"crop": framing.to_crop(r, v.width, v.height, v.sar, aspect), "crop_mode": "manual"},
                            tr("Crop"))
                return
            w = min(w, v.width - x)
            h = min(h, v.height - y)
        self._apply({"crop": CropRect(x, y, w, h), "crop_mode": "manual"}, tr("Crop"))

    def _crop_aspect_changed(self) -> None:
        """A new ratio: each selected clip's crop becomes the largest rectangle with it inside the old one."""
        if self._updating or not (self.doc.info and self.doc.info.video):
            return
        key = self.crop_aspect.currentData()
        v = self.doc.info.video
        dar = framing.display_aspect(v.width, v.height, v.sar)
        aspect = framing.parse_aspect(key, dar)
        clips = self._selected()
        self.doc.undo.beginMacro(tr("Crop aspect ratio"))
        try:
            for c in clips:
                fields = {"crop_aspect": key}
                if aspect:
                    r = (framing.from_crop(c.crop, v.width, v.height) if c.crop and c.crop.is_valid()
                         else (0.0, 0.0, 1.0, 1.0))
                    fields["crop"] = framing.to_crop(framing.fit_aspect_inside(r, aspect, dar), v.width, v.height,
                                                     v.sar, aspect)
                self.doc.set_clip_fields(c.id, fields, tr("Crop aspect ratio"))
        finally:
            self.doc.undo.endMacro()

    def _height_changed(self) -> None:
        if self._updating:
            return
        data = self.res_combo.currentData()
        self.custom_h.setVisible(data == -1)
        if data == -1:
            src_h = self.doc.info.video.height if self.doc.info and self.doc.info.video else 4320
            val = min(self.custom_h.value(), src_h - 2) // 2 * 2
            self._apply({"target_height": val}, tr("Resolution"))
        else:
            self._apply({"target_height": data}, tr("Resolution"))

    def _emit_preview(self) -> None:
        clips = self._selected()
        if not self.settings.shorts_mode and self.crop_preview.isChecked() and clips and clips[0].crop_mode != "none" and clips[0].crop \
                and clips[0].crop.is_valid():
            self.preview_crop.emit(clips[0].crop)
        else:
            self.preview_crop.emit(None)
