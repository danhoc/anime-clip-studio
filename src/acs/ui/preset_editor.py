"""Preset editor: built-in presets are read-only (duplicate to edit), user presets are editable."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from acs.core.model import new_id
from acs.core.presets import (ANIM_HEIGHT_STEPS, AUDIO_CODECS, CODECS, CONTAINERS, GIF_DITHERS, HEIGHT_STEPS,
                              Preset, PresetStore)
from acs.ui import theme
from acs.i18n import tr

CODEC_HELP = {
    "libx264": "H.264 (x264): plays everywhere. CRF: lower = better quality and bigger file (18 ≈ visually lossless).",
    "libx265": "HEVC (x265): ~40% smaller than H.264 at the same quality. 10-bit avoids banding in gradients.",
    "libsvtav1": "AV1 (SVT-AV1): modern and efficient, fast for AV1. CRF ~25–32 is typical. Preset: lower = slower/better.",
    "libaom-av1": "AV1 (libaom): best AV1 quality per bit, but very slow.",
    "librav1e": "AV1 (rav1e): alternative AV1 encoder; quantizer 0–255.",
    "libvpx-vp9": "VP9: WebM friendly, good browser support.",
    "libvvenc": "VVC / H.266 (vvenc): newest codec, smallest files, but few players support it yet.",
    "h264_nvenc": "H.264 on the NVIDIA GPU: very fast, slightly larger files than x264.",
    "hevc_nvenc": "HEVC on the NVIDIA GPU: very fast, 10-bit supported.",
    "av1_nvenc": "AV1 on the NVIDIA GPU (RTX 40xx): very fast modern codec, 10-bit supported.",
}


class PresetEditor(QDialog):
    def __init__(self, store: PresetStore, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Export presets"))
        self.resize(860, 600)
        self.store = store
        self.current: Preset | None = None
        self._loading = False
        lay = QHBoxLayout(self)
        left = QVBoxLayout()
        self.list = QListWidget()
        left.addWidget(self.list, 1)
        brow = QHBoxLayout()
        self.b_dup = QPushButton(tr("Duplicate"))
        self.b_dup.setToolTip(tr("Copy the selected preset so you can edit it"))
        self.b_new_v = QPushButton(tr("New video"))
        self.b_new_g = QPushButton(tr("New GIF"))
        self.b_new_w = QPushButton(tr("New WebP"))
        self.b_del = QPushButton(tr("Delete"))
        for b in (self.b_dup, self.b_new_v, self.b_new_g, self.b_new_w, self.b_del):
            brow.addWidget(b)
        left.addLayout(brow)
        lay.addLayout(left, 2)

        right = QVBoxLayout()
        self.ro_label = QLabel(tr("Built-in preset (read-only). Duplicate it to make your own version."))
        self.ro_label.setStyleSheet("color: #e0b341;")
        right.addWidget(self.ro_label)
        self.name = QLineEdit()
        nrow = QFormLayout()
        nrow.addRow(tr("Name"), self.name)
        right.addLayout(nrow)
        self.stack = QStackedWidget()
        self.video_page = self._build_video_page()
        self.anim_page = self._build_anim_page()
        self.stack.addWidget(self.video_page)
        self.stack.addWidget(self.anim_page)
        right.addWidget(self.stack, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close)
        self.b_save = bb.button(QDialogButtonBox.StandardButton.Save)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        right.addWidget(bb)
        lay.addLayout(right, 3)

        self.list.currentItemChanged.connect(lambda *_: self._select())
        self.b_dup.clicked.connect(self._duplicate)
        self.b_new_v.clicked.connect(lambda: self._new("video"))
        self.b_new_g.clicked.connect(lambda: self._new("gif"))
        self.b_new_w.clicked.connect(lambda: self._new("webp"))
        self.b_del.clicked.connect(self._delete)
        self._fill()

    # ---- pages ----
    def _build_video_page(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.v_mode = QComboBox()
        self.v_mode.addItem(tr("Re-encode"), "encode")
        self.v_mode.addItem(tr("Original (smart cut)"), "smartcut")
        self.v_mode.setToolTip(tr("Smart cut keeps the original quality and re-encodes only the clip edges"))
        self.v_codec = QComboBox()
        for k in CODECS:
            self.v_codec.addItem(k, k)
        self.v_codec_help = QLabel()
        self.v_codec_help.setWordWrap(True)
        self.v_codec_help.setStyleSheet(theme.css_info())
        self.v_depth = QComboBox()
        self.v_depth.addItem("8-bit", 8)
        self.v_depth.addItem("10-bit", 10)
        self.v_depth.setToolTip(tr("10-bit keeps smooth gradients (less banding), recommended for anime"))
        self.v_quality = QSpinBox()
        self.v_quality.setToolTip(tr("CRF / CQ: lower = better quality, bigger file"))
        self.v_speed = QComboBox()
        self.v_speed.setToolTip(tr("Encoder speed preset: slower = smaller file at the same quality"))
        self.v_tune = QComboBox()
        self.v_tune.setToolTip(tr("'animation' tune is tuned for cartoons/anime"))
        self.v_container = QComboBox()
        for c in CONTAINERS:
            self.v_container.addItem(c.upper(), c)
        self.v_container.setToolTip(tr("MP4 plays everywhere; MKV can keep any audio (FLAC, AC3); WebM for the web"))
        self.v_acodec = QComboBox()
        for a in AUDIO_CODECS:
            self.v_acodec.addItem({"aac": "AAC", "libopus": "Opus", "copy": tr("Copy original")}[a], a)
        self.v_abitrate = QSpinBox()
        self.v_abitrate.setRange(32, 512)
        self.v_abitrate.setSuffix(" kbps")
        self.v_downmix = QCheckBox(tr("Downmix 5.1 to stereo"))
        self.v_height = QComboBox()
        self.v_height.addItem(tr("Original"), None)
        for h in HEIGHT_STEPS:
            self.v_height.addItem(f"{h}p", h)
        self.v_height.setToolTip(tr("Downscale only — a larger value than the source is ignored"))
        self.v_size = QDoubleSpinBox()
        self.v_size.setRange(0, 4096)
        self.v_size.setDecimals(1)
        self.v_size.setSuffix(" MB")
        self.v_size.setSpecialValueText(tr("off"))
        self.v_size.setToolTip(tr("Fit the clip into this file size (2-pass). 0 = off, use CRF quality."))
        self.v_autods = QCheckBox(tr("Allow lowering the resolution to fit the size"))
        self.v_autods.setToolTip(tr("If the bitrate is too low for the resolution, pick a smaller one automatically"))
        self.v_minh = QComboBox()
        for h in HEIGHT_STEPS:
            self.v_minh.addItem(f"{h}p", h)
        self.v_minh.setToolTip(tr("Never go below this height when lowering automatically"))
        self.v_extra = QLineEdit()
        self.v_extra.setPlaceholderText("-x265-params aq-mode=3")
        self.v_extra.setToolTip(tr("Advanced: extra ffmpeg output options"))
        f.addRow(tr("Mode"), self.v_mode)
        f.addRow(tr("Video codec"), self.v_codec)
        f.addRow("", self.v_codec_help)
        f.addRow(tr("Bit depth"), self.v_depth)
        f.addRow(tr("Quality (CRF/CQ)"), self.v_quality)
        f.addRow(tr("Speed preset"), self.v_speed)
        f.addRow(tr("Tune"), self.v_tune)
        f.addRow(tr("Container"), self.v_container)
        f.addRow(tr("Audio codec"), self.v_acodec)
        f.addRow(tr("Audio bitrate"), self.v_abitrate)
        f.addRow("", self.v_downmix)
        f.addRow(tr("Resolution"), self.v_height)
        f.addRow(tr("Fit into size"), self.v_size)
        f.addRow("", self.v_autods)
        f.addRow(tr("Minimum height"), self.v_minh)
        f.addRow(tr("Extra options"), self.v_extra)
        self.v_codec.currentIndexChanged.connect(self._codec_changed)
        self.v_mode.currentIndexChanged.connect(self._mode_changed)
        self.v_size.valueChanged.connect(self._mode_changed)
        return w

    def _build_anim_page(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        self.a_height = QComboBox()
        self.a_height.addItem(tr("Original"), None)
        for h in ANIM_HEIGHT_STEPS:
            self.a_height.addItem(f"{h}p", h)
        self.a_fps = QSpinBox()
        self.a_fps.setRange(0, 30)
        self.a_fps.setSpecialValueText(tr("source (max 30)"))
        self.a_colors = QSpinBox()
        self.a_colors.setRange(16, 256)
        self.a_colors.setToolTip(tr("GIF palette size: fewer colours = smaller file"))
        self.a_dither = QComboBox()
        for d in GIF_DITHERS:
            self.a_dither.addItem(d, d)
        self.a_dither.setToolTip(tr("Dithering hides banding; Bayer compresses better than sierra"))
        self.a_quality = QSpinBox()
        self.a_quality.setRange(0, 100)
        self.a_quality.setToolTip(tr("WebP quality"))
        self.a_lossless = QCheckBox(tr("Lossless WebP"))
        self.a_level = QSpinBox()
        self.a_level.setRange(0, 6)
        self.a_level.setToolTip(tr("WebP compression effort (higher = slower, smaller)"))
        self.a_loop = QCheckBox(tr("Loop forever"))
        self.a_fit = QDoubleSpinBox()
        self.a_fit.setRange(0, 512)
        self.a_fit.setDecimals(1)
        self.a_fit.setSuffix(" MB")
        self.a_fit.setSpecialValueText(tr("off"))
        self.a_fit.setToolTip(tr("Automatically lower parameters until the file fits (0 = off)"))
        f.addRow(tr("Resolution"), self.a_height)
        f.addRow(tr("Frame rate"), self.a_fps)
        f.addRow(tr("Colours (GIF)"), self.a_colors)
        f.addRow(tr("Dithering (GIF)"), self.a_dither)
        f.addRow(tr("Quality (WebP)"), self.a_quality)
        f.addRow("", self.a_lossless)
        f.addRow(tr("Compression (WebP)"), self.a_level)
        f.addRow("", self.a_loop)
        f.addRow(tr("Fit into size"), self.a_fit)
        f.addRow(QLabel(tr("When fitting: lock a parameter to keep it, or set the lowest allowed value.")))
        self.locks: dict[str, QCheckBox] = {}
        self.mins: dict[str, QWidget] = {}
        for key, label in (("height", "Height"), ("fps", "Frame rate"), ("colors", "Colours"),
                           ("dither", "Dithering"), ("quality", "WebP quality")):
            row = QHBoxLayout()
            lock = QCheckBox(tr("lock"))
            lock.setToolTip(tr("Never change this parameter"))
            self.locks[key] = lock
            row.addWidget(lock)
            if key != "dither":
                sp = QSpinBox()
                sp.setRange(0, 2160)
                sp.setSpecialValueText(tr("no minimum"))
                sp.setPrefix(tr("min") + " ")
                self.mins[key] = sp
                row.addWidget(sp, 1)
            else:
                row.addStretch(1)
            f.addRow(tr(label), row)
        return w

    # ---- list ----
    def _fill(self, select_id: str | None = None) -> None:
        self.list.clear()
        for p in self.store.all():
            label = ("🔒 " + tr(p.name)) if p.builtin else p.name
            it = QListWidgetItem(label)
            it.setData(Qt.ItemDataRole.UserRole, p.id)
            self.list.addItem(it)
            if p.id == select_id:
                self.list.setCurrentItem(it)
        if self.list.currentItem() is None and self.list.count():
            self.list.setCurrentRow(0)

    def _select(self) -> None:
        it = self.list.currentItem()
        if it is None:
            return
        p = self.store.get(it.data(Qt.ItemDataRole.UserRole))
        if p is None:
            return
        self.current = p
        self._load(p)

    def _load(self, p: Preset) -> None:
        self._loading = True
        ro = p.builtin
        self.ro_label.setVisible(ro)
        for w in (self.name, self.stack, self.b_save):
            w.setEnabled(not ro)
        self.b_del.setEnabled(not ro)
        self.name.setText(tr(p.name) if ro else p.name)
        if p.kind == "video":
            self.stack.setCurrentWidget(self.video_page)
            self.v_mode.setCurrentIndex(max(0, self.v_mode.findData(p.mode)))
            self.v_codec.setCurrentIndex(max(0, self.v_codec.findData(p.vcodec if p.vcodec in CODECS else "libx264")))
            self._codec_changed()
            self.v_depth.setCurrentIndex(max(0, self.v_depth.findData(p.bit_depth)))
            self.v_quality.setValue(p.quality)
            self.v_speed.setCurrentIndex(max(0, self.v_speed.findData(p.speed or CODECS.get(p.vcodec, CODECS["libx264"]).default_speed)))
            self.v_tune.setCurrentIndex(max(0, self.v_tune.findData(p.tune or "")))
            self.v_container.setCurrentIndex(max(0, self.v_container.findData(p.container)))
            self.v_acodec.setCurrentIndex(max(0, self.v_acodec.findData(p.acodec)))
            self.v_abitrate.setValue(p.abitrate)
            self.v_downmix.setChecked(p.downmix_stereo)
            self.v_height.setCurrentIndex(max(0, self.v_height.findData(p.target_height)))
            self.v_size.setValue(p.target_size_mb or 0)
            self.v_autods.setChecked(p.allow_auto_downscale)
            self.v_minh.setCurrentIndex(max(0, self.v_minh.findData(p.min_auto_height)))
            self.v_extra.setText(" ".join(p.extra_args))
            self._mode_changed()
        else:
            self.stack.setCurrentWidget(self.anim_page)
            gif = p.kind == "gif"
            self.a_height.setCurrentIndex(max(0, self.a_height.findData(p.target_height)))
            self.a_fps.setValue(p.fps or 0)
            self.a_colors.setValue(p.colors)
            self.a_dither.setCurrentIndex(max(0, self.a_dither.findData(p.dither)))
            self.a_quality.setValue(p.webp_quality)
            self.a_lossless.setChecked(p.lossless)
            self.a_level.setValue(p.compression_level)
            self.a_loop.setChecked(p.loop)
            self.a_fit.setValue(p.fit_size_mb or 0)
            for w in (self.a_colors, self.a_dither):
                w.setEnabled(gif and not ro)
            for w in (self.a_quality, self.a_lossless, self.a_level):
                w.setEnabled(not gif and not ro)
            for k, cb in self.locks.items():
                cb.setChecked(bool(p.locks.get(k)))
                cb.setEnabled(not ro and (k not in ("colors", "dither") or gif) and (k != "quality" or not gif))
            for k, sp in self.mins.items():
                sp.setValue(int(p.mins.get(k, 0) or 0))
                sp.setEnabled(not ro and (k != "colors" or gif) and (k != "quality" or not gif))
        self._loading = False

    def _codec_changed(self) -> None:
        codec = self.v_codec.currentData()
        info = CODECS.get(codec)
        if info is None:
            return
        self.v_codec_help.setText(tr(CODEC_HELP.get(codec, "")))
        lo, hi = info.quality_range
        self.v_quality.setRange(lo, hi)
        if not self._loading:
            self.v_quality.setValue(info.default_quality)
        self.v_speed.clear()
        for s in info.speeds:
            self.v_speed.addItem(s, s)
        self.v_speed.setCurrentIndex(max(0, self.v_speed.findData(info.default_speed)))
        self.v_tune.clear()
        self.v_tune.addItem(tr("none"), "")
        for t in info.tunes:
            self.v_tune.addItem(t, t)
        self.v_depth.model().item(0).setEnabled(info.supports_8bit)
        self.v_depth.model().item(1).setEnabled(info.supports_10bit)
        if not info.supports_10bit:
            self.v_depth.setCurrentIndex(0)
        elif not info.supports_8bit:
            self.v_depth.setCurrentIndex(1)

    def _mode_changed(self) -> None:
        smart = self.v_mode.currentData() == "smartcut"
        for w in (self.v_codec, self.v_depth, self.v_quality, self.v_speed, self.v_tune, self.v_size,
                  self.v_height):
            w.setEnabled(not smart and not (self.current and self.current.builtin))
        sized = self.v_size.value() > 0 and not smart
        self.v_autods.setEnabled(sized)
        self.v_minh.setEnabled(sized)

    # ---- actions ----
    def _collect(self) -> Preset | None:
        p = self.current
        if p is None or p.builtin:
            return None
        p.name = self.name.text().strip() or p.name
        if p.kind == "video":
            p.mode = self.v_mode.currentData()
            p.vcodec = "copy" if p.mode == "smartcut" else self.v_codec.currentData()
            p.bit_depth = self.v_depth.currentData()
            p.quality = self.v_quality.value()
            p.speed = self.v_speed.currentData() or ""
            p.tune = self.v_tune.currentData() or ""
            p.container = "auto" if p.mode == "smartcut" else self.v_container.currentData()
            p.acodec = self.v_acodec.currentData()
            p.abitrate = self.v_abitrate.value()
            p.downmix_stereo = self.v_downmix.isChecked()
            p.target_height = self.v_height.currentData()
            p.target_size_mb = self.v_size.value() or None
            p.allow_auto_downscale = self.v_autods.isChecked()
            p.min_auto_height = self.v_minh.currentData()
            p.extra_args = self.v_extra.text().split()
            if p.mode == "encode" and p.container == "webm" and CODECS[p.vcodec].family not in ("vp9", "av1"):
                QMessageBox.warning(self, tr("Export presets"), tr("WebM supports only VP9 or AV1 video."))
                return None
        else:
            p.target_height = self.a_height.currentData()
            p.fps = self.a_fps.value() or None
            p.colors = self.a_colors.value()
            p.dither = self.a_dither.currentData()
            p.webp_quality = self.a_quality.value()
            p.lossless = self.a_lossless.isChecked()
            p.compression_level = self.a_level.value()
            p.loop = self.a_loop.isChecked()
            p.fit_size_mb = self.a_fit.value() or None
            p.locks = {k: cb.isChecked() for k, cb in self.locks.items() if cb.isChecked()}
            p.mins = {k: sp.value() for k, sp in self.mins.items() if sp.value()}
        return p

    def _save(self) -> None:
        p = self._collect()
        if p is None:
            return
        self.store.upsert(p)
        self._fill(p.id)

    def _duplicate(self) -> None:
        if self.current is None:
            return
        base = self.current
        name = (tr(base.name) if base.builtin else base.name) + " " + tr("(copy)")
        p = base.copy_as_user(name)
        self.store.upsert(p)
        self._fill(p.id)

    def _new(self, kind: str) -> None:
        names = {"video": "My video preset", "gif": "My GIF preset", "webp": "My WebP preset"}
        p = Preset(id=new_id(), name=tr(names[kind]), kind=kind)
        if kind == "video":
            p.vcodec, p.bit_depth, p.quality, p.speed = "libx265", 10, 18, "slow"
        else:
            p.target_height, p.fps = 480, 15
        self.store.upsert(p)
        self._fill(p.id)

    def _delete(self) -> None:
        if self.current is None or self.current.builtin:
            return
        if QMessageBox.question(self, tr("Delete preset"), tr("Delete preset “{name}”?").format(
                name=self.current.name)) == QMessageBox.StandardButton.Yes:
            self.store.delete(self.current.id)
            self.current = None
            self._fill()
