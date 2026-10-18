"""Extract-frames dialog."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QRadioButton, QSpinBox, QVBoxLayout)

from acs.core.frames import DEFAULT_TEMPLATE, FramesConfig, default_frames_dir, estimate_count, template_problem
from acs.core.probe import MediaInfo
from acs.core.util import fmt_time
from acs.i18n import tr


class FramesDialog(QDialog):
    def __init__(self, src: str, info: MediaInfo, settings, markers=None, clips=None, selected_clip_ids=None,
                 duration: float = 0.0, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Extract frames"))
        self.src, self.info, self.settings = src, info, settings
        self.markers = list(markers or [])
        self.clips = [c for c in (clips or []) if c.is_complete]
        self.selected_ids = set(selected_clip_ids or [])
        self.duration = duration or info.duration
        last = settings.frames_last or {}
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Video:") + " " + Path(src).name))

        # ---- range ----
        rg = QGroupBox(tr("Range"))
        rl = QVBoxLayout(rg)
        self.r_all = QRadioButton(tr("Whole video"))
        self.r_markers = QRadioButton(tr("Between two markers"))
        self.r_sel = QRadioButton(tr("Inside selected clips"))
        self.r_clips = QRadioButton(tr("Inside all clips"))
        self.range_group = QButtonGroup(self)
        for i, r in enumerate((self.r_all, self.r_markers, self.r_sel, self.r_clips)):
            self.range_group.addButton(r, i)
        self.m_from, self.m_to = QComboBox(), QComboBox()
        for m in self.markers:
            label = f"{fmt_time(m.time)} {m.label}".strip()
            self.m_from.addItem(label, m.time)
            self.m_to.addItem(label, m.time)
        if len(self.markers) >= 2:
            self.m_to.setCurrentIndex(len(self.markers) - 1)
        mrow = QHBoxLayout()
        mrow.addWidget(self.r_markers)
        mrow.addWidget(self.m_from)
        mrow.addWidget(QLabel("→"))
        mrow.addWidget(self.m_to)
        rl.addWidget(self.r_all)
        rl.addLayout(mrow)
        rl.addWidget(self.r_sel)
        rl.addWidget(self.r_clips)
        self.r_markers.setEnabled(len(self.markers) >= 2)
        self.r_sel.setEnabled(any(c.id in self.selected_ids for c in self.clips))
        self.r_clips.setEnabled(bool(self.clips))
        self.r_all.setChecked(True)
        lay.addWidget(rg)

        # ---- mode ----
        mg = QGroupBox(tr("Which frames"))
        ml = QFormLayout(mg)
        self.mode = QComboBox()
        self.mode.addItem(tr("Every N seconds"), "interval")
        self.mode.addItem(tr("One per scene change"), "scene")
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.04, 3600)
        self.interval.setDecimals(2)
        self.interval.setSuffix(" s")
        self.interval.setValue(float(last.get("interval", 2.0)))
        self.interval.setToolTip(tr("Take the first frame of every N-second interval"))
        self.scene = QSpinBox()
        self.scene.setRange(1, 60)
        self.scene.setValue(int(last.get("scene_threshold", 10)))
        self.scene.setToolTip(tr("Scene change sensitivity: lower = more frames"))
        ml.addRow(tr("Mode"), self.mode)
        ml.addRow(tr("Interval"), self.interval)
        ml.addRow(tr("Scene threshold"), self.scene)
        lay.addWidget(mg)

        # ---- dedup ----
        dg = QGroupBox(tr("Skip duplicates"))
        dl = QFormLayout(dg)
        self.dedup = QComboBox()
        self.dedup.addItem(tr("Keep everything"), "none")
        self.dedup.addItem(tr("Drop exact and near duplicates"), "strict")
        self.dedup.addItem(tr("Drop all similar neighbouring frames"), "similar")
        self.dedup.setToolTip(tr("Anime has many static shots — this avoids saving the same picture many times"))
        self.strict = QDoubleSpinBox()
        self.strict.setRange(0.1, 10)
        self.strict.setValue(float(last.get("strict_thresh", 1.0)))
        self.strict.setToolTip(tr("Max average pixel difference (0–255) to count as a duplicate"))
        self.similar = QDoubleSpinBox()
        self.similar.setRange(1, 40)
        self.similar.setValue(float(last.get("similar_thresh", 6.0)))
        self.similar.setToolTip(tr("Higher = more frames treated as similar"))
        self.window = QSpinBox()
        self.window.setRange(1, 20)
        self.window.setValue(int(last.get("similar_window", 5)))
        self.window.setToolTip(tr("Compare with this many previously kept frames"))
        dl.addRow(tr("Strategy"), self.dedup)
        dl.addRow(tr("Duplicate threshold"), self.strict)
        dl.addRow(tr("Similarity threshold"), self.similar)
        dl.addRow(tr("Compare with last N kept"), self.window)
        lay.addWidget(dg)

        # ---- output ----
        og = QGroupBox(tr("Output"))
        ol = QFormLayout(og)
        self.fmt = QComboBox()
        for key, label in (("png", "PNG (lossless)"), ("png16", "PNG 16-bit (keeps 10-bit detail)"),
                           ("jpg", "JPEG"), ("webp", "WebP")):
            self.fmt.addItem(tr(label), key)
        self.quality = QSpinBox()
        self.quality.setRange(1, 100)
        self.quality.setValue(int(last.get("quality", 92)))
        self.quality.setToolTip(tr("JPEG / WebP quality"))
        self.lossless = QCheckBox(tr("Lossless WebP"))
        self.lossless.setChecked(bool(last.get("webp_lossless", False)))
        self.subs = QCheckBox(tr("Draw subtitles"))
        self.sub_track = QComboBox()
        for s in info.subs:
            if not s.is_bitmap:
                self.sub_track.addItem(s.label(), s.rel)
        self.subs.setEnabled(self.sub_track.count() > 0)
        srow = QHBoxLayout()
        srow.addWidget(self.subs)
        srow.addWidget(self.sub_track, 1)
        orow = QHBoxLayout()
        self.out = QLineEdit(last.get("out_dir") or default_frames_dir(settings.output_dir, src))
        b = QPushButton("…")
        b.setFixedWidth(28)
        b.clicked.connect(self._browse)
        orow.addWidget(self.out, 1)
        orow.addWidget(b)
        self.template = QLineEdit(settings.frames_template)
        self.template.setToolTip(tr("Variables: {video} {time} {frame}"))
        ol.addRow(tr("Format"), self.fmt)
        ol.addRow(tr("Quality"), self.quality)
        ol.addRow("", self.lossless)
        ol.addRow(tr("Subtitles"), srow)
        ol.addRow(tr("Folder"), orow)
        ol.addRow(tr("File name"), self.template)
        lay.addWidget(og)

        self.estimate = QLabel()
        lay.addWidget(self.estimate)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Start"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        i = self.mode.findData(last.get("mode", "interval"))
        self.mode.setCurrentIndex(max(0, i))
        i = self.dedup.findData(last.get("dedup", "none"))
        self.dedup.setCurrentIndex(max(0, i))
        i = self.fmt.findData(last.get("fmt", "png"))
        self.fmt.setCurrentIndex(max(0, i))
        for w in (self.mode, self.dedup, self.fmt):
            w.currentIndexChanged.connect(self._update)
        for w in (self.interval,):
            w.valueChanged.connect(self._update)
        self.range_group.idToggled.connect(lambda *_: self._update())
        self.subs.toggled.connect(self._update)
        self._update()

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, tr("Folder"), self.out.text())
        if d:
            self.out.setText(d)

    def _ranges(self) -> list[tuple[float, float]]:
        rid = self.range_group.checkedId()
        if rid == 1 and len(self.markers) >= 2:
            a, b = sorted((self.m_from.currentData(), self.m_to.currentData()))
            return [(a, b)] if b > a else [(0.0, self.duration)]
        if rid == 2:
            return [(c.in_time, c.out_time) for c in self.clips if c.id in self.selected_ids]
        if rid == 3:
            return [(c.in_time, c.out_time) for c in self.clips]
        return [(0.0, self.duration)]

    def _update(self) -> None:
        mode = self.mode.currentData()
        self.interval.setEnabled(mode == "interval")
        self.scene.setEnabled(mode == "scene")
        d = self.dedup.currentData()
        self.strict.setEnabled(d == "strict")
        self.similar.setEnabled(d == "similar")
        self.window.setEnabled(d == "similar")
        fmt = self.fmt.currentData()
        self.quality.setEnabled(fmt in ("jpg", "webp"))
        self.lossless.setEnabled(fmt == "webp")
        self.sub_track.setEnabled(self.subs.isChecked())
        n = estimate_count(self.config())
        self.estimate.setText(tr("About {n} frames before de-duplication").format(n=n) if n else
                              tr("Number of frames depends on the video"))

    def config(self) -> FramesConfig:
        return FramesConfig(
            src=self.src, ranges=self._ranges(), out_dir=self.out.text().strip(),
            mode=self.mode.currentData(), interval=self.interval.value(), scene_threshold=self.scene.value(),
            fmt=self.fmt.currentData(), quality=self.quality.value(), webp_lossless=self.lossless.isChecked(),
            sub_rel=self.sub_track.currentData() if self.subs.isChecked() and self.sub_track.count() else None,
            dedup=self.dedup.currentData(), strict_thresh=self.strict.value(),
            similar_thresh=self.similar.value(), similar_window=self.window.value(),
            template=self.template.text() or DEFAULT_TEMPLATE, hwaccel=self.settings.hwdec,
            fps=float(self.info.fps),
        )

    def remember(self) -> dict:
        c = self.config()
        return {"interval": c.interval, "scene_threshold": c.scene_threshold, "mode": c.mode, "dedup": c.dedup,
                "fmt": c.fmt, "quality": c.quality, "webp_lossless": c.webp_lossless, "strict_thresh": c.strict_thresh,
                "similar_thresh": c.similar_thresh, "similar_window": c.similar_window}

    def accept(self) -> None:
        problem = template_problem(self.template.text() or DEFAULT_TEMPLATE)
        if problem == "invalid":
            QMessageBox.warning(self, self.windowTitle(),
                                tr("The file name template has an unknown variable or a stray brace. "
                                   "Variables: {video} {time} {frame}"))
            return
        if problem == "not_unique":
            QMessageBox.warning(self, self.windowTitle(),
                                tr("The file name template needs {time} or {frame}, otherwise every frame "
                                   "gets the same name."))
            return
        super().accept()
