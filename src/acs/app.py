"""Application bootstrap: settings, language, crash protection, main window."""

from __future__ import annotations

import sys
from pathlib import Path

import PySide6
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from acs import APP_NAME, APP_TITLE, __version__
from acs.core import recovery, trace, uiscale
from acs.core.presets import PresetStore
from acs.core.project_io import PROJECT_EXT
from acs.core.settings import AppDirs, AppSettings
from acs.i18n import install_qt_translations, set_language, tr
from acs.ui import theme
from acs.ui.trace_filter import EventTracer


def _dark_palette(app: QApplication, name: str = "dark") -> None:
    """Kept for helper scripts; the app uses the theme chosen in Settings."""
    theme.apply(app, name)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    debug = "--debug" in argv
    argv = [a for a in argv if a != "--debug"]
    settings = AppSettings.load()
    set_language(settings.language)
    ui_factor = uiscale.apply_before_qapplication(settings.ui_scale)  # must precede QApplication
    logs = AppDirs.logs_dir()
    trace_path = trace.start(logs, verbose=debug)
    trace.log("session", f"{APP_TITLE} {__version__} python={sys.version.split()[0]} "
                         f"pyside={PySide6.__version__} exe={sys.executable} debug={debug} "
                         f"dev_mode={sys.flags.dev_mode} args={argv[1:]}")
    trace.log("session", f"settings: ffmpeg={settings.ffmpeg} libmpv={settings.libmpv} hwdec={settings.hwdec} "
                         f"library={settings.library_folders} lang={settings.language} "
                         f"ui_scale={settings.ui_scale}->{ui_factor:g} display={uiscale.display_metrics()}")
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_TITLE)
    t = theme.apply(app, settings.theme)
    trace.log("session", f"theme={settings.theme}->{t.name}")
    install_qt_translations(app, settings.language)  # Qt's own strings: OK / Cancel / Save / file dialogs …
    tracer = EventTracer(app)
    app.installEventFilter(tracer)

    # One instance at a time: two windows overwrite each other's settings and autosaves on exit
    # (e.g. a changed interface size silently reverted by the other window).
    live = recovery.read_live_lock()
    if live:
        trace.log("session", f"another instance is running: {live}")
        from PySide6.QtWidgets import QMessageBox
        r = QMessageBox.question(
            None, APP_TITLE,
            tr("Anime Clip Studio is already running. Two windows would overwrite each other's settings "
               "and autosaves.\n\nStart another window anyway?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            trace.stop()
            return 0

    # Read the previous session's lock *before* writing ours: a stale lock means a crash.
    stale = recovery.read_stale_lock()
    if stale:
        trace.log("session", f"previous session did not exit cleanly: {stale}")
    recovery.write_lock()
    holder: dict = {}
    recovery.install_crash_handlers(lambda: holder["win"].emergency_save() if "win" in holder else None)

    from acs.ui.main_window import MainWindow  # imported after the language is set

    mpv_log = str(logs / f"mpv-{trace_path.stem.removeprefix('trace-')}.log") if debug else None
    win = MainWindow(settings, PresetStore(), mpv_log_file=mpv_log)
    holder["win"] = win
    win.show()

    def after_show():
        win.startup_checks(stale)
        if len(argv) > 1 and Path(argv[1]).exists():
            target = argv[1]
            if target.lower().endswith(PROJECT_EXT):
                win.open_project(target)
            else:
                win.open_video(target)

    QTimer.singleShot(250, after_show)
    rc = app.exec()
    trace.log("session", f"event loop finished rc={rc} restart={win.restart_requested}")
    recovery.release_lock()  # before relaunching, so the new instance does not see us as running
    recovery.shutdown_crash_handlers()
    trace.stop()
    if win.restart_requested:
        relaunch(debug)
    return rc


def relaunch(debug: bool) -> None:
    """Start a fresh instance (same interpreter, same flags) after this one has released its lock."""
    import subprocess
    args = [sys.executable, "-m", "acs"] + (["--debug"] if debug else [])
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, cwd=str(Path.cwd()), creationflags=flags, close_fds=True)
