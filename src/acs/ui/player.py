"""Embedded libmpv player widget.

Threading rule: python-mpv's own event thread is NOT started. That thread converted libmpv event
structures with ctypes concurrently with the GUI thread and crashed the interpreter (access
violations in python313.dll / mpv.node_cast_value). Instead everything runs on the GUI thread:
a QTimer drains the client's event queue. time-pos / pause / duration are *observed* (mpv pushes
changes as events, read here with a minimal ctypes struct, never python-mpv's node decoders), so
polling never waits for a busy mpv core. Seeks are sent asynchronously and merged: at most one is
in flight, newer requests replace the waiting one.
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QWidget

from acs.core import trace

log = logging.getLogger("acs.player")

_mpv_module = None
POLL_SEEK_MS = 4       # a seek is running: notice its end at once, so the next one follows without delay
POLL_FAST_MS = 15      # playing or right after a command
POLL_IDLE_MS = 60      # paused, nothing happening
POLL_EMPTY_MS = 250    # no file loaded
FAST_AFTER_CMD = 1.0   # seconds of fast polling after a command
# A seek with no "playback restart" event counts as done after this. Generous: an exact seek on a
# slow PC can take ~0.5 s, and sending the next one earlier would abort it before any frame shows.
SEEK_TIMEOUT = 1.0

_OBS_TIME, _OBS_PAUSE, _OBS_DURATION = 1, 2, 3
_FMT_FLAG, _FMT_DOUBLE = 3, 5


class _PropEvent(ctypes.Structure):
    """mpv_event_property: {const char *name; mpv_format format; void *data}."""

    _fields_ = [("name", ctypes.c_char_p), ("format", ctypes.c_int), ("data", ctypes.c_void_p)]


def _total_ram() -> int:
    """Physical memory in bytes (0 if unknown)."""
    if os.name != "nt":
        return 0

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    try:
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullTotalPhys)
    except (OSError, AttributeError):
        pass
    return 0


def demuxer_cache() -> tuple[str, str]:
    """(forward, back) demuxer cache: generous normally, smaller below 8 GB of RAM so the cache
    (up to 1 GiB for one episode) does not push the system into swapping."""
    ram = _total_ram()
    if 0 < ram < 8 * 1000 ** 3:
        return "256MiB", "128MiB"
    return "512MiB", "512MiB"


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
        cache_fwd, cache_back = demuxer_cache()
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
            demuxer_max_bytes=cache_fwd,
            demuxer_max_back_bytes=cache_back,
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
        # latest values pushed by mpv (observed properties)
        self._time: float | None = None
        self._pause: bool | None = None
        self._duration: float | None = None
        self._crop: str | None = None
        self._seek_want: tuple[float, bool] | None = None   # newest request not sent yet
        self._seek_sent: float | None = None                # monotonic time of the seek in flight
        self._seek_precise = True
        self.seek_latency = 0.05   # smoothed duration of an exact seek on this PC / file (seconds)
        self._fast_until = 0.0
        self._ev = getattr(self.mpv, "_event_handle", None)
        self._observing = False
        if self._ev:
            try:
                for uid, name, fmt in ((_OBS_TIME, b"time-pos", _FMT_DOUBLE), (_OBS_PAUSE, b"pause", _FMT_FLAG),
                                       (_OBS_DURATION, b"duration", _FMT_DOUBLE)):
                    self._lib._mpv_observe_property(self._ev, uid, name, fmt)  # noqa: SLF001
                self._observing = True
            except Exception as e:  # noqa: BLE001 - fall back to synchronous property reads
                trace.log("player", f"observe_property failed, polling instead: {e}")
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_EMPTY_MS)
        self._poll.timeout.connect(self._poll_state)
        self._poll.start()

    # ---- GUI-thread polling ----
    def _prop(self, name: str):
        try:
            return getattr(self.mpv, name)
        except Exception:  # noqa: BLE001 - unavailable while loading / idle
            return None

    def _drain_events(self) -> None:
        """Handle queued client events: observed property changes and seek completion."""
        if not self._ev:
            return
        ids = self._lib.MpvEventID
        for _ in range(256):
            ev = self._lib._mpv_wait_event(self._ev, 0).contents  # noqa: SLF001
            eid = ev.event_id.value
            if eid == ids.NONE:
                break
            if eid == ids.PROPERTY_CHANGE and ev._data:  # noqa: SLF001
                self._on_property(int(ev.reply_userdata), ev._data)  # noqa: SLF001
            elif eid == ids.PLAYBACK_RESTART:
                if self._seek_sent is not None and self._seek_precise:
                    took = time.monotonic() - self._seek_sent
                    self.seek_latency = 0.7 * self.seek_latency + 0.3 * took
                self._seek_sent = None

    def _on_property(self, uid: int, ptr: int) -> None:
        p = ctypes.cast(ptr, ctypes.POINTER(_PropEvent)).contents
        if uid == _OBS_PAUSE:
            if p.data and p.format == _FMT_FLAG:
                self._pause = bool(ctypes.c_int.from_address(p.data).value)
            return
        value = ctypes.c_double.from_address(p.data).value if p.data and p.format == _FMT_DOUBLE else None
        if uid == _OBS_TIME:
            self._time = value
        elif uid == _OBS_DURATION:
            self._duration = value

    def _read_sync(self) -> None:
        """Fallback when properties cannot be observed (old python-mpv / libmpv)."""
        self._duration = self._prop("duration")
        self._pause = self._prop("pause")
        self._time = self._prop("time_pos")

    def _poll_state(self) -> None:
        if not self._alive:
            return
        self._drain_events()
        self._pump_seek()
        self._retime()
        if not self._path:
            return
        if not self._observing:
            self._read_sync()
        d = self._duration
        if d is not None and d != self._seen_duration:
            self._seen_duration = float(d)
            trace.log("player", f"file loaded, duration={d:.3f}")
            self.duration_changed.emit(float(d))
            self.file_loaded.emit()
        p = self._pause
        if p is not None and bool(p) != self._seen_pause:
            self._seen_pause = bool(p)
            trace.log("player", f"pause={bool(p)}")
            self.pause_changed.emit(bool(p))
        t = self._time
        if t is not None and t != self._seen_time:
            self._seen_time = float(t)
            trace.throttled("player.time", 1.0, "player", f"time={t:.3f}")
            self.time_changed.emit(float(t))

    def _retime(self) -> None:
        """Poll fast only while something can change (playback, seeks, recent commands)."""
        if not self._path:
            ms = POLL_EMPTY_MS
        elif self.seek_busy:
            ms = POLL_SEEK_MS
        elif self._seen_pause is False or time.monotonic() < self._fast_until:
            ms = POLL_FAST_MS
        else:
            ms = POLL_IDLE_MS
        if self._poll.interval() != ms:
            self._poll.setInterval(ms)

    def _kick(self) -> None:
        self._fast_until = time.monotonic() + FAST_AFTER_CMD
        if self._poll.interval() > POLL_FAST_MS:
            self._poll.setInterval(POLL_FAST_MS)

    def _cmd(self, *args: str) -> None:
        self._kick()
        try:
            self.mpv.command(*args)
        except Exception as e:  # noqa: BLE001 - e.g. seek while nothing is loaded
            trace.log("player", f"command {args} failed: {e}")

    def _cmd_async(self, name: str, *args: str) -> None:
        """Queue a command without waiting for the mpv core; the reply event is drained and ignored."""
        self._kick()
        if not self._ev:
            self._cmd(name, *args)
            return
        try:
            arr = self._lib._create_null_term_cmd_arg_array(name, args)  # noqa: SLF001
            self._lib._mpv_command_async(self._ev, 0, arr)  # noqa: SLF001
        except Exception as e:  # noqa: BLE001
            trace.log("player", f"async command {name} {args} failed: {e}")

    # ---- seeking ----
    @property
    def seek_busy(self) -> bool:
        """A seek is running or waiting; the reported time may still be the old position."""
        return self._seek_want is not None or self._seek_in_flight()

    def _seek_in_flight(self) -> bool:
        if self._seek_sent is not None and time.monotonic() - self._seek_sent >= SEEK_TIMEOUT:
            if self._seek_precise:   # no restart event: count it as (at least) this slow
                self.seek_latency = 0.7 * self.seek_latency + 0.3 * SEEK_TIMEOUT
            self._seek_sent = None
        return self._seek_sent is not None

    def _pump_seek(self) -> None:
        if self._seek_want is None or self._seek_in_flight():
            return
        t, precise = self._seek_want
        self._seek_want = None
        self._seek_sent = time.monotonic()
        self._seek_precise = precise
        trace.throttled("player.seek", 0.25, "player", f"seek {t:.3f} {'exact' if precise else 'keyframes'}")
        self._cmd_async("seek", f"{t:.6f}", "absolute+exact" if precise else "absolute+keyframes")
        self._poll.setInterval(POLL_SEEK_MS)

    # ---- control ----
    def load(self, path: str, start: float = 0.0, aid: int | None = None, sid: int | None = None) -> None:
        trace.log("player", f"load {path!r} start={start:.3f} aid={aid} sid={sid}")
        self._drain_events()   # values of the previous file must not count for this one
        self._path = path
        self._seen_duration = None
        self._seen_time = None
        self._time = self._duration = None
        self._seek_want = self._seek_sent = None
        self.mpv["start"] = f"{max(0.0, start):.6f}"
        self.mpv["pause"] = True
        self.mpv["aid"] = "auto" if aid is None else str(aid + 1)
        self.mpv["sid"] = "no" if sid is None else str(sid + 1)
        self._cmd("loadfile", path, "replace")

    def unload(self) -> None:
        trace.log("player", "unload")
        self._path = ""
        self._seek_want = self._seek_sent = None
        self._cmd("stop")

    @property
    def paused(self) -> bool:
        p = self._pause if self._observing else self._prop("pause")
        return True if p is None else bool(p)

    def set_paused(self, paused: bool) -> None:
        trace.log("player", f"set_paused {paused}")
        self._kick()
        self.mpv.pause = paused

    def toggle_pause(self) -> None:
        if not self._path:
            return
        trace.log("player", "toggle_pause")
        self._kick()
        self.mpv.pause = not self.paused

    def seek(self, t: float, precise: bool = True) -> None:
        """precise=False: nearest keyframe (instant, used while scrubbing); True: exact frame."""
        if self._path:
            self._seek_want = (max(0.0, t), precise)
            self._pump_seek()

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
        if (mpv_rect or "") == self._crop:
            return  # sent on every selection change; skip the synchronous call when nothing changes
        self._crop = mpv_rect or ""
        trace.log("player", f"video-crop {mpv_rect or 'off'}")
        try:
            self.mpv["video-crop"] = mpv_rect or ""
        except Exception as e:  # noqa: BLE001 - older libmpv without video-crop
            log.info("video-crop unsupported: %s", e)

    def set_osd_overlay(self, ass: str | None, res_x: int = 0, res_y: int = 0) -> None:
        """ASS events drawn by mpv over the picture (coordinates in a res_x × res_y space); None
        removes them. Asynchronous: called for every mouse move while the Shorts frame is dragged."""
        if ass is None:
            self._cmd_async("osd-overlay", "1", "none", "")
        else:
            self._cmd_async("osd-overlay", "1", "ass-events", ass, str(res_x), str(res_y))

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
