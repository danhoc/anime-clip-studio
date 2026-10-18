"""Application settings dialog."""

from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from acs.core import uiscale
from acs.core.settings import DEFAULT_STEPS, parse_step, step_spec
from acs.core.thumbs import QUALITY_CHOICES as QUALITY_THUMBS
from acs.core.waveform import QUALITY_CHOICES as QUALITY_WAVE
from acs.ui import theme
from acs.i18n import LANGUAGES, tr
from acs.ui.tools_panel import ToolsPanel


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Settings"))
        self.resize(760, 620)
        self.s = settings
        lay = QVBoxLayout(self)
        f = QFormLayout()

        def path_row(value: str, file: bool, filt: str = "") -> tuple[QHBoxLayout, QLineEdit]:
            row = QHBoxLayout()
            le = QLineEdit(value)
            b = QPushButton("…")
            b.setFixedWidth(28)

            def pick():
                if file:
                    p, _ = QFileDialog.getOpenFileName(self, "", le.text(), filt)
                else:
                    p = QFileDialog.getExistingDirectory(self, "", le.text())
                if p:
                    le.setText(p)

            b.clicked.connect(pick)
            row.addWidget(le, 1)
            row.addWidget(b)
            return row, le

        r, self.output = path_row(settings.output_dir, False)
        f.addRow(tr("Default output folder"), r)
        self.lang = QComboBox()
        for k, v in LANGUAGES.items():
            self.lang.addItem(v, k)
        self.lang.setCurrentIndex(max(0, self.lang.findData(settings.language)))
        f.addRow(tr("Language (after restart)"), self.lang)
        self.scale = QComboBox()
        auto = uiscale.auto_scale()
        m = uiscale.display_metrics()
        for choice in uiscale.SCALE_CHOICES:
            if choice == "auto":
                label = tr("Auto for this display") + f" ({auto * 100:.0f}%)"
            else:
                label = f"{float(choice) * 100:.0f}%"
            self.scale.addItem(label, choice)
        self.scale.setCurrentIndex(max(0, self.scale.findData(settings.ui_scale)))
        tip = tr("Makes all text, buttons and the timeline larger. Applied after restart.")
        if m is not None:
            tip += "\n" + tr("Display:") + f" {m[0]}×{m[1]}, Windows {m[2] * 100:.0f}%"
        self.scale.setToolTip(tip)
        f.addRow(tr("Interface size (after restart)"), self.scale)
        self.theme = QComboBox()
        for key, label in (("dark", "Dark"), ("light", "Light"), ("system", "As in Windows")):
            self.theme.addItem(tr(label), key)
        self.theme.setCurrentIndex(max(0, self.theme.findData(settings.theme)))
        f.addRow(tr("Theme (after restart)"), self.theme)
        dpr = self.devicePixelRatioF()
        self.thumbq = QComboBox()
        self.thumbq.addItem(tr("Auto for this display") + f" ({uiscale.auto_thumb_height(dpr)} px)", "auto")
        for h in QUALITY_THUMBS:
            self.thumbq.addItem(f"{h} px", str(h))
        self.thumbq.setCurrentIndex(max(0, self.thumbq.findData(settings.thumb_quality)))
        self.thumbq.setToolTip(tr("Height of the timeline thumbnails. Higher = sharper on 4K screens, "
                                  "a bit slower to generate the first time."))
        f.addRow(tr("Timeline thumbnails"), self.thumbq)
        self.waveq = QComboBox()
        self.waveq.addItem(tr("Auto for this display") + f" ({uiscale.auto_wave_bps(dpr)}/s)", "auto")
        for b in QUALITY_WAVE:
            self.waveq.addItem(tr("{n} points per second").format(n=b), str(b))
        self.waveq.setCurrentIndex(max(0, self.waveq.findData(settings.wave_quality)))
        self.waveq.setToolTip(tr("Detail of the audio waveform. Higher = finer when zoomed in."))
        f.addRow(tr("Waveform detail"), self.waveq)
        self.audio_langs = QLineEdit(", ".join(settings.preferred_audio_langs))
        self.audio_langs.setToolTip(tr("Preferred audio languages in order, e.g. jpn, rus"))
        f.addRow(tr("Preferred audio languages"), self.audio_langs)
        self.sub_langs = QLineEdit(", ".join(settings.preferred_sub_langs))
        f.addRow(tr("Preferred subtitle languages"), self.sub_langs)
        self.hwdec = QCheckBox(tr("Use hardware decoding (GPU)"))
        self.hwdec.setChecked(settings.hwdec)
        f.addRow(self.hwdec)
        self.par_cpu = QSpinBox()
        self.par_cpu.setRange(1, 8)
        self.par_cpu.setValue(settings.parallel_cpu)
        self.par_cpu.setToolTip(tr("CPU encoders already use all cores; 1 is usually best"))
        f.addRow(tr("Parallel CPU jobs"), self.par_cpu)
        self.par_gpu = QSpinBox()
        self.par_gpu.setRange(1, 8)
        self.par_gpu.setValue(settings.parallel_gpu)
        f.addRow(tr("Parallel GPU (NVENC) jobs"), self.par_gpu)
        self.bpp264 = QDoubleSpinBox()
        self.bpp264.setRange(0.005, 1)
        self.bpp264.setDecimals(3)
        self.bpp264.setSingleStep(0.005)
        self.bpp264.setValue(settings.bpp_x264)
        self.bpp264.setToolTip(tr("Bits per pixel below which H.264 starts to look bad (used for auto downscale)"))
        f.addRow(tr("Min bits/pixel H.264"), self.bpp264)
        self.bpp265 = QDoubleSpinBox()
        self.bpp265.setRange(0.005, 1)
        self.bpp265.setDecimals(3)
        self.bpp265.setSingleStep(0.005)
        self.bpp265.setValue(settings.bpp_hevc_av1)
        f.addRow(tr("Min bits/pixel HEVC/AV1"), self.bpp265)
        self.exts = QLineEdit(", ".join(settings.video_exts))
        f.addRow(tr("Video file extensions"), self.exts)
        self.steps: dict[str, tuple[QDoubleSpinBox, QComboBox]] = {}
        for mod, keys in (("arrow", "← / →"), ("shift", "Shift + ← / →"), ("ctrl", "Ctrl + ← / →")):
            row, spin, unit = self._step_row(getattr(settings, f"step_{mod}"), DEFAULT_STEPS[mod])
            f.addRow(tr("Step {keys}").format(keys=keys), row)
            self.steps[mod] = (spin, unit)
        self.quick_help = QCheckBox(tr("Show the quick help at start"))
        self.quick_help.setChecked(settings.show_quick_help)
        self.quick_help.setToolTip(tr("Main keys and tips; also in Help → Quick help"))
        f.addRow(self.quick_help)
        self.tour = QCheckBox(tr("Show the introduction tour on next start"))
        self.tour.setChecked(settings.show_tour)
        f.addRow(self.tour)
        tabs = QTabWidget()
        general = QWidget()
        general.setLayout(f)
        tabs.addTab(general, tr("General"))
        self.tools = ToolsPanel(settings)
        tools_page = QWidget()
        tl = QVBoxLayout(tools_page)
        tl.addWidget(self.tools)
        tl.addStretch(1)
        tabs.addTab(tools_page, tr("External tools"))
        if not self.tools.all_ok():
            tabs.setCurrentWidget(tools_page)
        lay.addWidget(tabs, 1)
        note = QLabel(tr("Changes to language, theme, interface size or libmpv take effect after restarting the app. "
                         "Thumbnail / waveform quality applies to the next opened video."))
        note.setWordWrap(True)
        note.setStyleSheet(theme.css_info())
        lay.addWidget(note)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    @staticmethod
    def _step_row(spec: str, default: str) -> tuple[QHBoxLayout, QDoubleSpinBox, QComboBox]:
        """Arrow-key step: a number of frames or seconds, or "to keyframe"."""
        kind, value = parse_step(spec, default)
        spin = QDoubleSpinBox()
        unit = QComboBox()
        for key, label in (("frames", "frames"), ("seconds", "seconds"), ("keyframe", "to the next keyframe")):
            unit.addItem(tr(label), key)
        unit.setToolTip(tr("Frames step exactly; seconds go to the frame nearest that time; "
                           "keyframe jumps to the next / previous keyframe."))

        def on_unit():
            k = unit.currentData()
            spin.setEnabled(k != "keyframe")
            if k == "frames":
                spin.setDecimals(0)
                spin.setRange(1, 10000)
                spin.setSingleStep(1)
            else:
                spin.setDecimals(2)
                spin.setRange(0.04, 3600)
                spin.setSingleStep(0.5)

        spin.setDecimals(2)
        spin.setRange(0, 10000)
        spin.setFixedWidth(spin.sizeHint().width())   # same width for frames and seconds: rows line up
        unit.currentIndexChanged.connect(on_unit)
        unit.setCurrentIndex(max(0, unit.findData(kind)))
        on_unit()
        spin.setValue(value)
        row = QHBoxLayout()
        row.addWidget(spin)
        row.addWidget(unit, 1)
        return row, spin, unit

    @staticmethod
    def _split(text: str) -> list[str]:
        return [x.strip().lstrip(".") for x in text.replace(";", ",").split(",") if x.strip()]

    def _ok(self) -> None:
        s = self.s
        self.libmpv_changed = self.tools.apply()
        s.output_dir = self.output.text().strip()
        s.language = self.lang.currentData()
        s.ui_scale = self.scale.currentData()
        s.theme = self.theme.currentData()
        s.thumb_quality = self.thumbq.currentData()
        s.wave_quality = self.waveq.currentData()
        s.preferred_audio_langs = self._split(self.audio_langs.text())
        s.preferred_sub_langs = self._split(self.sub_langs.text())
        s.hwdec = self.hwdec.isChecked()
        s.parallel_cpu, s.parallel_gpu = self.par_cpu.value(), self.par_gpu.value()
        s.bpp_x264, s.bpp_hevc_av1 = self.bpp264.value(), self.bpp265.value()
        s.video_exts = self._split(self.exts.text()) or s.video_exts
        s.show_tour = self.tour.isChecked()
        s.show_quick_help = self.quick_help.isChecked()
        for mod, (spin, unit) in self.steps.items():
            setattr(s, f"step_{mod}", step_spec(unit.currentData(), spin.value()))
        s.resolved()
        s.save()
        self.accept()
