"""Help → Check for updates: ask GitHub (or the Git remote) on request, install only after consent."""

from __future__ import annotations

import threading

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QProgressDialog

from acs import APP_TITLE, __version__
from acs.core import trace, updates
from acs.core.settings import app_root
from acs.i18n import tr


def error_text(e: updates.UpdateError) -> str:
    texts = {
        "not_configured": tr("The update source is not set in this copy of the app "
                             "(GITHUB_REPO in src/acs/__init__.py)."),
        "no_release": tr("No published release was found on GitHub yet."),
        "network": tr("Could not connect to GitHub. Check the internet connection and try again."),
        "no_git": tr("Git was not found, although this copy was installed with Git."),
        "no_upstream": tr("This Git copy has no remote branch to update from."),
        "dirty": tr("The program files have local changes. Commit or discard them first (git status), "
                    "then try again."),
        "diverged": tr("The local branch has its own commits, so it cannot be updated automatically "
                       "(use git pull)."),
        "git_failed": tr("Git reported an error."),
        "bad_archive": tr("The downloaded archive is not a valid Anime Clip Studio update. Nothing was changed."),
    }
    text = texts.get(e.code, tr("The update could not be installed."))
    return f"{text}\n\n{e.detail}" if e.detail else text


class Updater:
    """Runs the network and file work on worker threads; results come back through the dispatcher."""

    def __init__(self, win):
        self.win = win
        self._busy = False

    def check(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.win.statusBar().showMessage(tr("Checking for updates…"))
        post, done, root = self.win.dispatch.post, self._checked, app_root()

        def work():
            try:
                post(done, updates.check(root), None)
            except updates.UpdateError as e:
                post(done, None, e)
            except Exception as e:  # noqa: BLE001 - shown to the user instead of a silent failure
                post(done, None, updates.UpdateError("failed", repr(e)))

        self.win._thread("update-check", work)

    def _checked(self, info: updates.UpdateInfo | None, err: updates.UpdateError | None) -> None:
        self._busy = False
        self.win.statusBar().clearMessage()
        trace.log("update", f"check: info={info} err={err}")
        title = tr("Check for updates")
        if err is not None:
            QMessageBox.warning(self.win, title, tr("Could not check for updates.") + "\n\n" + error_text(err))
        elif not info.available:
            QMessageBox.information(self.win, title, tr("You have the latest version ({version}).")
                                    .format(version=__version__))
        else:
            self._offer(info)

    def _offer(self, info: updates.UpdateInfo) -> None:
        box = QMessageBox(self.win)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(tr("Check for updates"))
        if info.kind == "git":
            box.setText(tr("{n} new changes are available for your copy (version {current}).")
                        .format(n=len(info.commits), current=info.current))
        else:
            box.setText(tr("Version {new} is available (you have {current}).")
                        .format(new=info.latest, current=info.current))
        question = tr("Download and install it now? Your projects, presets and settings are kept. "
                      "The app restarts afterwards; new libraries, if any, are installed at that start.")
        box.setInformativeText(tr("What is new:") + "\n" + info.notes + "\n\n" + question if info.notes else question)
        b_install = box.addButton(tr("Download and install"), QMessageBox.ButtonRole.AcceptRole)
        b_page = box.addButton(tr("Open the page"), QMessageBox.ButtonRole.ActionRole) if info.url else None
        box.addButton(tr("Later"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(b_install)
        box.exec()
        clicked = box.clickedButton()
        if b_page is not None and clicked is b_page:
            QDesktopServices.openUrl(QUrl(info.url))
        elif clicked is b_install:
            self._install(info)

    def _install(self, info: updates.UpdateInfo) -> None:
        trace.log("update", f"install {info.kind} {info.latest}")
        dlg = QProgressDialog(tr("Installing the update…"), tr("Cancel"), 0, 0, self.win)
        dlg.setWindowTitle(APP_TITLE)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        cancel = threading.Event()
        dlg.canceled.connect(cancel.set)
        if info.kind == "git":
            dlg.setCancelButton(None)   # git pull is not stopped half-way; only a download can be cancelled
        dlg.show()
        post, root = self.win.dispatch.post, app_root()
        progress, done = self._progress, self._installed

        def work():
            try:
                updates.apply(root, info, cancel=cancel.is_set,
                              on_progress=lambda n, total: post(progress, dlg, n, total))
                post(done, dlg, None)
            except updates.UpdateError as e:
                post(done, dlg, e)
            except Exception as e:  # noqa: BLE001
                post(done, dlg, updates.UpdateError("failed", repr(e)))

        self.win._thread("update-install", work)

    @staticmethod
    def _progress(dlg: QProgressDialog, n: int, total: int) -> None:
        if total:
            dlg.setMaximum(100)
            dlg.setValue(min(100, int(n * 100 / total)))
        dlg.setLabelText(tr("Downloading the update… {mb} MB").format(mb=f"{n / 2**20:.1f}"))

    def _installed(self, dlg: QProgressDialog, err: updates.UpdateError | None) -> None:
        dlg.close()
        trace.log("update", f"install finished err={err}")
        title = tr("Check for updates")
        if err is not None:
            if err.code == "cancelled":
                self.win.statusBar().showMessage(tr("The update was cancelled. Nothing was changed."), 5000)
            else:
                QMessageBox.warning(self.win, title, error_text(err))
            return
        r = QMessageBox.question(self.win, title, tr("The update is installed. Restart the app now?"),
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                 QMessageBox.StandardButton.Yes)
        if r == QMessageBox.StandardButton.Yes:
            self.win.restart_via_launcher = True   # run.bat: `uv run` installs changed libraries first
            self.win.restart_app()
        else:
            self.win.statusBar().showMessage(tr("The update is used from the next start of the app."), 8000)
