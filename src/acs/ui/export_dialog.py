"""Export dialog: choose clips, output folder, file naming and conflict policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout)

from acs.core.clipinfo import ClipSummary, summarize
from acs.core.document import ProjectDocument
from acs.core.model import Clip
from acs.core.presets import PresetStore
from acs.core.util import fmt_time, fmt_time_filename, safe_filename
from acs.i18n import tr
from acs.ui.texts import mode_text, reason_text, warning_text


@dataclass
class ExportItem:
    clip: Clip
    summary: ClipSummary
    output: Path


def render_name(template: str, video: str, clip: Clip, n: int, preset_id: str) -> str:
    try:
        name = template.format(video=video, clip=clip.name, n=f"{n:02d}", preset=preset_id,
                               **{"in": fmt_time_filename(clip.in_time),
                                  "out": fmt_time_filename(clip.out_time or clip.in_time)})
    except (KeyError, IndexError, ValueError):
        name = f"{video}_{clip.name}"
    return safe_filename(name)


def resolve_conflict(path: Path, policy: str, taken: set[str]) -> Path | None:
    if policy == "overwrite":
        return path
    if policy == "skip" and (path.exists() or str(path).lower() in taken):
        return None
    candidate = path
    i = 2
    while candidate.exists() or str(candidate).lower() in taken:
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        i += 1
    return candidate


class ExportDialog(QDialog):
    def __init__(self, doc: ProjectDocument, presets: PresetStore, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Export clips"))
        self.resize(900, 520)
        self.doc, self.presets, self.settings = doc, presets, settings
        p = doc.project
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(tr("Tick the clips to export. Each clip uses its own preset "
                                "(or the project default marked with ·).")))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Clip"), tr("Range"), tr("Preset"), tr("Mode"), tr("File name")])
        self.tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.tree.setRootIsDecorated(False)
        lay.addWidget(self.tree, 1)
        form = QFormLayout()
        out_row = QHBoxLayout()
        self.out = QLineEdit(p.output_dir or settings.output_dir)
        self.out.setToolTip(tr("Folder for the exported files"))
        b = QPushButton("…")
        b.setFixedWidth(28)
        b.clicked.connect(self._browse)
        out_row.addWidget(self.out, 1)
        out_row.addWidget(b)
        form.addRow(tr("Output folder"), out_row)
        self.template = QLineEdit(p.name_template or settings.name_template)
        self.template.setToolTip(tr("Variables: {video} {clip} {n} {in} {out} {preset}"))
        form.addRow(tr("File name"), self.template)
        self.policy = QComboBox()
        self.policy.addItem(tr("Add a number"), "number")
        self.policy.addItem(tr("Overwrite"), "overwrite")
        self.policy.addItem(tr("Skip"), "skip")
        self.policy.setToolTip(tr("What to do when a file with the same name already exists"))
        form.addRow(tr("If the file exists"), self.policy)
        lay.addLayout(form)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Export"))
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.template.textChanged.connect(self._refresh_names)
        self.out.textChanged.connect(self._refresh_names)
        self.tree.itemChanged.connect(lambda *_: self._update_summary())
        self._fill()

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, tr("Output folder"), self.out.text())
        if d:
            self.out.setText(d)

    def _fill(self) -> None:
        p = self.doc.project
        sel = set(self.doc.selected_clips)
        self._rows: list[tuple[QTreeWidgetItem, Clip, ClipSummary]] = []
        for c in p.clips:
            if not c.is_complete:
                continue
            s = summarize(p, self.presets, self.doc.info, c, self.settings)
            mode = mode_text(s.mode)
            tips = []
            if s.reasons:
                mode = "ⓘ " + mode
                tips.append(tr("Re-encoded because of: ") + ", ".join(reason_text(r) for r in s.reasons))
            tips += [warning_text(n) for n in s.notes]
            pname = tr(s.preset.name) if s.preset.builtin else s.preset.name
            it = QTreeWidgetItem([c.name, f"{fmt_time(c.in_time)} – {fmt_time(c.out_time)}",
                                  ("· " if c.preset_id is None else "") + pname, mode, ""])
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(0, Qt.CheckState.Checked if (not sel or c.id in sel) else Qt.CheckState.Unchecked)
            if tips:
                it.setToolTip(3, "\n".join(tips))
            self.tree.addTopLevelItem(it)
            self._rows.append((it, c, s))
        for i in range(4):
            self.tree.resizeColumnToContents(i)
        self._refresh_names()

    def _refresh_names(self) -> None:
        video = Path(self.doc.project.video.path).stem
        for n, (it, c, s) in enumerate(self._rows, 1):
            it.setText(4, render_name(self.template.text(), video, c, n, s.preset.id) + "." + self._ext(s))
        self._update_summary()

    def _ext(self, s: ClipSummary) -> str:
        p = s.preset
        if p.kind != "video":
            return p.ext
        if p.container in ("mp4", "mkv", "webm"):
            return p.container
        info = self.doc.info
        return "mp4" if info is not None and info.is_mp4 else "mkv"

    def _update_summary(self) -> None:
        n = sum(1 for it, _, _ in self._rows if it.checkState(0) == Qt.CheckState.Checked)
        text = tr("Clips to export:") + f" {n}"
        if self.settings.shorts_mode:
            text += "\n" + tr("Shorts Mode is on: only the picture inside each clip's 9:16 frame is exported.")
        self.summary.setText(text)

    def items(self) -> list[ExportItem]:
        out_dir = Path(self.out.text().strip() or self.settings.output_dir)
        policy = self.policy.currentData()
        taken: set[str] = set()
        video = Path(self.doc.project.video.path).stem
        result = []
        for n, (it, c, s) in enumerate(self._rows, 1):
            if it.checkState(0) != Qt.CheckState.Checked:
                continue
            name = render_name(self.template.text(), video, c, n, s.preset.id) + "." + self._ext(s)
            path = resolve_conflict(out_dir / name, policy, taken)
            if path is None:
                continue
            taken.add(str(path).lower())
            result.append(ExportItem(c, s, path))
        return result

    def output_dir(self) -> str:
        return self.out.text().strip()

    def name_template(self) -> str:
        return self.template.text()
