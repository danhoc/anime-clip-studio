"""Clip table + properties of the selected clip(s) + project defaults."""

from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QSpinBox,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from acs.core.clipinfo import effective_audio, effective_preset, summarize
from acs.core.document import ProjectDocument
from acs.core.model import CropRect
from acs.core.ffmpeg_cmd import available_heights
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

        # ---- project defaults row ----
        defaults = QHBoxLayout()
        self.def_preset = QComboBox()
        self.def_preset.setToolTip(tr("Preset used by clips that do not override it"))
        self.def_audio = QComboBox()
        self.def_audio.setToolTip(tr("Audio track used by clips that do not override it"))
        self.def_sub = QComboBox()
        self.def_sub.setToolTip(tr("Subtitle track burned in when a clip enables subtitles"))
        self.out_dir = QLineEdit()
        self.out_dir.setPlaceholderText(tr("Output folder"))
        self.out_dir.setToolTip(tr("Where exported clips are saved"))
        self.out_dir.setMinimumWidth(180)
        browse = QPushButton("…")
        browse.setFixedWidth(28)
        browse.setToolTip(tr("Choose output folder"))
        edit_presets = QPushButton(tr("Presets…"))
        edit_presets.setToolTip(tr("Create and edit your own export presets"))
        defaults.addWidget(QLabel(tr("Default preset:")))
        defaults.addWidget(self.def_preset, 3)
        defaults.addWidget(edit_presets)
        defaults.addWidget(QLabel(tr("Audio:")))
        defaults.addWidget(self.def_audio, 2)
        defaults.addWidget(QLabel(tr("Subtitles:")))
        defaults.addWidget(self.def_sub, 2)
        defaults.addWidget(QLabel(tr("Output:")))
        defaults.addWidget(self.out_dir, 3)
        defaults.addWidget(browse)
        root.addLayout(defaults)


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
        self.mute = QCheckBox(tr("Mute audio"))
        self.mute.setToolTip(tr("Export this clip without any audio track"))
        self.audio = QComboBox()
        self.audio.setToolTip(tr("Audio track for this clip"))
        self.burn = QCheckBox(tr("Burn in subtitles"))
        self.burn.setToolTip(tr("Render the subtitles into the picture (requires re-encoding)"))
        self.sub = QComboBox()
        self.sub.setToolTip(tr("Subtitle track to burn in"))
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
        self.crop_spins = []
        spin_row = QHBoxLayout()
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
        form.addRow(tr("Crop"), crop_row)
        form.addRow("", spin_row)
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
        self.crop_mode.currentIndexChanged.connect(self._crop_mode_changed)
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
        self.table.setRowCount(len(p.clips))
        for row, c in enumerate(p.clips):
            s = summarize(p, self.presets, info, c, self.settings)
            a = effective_audio(p, info, c)
            audio = tr("muted") if c.muted else (f"#{a + 1}" if a is not None else "—")
            subs = "CC" if c.burn_subs else ""
            size = f"{s.height}p" if s.height else tr("source")
            if c.crop_mode != "none":
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
                    self.table.setItem(row, col, it)
                it.setText(v)
                it.setData(Qt.ItemDataRole.UserRole, c.id)
                flags = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                if col == 1:
                    flags |= Qt.ItemFlag.ItemIsEditable
                it.setFlags(flags)
                if col == 0:
                    it.setForeground(Qt.GlobalColor.white)
                    it.setBackground(QColor(c.color or "#4f9dff"))
                if col == len(vals) - 1:
                    it.setToolTip(tip)
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
        clips = self._selected()
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
        _combo_set(self.crop_mode, c.crop_mode)
        rect = c.crop or CropRect()
        for sp, v in zip(self.crop_spins, (rect.x, rect.y, rect.w, rect.h)):
            sp.blockSignals(True)
            sp.setValue(v)
            sp.setEnabled(c.crop_mode == "manual")
            sp.blockSignals(False)
        self.detect.setEnabled(c.crop_mode != "none")
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
        self._updating = False
        s = summarize(self.doc.project, self.presets, self.doc.info, c, self.settings)
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
        if self.doc.info and self.doc.info.video:
            v = self.doc.info.video
            w = min(w, v.width - x)
            h = min(h, v.height - y)
        self._apply({"crop": CropRect(x, y, w, h), "crop_mode": "manual"}, tr("Crop"))

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
        if self.crop_preview.isChecked() and clips and clips[0].crop_mode != "none" and clips[0].crop \
                and clips[0].crop.is_valid():
            self.preview_crop.emit(clips[0].crop)
        else:
            self.preview_crop.emit(None)
