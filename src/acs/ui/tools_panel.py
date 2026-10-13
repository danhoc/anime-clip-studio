"""External tools panel (ffmpeg / ffprobe / libmpv): paths, validation, auto-detect, official links."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFileDialog, QGridLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget)

from acs.core import trace
from acs.core.settings import app_root
from acs.core.tools import OFFICIAL_LINKS, WINGET_FFMPEG, ToolStatus, check_ff, check_libmpv, detect
from acs.ui import theme
from acs.i18n import tr

KINDS = (("ffmpeg", "ffmpeg.exe", "ffmpeg (*.exe)"),
         ("ffprobe", "ffprobe.exe", "ffprobe (*.exe)"),
         ("libmpv", "libmpv-2.dll", "libmpv (libmpv-2.dll mpv-2.dll)"))


class ToolsPanel(QWidget):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.status: dict[str, ToolStatus] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        grid = QGridLayout()
        self.edits: dict[str, QLineEdit] = {}
        self.labels: dict[str, QLabel] = {}
        for row, (kind, title, filt) in enumerate(KINDS):
            grid.addWidget(QLabel(title), row * 2, 0)
            le = QLineEdit(getattr(settings, kind))
            le.editingFinished.connect(lambda k=kind: self.validate(k))
            b = QPushButton("…")
            b.setFixedWidth(28)
            b.setToolTip(tr("Choose the file manually"))
            b.clicked.connect(lambda _c=False, k=kind, f=filt: self._browse(k, f))
            grid.addWidget(le, row * 2, 1)
            grid.addWidget(b, row * 2, 2)
            lab = QLabel()
            lab.setWordWrap(True)
            lab.setTextFormat(Qt.TextFormat.RichText)
            grid.addWidget(lab, row * 2 + 1, 1, 1, 2)
            self.edits[kind] = le
            self.labels[kind] = lab
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        row = QHBoxLayout()
        self.b_detect = QPushButton(tr("Auto-detect"))
        self.b_detect.setToolTip(tr("Look for the tools in the usual install locations (PATH, winget, scoop, "
                                    "chocolatey, Program Files, C:\\FFmpeg, C:\\mpv) and check them"))
        self.b_detect.clicked.connect(self.auto_detect)
        row.addWidget(self.b_detect)
        row.addStretch(1)
        lay.addLayout(row)

        self.help = QGroupBox(tr("Where to get them (official sources)"))
        hl = QVBoxLayout(self.help)
        self.links = QLabel()
        self.links.setTextFormat(Qt.TextFormat.RichText)
        self.links.setOpenExternalLinks(True)
        self.links.setWordWrap(True)
        self.links.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        hl.addWidget(self.links)
        wrow = QHBoxLayout()
        wl = QLabel(tr("Or install FFmpeg yourself with Windows Package Manager:") + f" <code>{WINGET_FFMPEG}</code>")
        wl.setTextFormat(Qt.TextFormat.RichText)
        wl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        b_copy = QPushButton(tr("Copy command"))
        b_copy.clicked.connect(lambda: QApplication.clipboard().setText(WINGET_FFMPEG))
        wrow.addWidget(wl, 1)
        wrow.addWidget(b_copy)
        hl.addLayout(wrow)
        note = QLabel(tr("Anime Clip Studio never downloads or installs anything by itself and does not change "
                         "PATH or the registry. After installing, press “Auto-detect”."))
        note.setWordWrap(True)
        note.setStyleSheet(theme.css_info())
        hl.addWidget(note)
        lay.addWidget(self.help)
        for kind, _t, _f in KINDS:
            self.validate(kind, quick=True)

    # ---- actions ----
    def _browse(self, kind: str, filt: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, kind, self.edits[kind].text(), filt)
        if path:
            self.edits[kind].setText(path)
            self.validate(kind)

    def validate(self, kind: str, quick: bool = False) -> ToolStatus:
        path = self.edits[kind].text().strip()
        if kind == "libmpv":
            st = check_libmpv(path)
        else:
            st = check_ff(path, kind, with_features=not quick)
        self.status[kind] = st
        self._show(kind, st)
        return st

    def auto_detect(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for kind, _t, _f in KINDS:
                st = detect(kind, self.edits[kind].text().strip(), app_root())
                self.status[kind] = st
                if st.ok:
                    self.edits[kind].setText(st.path)
                self._show(kind, st)
                trace.log("tools", f"detect {kind}: ok={st.ok} path={st.path!r} source={st.source} "
                                   f"version={st.version} error={st.error}")
        finally:
            QApplication.restoreOverrideCursor()

    def _show(self, kind: str, st: ToolStatus) -> None:
        if st.ok:
            src = f" · {tr('found in')} {st.source}" if st.source and st.source != "settings" else ""
            text = f"<span style='color:#5cd17b'>✔ {tr('OK')} {st.version}{src}</span>"
            missing = st.missing_features
            if missing:
                text += ("<br><span style='color:#e0b341'>⚠ " + tr("this build lacks:") + " "
                         + ", ".join(missing) + " — " + tr("the matching presets will fail") + "</span>")
        else:
            text = f"<span style='color:#ff6b6b'>✖ {tr(st.error or 'not found')}</span>"
        self.labels[kind].setText(text)
        self._update_links()

    def _update_links(self) -> None:
        need = []
        if any(not self.status.get(k, ToolStatus(k)).ok for k in ("ffmpeg", "ffprobe")):
            need.append("ffmpeg")
        if not self.status.get("libmpv", ToolStatus("libmpv")).ok:
            need.append("libmpv")
        parts = []
        for key in need:
            head = "FFmpeg (ffmpeg.exe + ffprobe.exe)" if key == "ffmpeg" else "libmpv (libmpv-2.dll, 64-bit)"
            items = "".join(f"<li><a href='{url}'>{tr(text)}</a></li>" for text, url in OFFICIAL_LINKS[key])
            parts.append(f"<b>{head}</b><ul style='margin-top:2px'>{items}</ul>")
        self.links.setText("".join(parts) if parts else tr("All tools are in place."))

    def all_ok(self) -> bool:
        return all(self.status.get(k, ToolStatus(k)).ok for k, _t, _f in KINDS)

    def apply(self) -> bool:
        """Write paths to settings. Returns True if the libmpv path changed (restart needed)."""
        old_mpv = self.settings.libmpv
        self.settings.ffmpeg = self.edits["ffmpeg"].text().strip()
        self.settings.ffprobe = self.edits["ffprobe"].text().strip()
        self.settings.libmpv = self.edits["libmpv"].text().strip()
        return self.settings.libmpv != old_mpv


class ToolsDialog(QDialog):
    """Shown at startup when a required tool is missing, and from Tools → External tools…"""

    def __init__(self, settings, parent=None, auto: bool = False):
        super().__init__(parent)
        self.setWindowTitle(tr("External tools"))
        self.resize(760, 560)
        self.settings = settings
        self.libmpv_changed = False
        lay = QVBoxLayout(self)
        intro = QLabel(tr("Anime Clip Studio uses FFmpeg (export, frames, analysis) and libmpv (video player). "
                          "Some of them were not found or do not work."
                          if auto else "Paths of the external programs used by Anime Clip Studio."))
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.panel = ToolsPanel(settings)
        lay.addWidget(self.panel, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        if auto:
            self.panel.auto_detect()

    def _save(self) -> None:
        self.libmpv_changed = self.panel.apply()
        self.settings.save()
        self.accept()
