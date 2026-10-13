"""Choose the keys of the mpv marker script, check them against mpv's own bindings, install."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QGridLayout, QKeySequenceEdit, QLabel,
                               QMessageBox, QPushButton, QVBoxLayout)

from acs.core import trace
from acs.core.mpv_keys import ACTIONS, Conflict, default_keys, find_conflicts, mpv_to_qt, qt_to_mpv
from acs.core.settings import AppDirs
from acs.core.tools import MpvProbe, install_marker_script, probe_mpv
from acs.i18n import tr
from acs.ui import theme

KIND_TITLE = {"input_conf": "your input.conf", "builtin": "mpv built-in", "script": "script",
              "duplicate": "this script"}


class MpvScriptDialog(QDialog):
    def __init__(self, settings, player: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Install marker script into mpv"))
        self.resize(720, 460)
        self.settings = settings
        self.player = player
        self._probe: MpvProbe | None = None
        self._probed_keys: dict[str, str] | None = None
        keys = {**default_keys(), **(settings.mpv_script_keys or {})}

        lay = QVBoxLayout(self)
        intro = QLabel(tr("Choose the keys you will press in mpv while watching. Click a field and press the "
                          "key combination; Backspace inside an empty field clears it.") + "\n"
                       + tr("mpv:") + f" {player}")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        grid = QGridLayout()
        self.edits: dict[str, QKeySequenceEdit] = {}
        self.names: dict[str, QLabel] = {}
        for row, (action, (_opt, default, title)) in enumerate(ACTIONS.items()):
            grid.addWidget(QLabel(tr(title)), row, 0)
            ed = QKeySequenceEdit(QKeySequence(mpv_to_qt(keys.get(action, default))))
            ed.setMaximumSequenceLength(1)
            ed.setClearButtonEnabled(True)
            ed.keySequenceChanged.connect(lambda _s, a=action: self._changed(a))
            name = QLabel()
            name.setStyleSheet(theme.css_info())
            name.setToolTip(tr("The key name as mpv writes it"))
            reset = QPushButton(tr("Default"))
            reset.clicked.connect(lambda _c=False, a=action, d=default: self.edits[a].setKeySequence(
                QKeySequence(mpv_to_qt(d))))
            grid.addWidget(ed, row, 1)
            grid.addWidget(name, row, 2)
            grid.addWidget(reset, row, 3)
            self.edits[action] = ed
            self.names[action] = name
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        self.report = QLabel()
        self.report.setWordWrap(True)
        self.report.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(self.report, 1)
        bb = QDialogButtonBox()
        self.b_check = bb.addButton(tr("Check keys"), QDialogButtonBox.ButtonRole.ActionRole)
        self.b_install = bb.addButton(tr("Install"), QDialogButtonBox.ButtonRole.AcceptRole)
        bb.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.b_check.clicked.connect(self.check)
        self.b_install.clicked.connect(self.install)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        for a in ACTIONS:
            self._changed(a)

    # ---- keys ----
    def keys(self) -> dict[str, str]:
        return {a: qt_to_mpv(ed.keySequence().toString(QKeySequence.SequenceFormat.PortableText))
                for a, ed in self.edits.items()}

    def _changed(self, action: str) -> None:
        k = self.keys()[action]
        self.names[action].setText(k or tr("(no key — only via input.conf)"))
        self.report.setText("")

    # ---- checking ----
    def _run_probe(self) -> MpvProbe:
        if self._probe is None:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self._probe = probe_mpv(self.player, AppDirs.cache / "mpvprobe")
            finally:
                QApplication.restoreOverrideCursor()
            trace.log("mpv-script", f"probe: scripts_dir={self._probe.scripts_dir} "
                                    f"bindings={len(self._probe.bindings)}")
        return self._probe

    def _conflicts(self) -> list[Conflict]:
        return find_conflicts(self.keys(), self._run_probe().bindings)

    def _describe(self, c: Conflict) -> str:
        action = tr(ACTIONS[c.action][2])
        if c.kind == "duplicate":
            what = tr("also chosen for") + " “" + tr(ACTIONS[c.owner][2]) + "”"
        else:
            src = tr(KIND_TITLE[c.kind]) + (f" “{c.owner}”" if c.kind == "script" else "")
            what = f"{src}: {html.escape(c.command)}"
        return f"<b>{html.escape(c.key)}</b> ({action}) — {what}<br><i>{tr(c.consequence())}</i>"

    def check(self) -> list[Conflict]:
        conflicts = self._conflicts()
        if self._probe and self._probe.scripts_dir is None:
            self.report.setText(f"<span style='color:#ff6b6b'>{tr('Could not ask this mpv for its scripts folder.')}"
                                "</span>")
            return conflicts
        if not conflicts:
            self.report.setText(f"<span style='color:#5cd17b'>✔ {tr('All keys are free in this mpv.')}</span>"
                                f"<br><span style='{theme.css_dim()}'>{tr('Scripts folder:')} "
                                f"{html.escape(str(self._probe.scripts_dir))}</span>")
        else:
            self.report.setText("<span style='color:#e0b341'>⚠ " + tr("Some keys are already used in mpv:")
                                + "</span><br>" + "<br>".join(self._describe(c) for c in conflicts))
        return conflicts

    # ---- install ----
    def install(self) -> None:
        keys = self.keys()
        if not any(keys.values()):
            QMessageBox.warning(self, self.windowTitle(), tr("Choose at least one key."))
            return
        conflicts = self.check()
        probe = self._run_probe()
        if probe.scripts_dir is None:
            QMessageBox.warning(self, self.windowTitle(), tr("Could not ask this mpv for its scripts folder."))
            return
        if conflicts:
            box = QMessageBox(QMessageBox.Icon.Warning, self.windowTitle(),
                              tr("Some keys are already used in mpv:"), parent=self)
            box.setTextFormat(Qt.TextFormat.RichText)
            box.setInformativeText("<br><br>".join(self._describe(c) for c in conflicts))
            b_anyway = box.addButton(tr("Install anyway"), QMessageBox.ButtonRole.AcceptRole)
            box.addButton(tr("Change keys"), QMessageBox.ButtonRole.RejectRole)
            box.exec()
            trace.log("mpv-script", f"conflicts: {[(c.key, c.kind, c.owner) for c in conflicts]} "
                                    f"-> {'install anyway' if box.clickedButton() is b_anyway else 'change keys'}")
            if box.clickedButton() is not b_anyway:
                return
        try:
            target = install_marker_script(self.player, AppDirs.cache / "mpvprobe", keys, probe.scripts_dir)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, self.windowTitle(), tr("Could not install the script:") + f"\n{e}")
            return
        self.settings.mpv_script_keys = keys
        self.settings.save()
        trace.log("mpv-script", f"installed to {target} keys={keys}")
        lines = "\n".join(f"  {k or '—'} — {tr(ACTIONS[a][2])}" for a, k in keys.items())
        QMessageBox.information(self, self.windowTitle(),
                                tr("The marker script was installed:") + f"\n{target}\n\n"
                                + tr("Restart mpv, then while watching:") + "\n" + lines + "\n\n"
                                + tr("When you open the same video in Anime Clip Studio it offers to import them."))
        self.accept()
