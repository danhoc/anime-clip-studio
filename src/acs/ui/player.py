"""Embedded libmpv player widget.

Threading rule: python-mpv's own event thread is NOT started. That thread converted libmpv event
structures with ctypes concurrently with the GUI thread and crashed the interpreter (access
violations in python313.dll / mpv.node_cast_value). Instead everything runs on the GUI thread:
a QTimer polls time-pos / pause / duration with plain synchronous property reads, commands are
synchronous, and the client's event queue is drained without decoding the events.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QWidget

from acs.core import trace

log = logging.getLogger("acs.player")

_mpv_module = None
POLL_MS = 15


def load_mpv(libmpv_path: str):
    """Import python-mpv with our bundled libmpv-2.dll on the DLL search path."""
    global _mpv_module
    if _mpv_module is not None:
        return _mpv_module
    if libmpv_path:
        d = str(Path(libmpv_path).parent)
        if os.path.isdir(d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
    import mpv  # noqa: PLC0415 - must happen after the DLL path is set
    _mpv_module = mpv
    return mpv


class MpvPlayer(QWidget):
    time_changed = Signal(float)
    pause_changed = Signal(bool)
    file_loaded = Signal()
    duration_changed = Signal(float)

    def __init__(self, libmpv_path: str, hwdec: bool = True, mpv_log_file: str | None = None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet("background: black;")
        self.setMinimumSize(320, 180)
        self._lib = load_mpv(libmpv_path)
        opts = dict(
            wid=str(int(self.winId())),
            config=False,
            vo="gpu-next",
            hwdec="auto-safe" if hwdec else "no",
            hr_seek="yes",
            hr_seek_framedrop="no",
            keep_open="yes",
            idle="yes",
            osc=False,
            osd_level=0,
            input_default_bindings=False,
            input_vo_keyboard=False,
            input_cursor=False,
            cursor_autohide="no",
            demuxer_max_bytes="512MiB",
            demuxer_max_back_bytes="512MiB",
            cache="yes",
            sub_auto="no",
            audio_file_auto="no",
            volume=80,
            # No built-in Lua scripts: the app needs none of them, and LuaJIT raises SEH exceptions
            # (code 0xE24C4A02) as part of normal operation inside our process.
            load_scripts="no",
            ytdl="no",
            load_stats_overlay="no",
            load_console="no",
            load_auto_profiles="no",
            load_select="no",
            load_commands="no",
            load_context_menu="no",
            load_positioning="no",
        )
        if mpv_log_file:
            opts["log_file"] = mpv_log_file
        # Diagnostics: ACS_MPV_OPTS="vo=gpu,hwdec=no" overrides any option above.
        for item in filter(None, os.environ.get("ACS_MPV_OPTS", "").split(",")):
            k, _, v = item.partition("=")
            opts[k.strip().replace("-", "_")] = v.strip()
        self.mpv = self._lib.MPV(start_event_thread=False, **opts)
        trace.log("player", "mpv options: " + ", ".join(f"{k}={v}" for k, v in opts.items()
                                                       if k not in ("wid", "log_file")))
        trace.log("player", f"libmpv started (no event thread) hwdec={hwdec} log_file={mpv_log_file or '-'}")
        self._path = ""
        self._alive = True
        self._seen_time: float | None = None
        self._seen_pause: bool | None = None
        self._seen_duration: float | None = None
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._poll_state)
        self._poll.start()

    # ---- GUI-thread polling ----
    def _prop(self, name: str):
        try:
            return getattr(self.mpv, name)
        except Exception:  # noqa: BLE001 - unavailable while loading / idle
            return None

    def _drain_events(self) -> None:
        """Discard queued client events (we poll properties instead of decoding events)."""
        handle = getattr(self.mpv, "_event_handle", None)
        if not handle:
            return
        none_id = self._lib.MpvEventID.NONE
        for _ in range(256):
            ev = self._lib._mpv_wait_event(handle, 0).contents  # noqa: SLF001
            if ev.event_id.value == none_id:
                break

    def _poll_state(self) -> None:
        if not self._alive:
            return
        self._drain_events()
        if not self._path:
            return
        d = self._prop("duration")
        if d is not None and d != self._seen_duration:
            self._seen_duration = float(d)
            trace.log("player", f"file loaded, duration={d:.3f}")
            self.duration_changed.emit(float(d))
            self.file_loaded.emit()
        p = self._prop("pause")
        if p is not None and bool(p) != self._seen_pause:
            self._seen_pause = bool(p)
            trace.log("player", f"pause={bool(p)}")
            self.pause_changed.emit(bool(p))
        t = self._prop("time_pos")
        if t is not None and t != self._seen_time:
            self._seen_time = float(t)
            trace.throttled("player.time", 1.0, "player", f"time={t:.3f}")
            self.time_changed.emit(float(t))

    def _cmd(self, *args: str) -> None:
        try:
            self.mpv.command(*args)
        except Exception as e:  # noqa: BLE001 - e.g. seek while nothing is loaded
            trace.log("player", f"command {args} failed: {e}")

    # ---- control ----
    def load(self, path: str, start: float = 0.0, aid: int | None = None, sid: int | None = None) -> None:
        trace.log("player", f"load {path!r} start={start:.3f} aid={aid} sid={sid}")
        self._path = path
        self._seen_duration = None
        self._seen_time = None
        self.mpv["start"] = f"{max(0.0, start):.6f}"
        self.mpv["pause"] = True
        self.mpv["aid"] = "auto" if aid is None else str(aid + 1)
        self.mpv["sid"] = "no" if sid is None else str(sid + 1)
        self._cmd("loadfile", path, "replace")

    def unload(self) -> None:
        trace.log("player", "unload")
        self._path = ""
        self._cmd("stop")

    @property
    def paused(self) -> bool:
        p = self._prop("pause")
        return True if p is None else bool(p)

    def set_paused(self, paused: bool) -> None:
        trace.log("player", f"set_paused {paused}")
        self.mpv.pause = paused

    def toggle_pause(self) -> None:
        if not self._path:
            return
        trace.log("player", "toggle_pause")
        self.mpv.pause = not self.paused

    def seek(self, t: float) -> None:
        if self._path:
            trace.throttled("player.seek", 0.25, "player", f"seek {t:.3f}")
            self._cmd("seek", f"{max(0.0, t):.6f}", "absolute+exact")

    def frame_step(self) -> None:
        if self._path:
            trace.log("player", "frame-step")
            self._cmd("frame-step")

    def frame_back_step(self) -> None:
        if self._path:
            trace.log("player", "frame-back-step")
            self._cmd("frame-back-step")

    def set_speed(self, speed: float) -> None:
        trace.log("player", f"speed {speed}")
        self.mpv.speed = speed

    @property
    def speed(self) -> float:
        s = self._prop("speed")
        return 1.0 if s is None else float(s)

    def set_volume(self, v: int) -> None:
        trace.throttled("player.volume", 0.5, "player", f"volume {v}")
        self.mpv.volume = max(0, min(130, v))

    def set_mute(self, m: bool) -> None:
        self.mpv.mute = m

    def set_aid(self, rel: int | None) -> None:
        trace.log("player", f"aid {rel}")
        self.mpv["aid"] = "no" if rel is None else str(rel + 1)

    def set_sid(self, rel: int | None) -> None:
        trace.log("player", f"sid {rel}")
        self.mpv["sid"] = "no" if rel is None else str(rel + 1)

    def set_crop(self, mpv_rect: str | None) -> None:
        trace.log("player", f"video-crop {mpv_rect or 'off'}")
        try:
            self.mpv["video-crop"] = mpv_rect or ""
        except Exception as e:  # noqa: BLE001 - older libmpv without video-crop
            log.info("video-crop unsupported: %s", e)

    def shutdown(self) -> None:
        if not self._alive:
            return
        trace.log("player", "shutdown")
        self._alive = False
        self._poll.stop()
        # The secondary event client must be destroyed *before* terminate(): mpv_terminate_destroy
        # blocks until every client handle is gone (otherwise closing the app hangs).
        handle = getattr(self.mpv, "_event_handle", None)
        if handle:
            try:
                self._lib._mpv_destroy(handle)  # noqa: SLF001 - the event client is ours to free
            except Exception:  # noqa: BLE001
                pass
            self.mpv._event_handle = None
        try:
            self.mpv.terminate()
        except Exception:  # noqa: BLE001
            pass
