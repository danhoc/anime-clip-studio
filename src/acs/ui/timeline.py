"""Timeline widget: ruler, thumbnails, waveform, keyframes, clip lanes, markers, playhead.

Zoom/scroll changes go through ProjectDocument.set_view (undoable, merged); auto-follow during
playback changes the view without touching the undo stack.
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QColorDialog, QInputDialog, QMenu, QSizePolicy, QWidget

from acs.core import trace
from acs.core.document import ProjectDocument
from acs.core.model import MARKER_COLOR_NAMES, MARKER_COLORS
from acs.core.util import fmt_time
from acs.core.waveform import DEFAULT_BPS
from acs.i18n import tr
from acs.ui import theme

RULER_H = 26
LANE_H = 26
LANE_GAP = 4
# Fixed row heights (logical px) chosen once from the screen height — the timeline does not stretch
# with the window, so resizing panels never re-scales hundreds of thumbnails or the waveform.
ROW_SIZES = (  # (min logical screen height, thumbnail row, waveform row)
    (1300, 120, 96),
    (1000, 100, 80),
    (0, 76, 60),
)
DEFAULT_LANES = 3                # minimum clip-lane area; the splitter can make it taller
CLICK_PX = 4                     # a press on a clip edge that moves less than this is a click (seek)
WAVE_REF_PERCENTILE = 99.97     # auto gain reference: (almost) the loudest moment of the track
WAVE_KNEE = 0.85                 # above this, peaks are compressed smoothly instead of clipped
EDGE_PX = 6
SNAP_PX = 8
MAX_PX_PER_FRAME = 48
TICK_STEPS = [1 / 24, 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1200]

C_BG = QColor("#1b1d22")
C_RULER = QColor("#24272e")
C_TEXT = QColor("#c8ccd4")
C_DIM = QColor("#6b717d")
C_KEY = QColor("#e0b341")
C_PLAYHEAD = QColor("#ff4d4d")
C_MARKER = QColor("#f5c542")
C_WAVE = QColor("#4fb38f")
C_GRID = QColor(255, 255, 255, 22)
C_THUMBS_BG = QColor("#15171b")
C_WAVE_BG = QColor("#191c20")
C_OUTLINE = QColor("#ffffff")      # selection outline
C_CLIP_TEXT = QColor("#ffffff")
C_LANES_BG = QColor("#16181c")
C_LANE = QColor(255, 255, 255, 10)


def _sync_colors() -> None:
    """Pick up the active theme (dark / light)."""
    global C_BG, C_RULER, C_TEXT, C_DIM, C_KEY, C_GRID, C_WAVE, C_THUMBS_BG, C_WAVE_BG, C_OUTLINE, C_CLIP_TEXT
    global C_LANES_BG, C_LANE
    t = theme.current()
    C_BG, C_RULER = QColor(t.timeline_bg), QColor(t.ruler_bg)
    C_TEXT, C_DIM = QColor(t.text), QColor(t.dim)
    C_KEY = QColor("#e0b341" if t.name == "dark" else "#b07d00")
    C_GRID = theme.qcolor(t.grid)
    C_WAVE, C_THUMBS_BG, C_WAVE_BG = QColor(t.wave), QColor(t.thumbs_bg), QColor(t.wave_bg)
    C_OUTLINE = QColor("#ffffff" if t.name == "dark" else "#10141a")
    C_CLIP_TEXT = QColor(t.clip_label)
    dark = t.name == "dark"
    C_LANES_BG = QColor(t.thumbs_bg)
    C_LANE = QColor(255, 255, 255, 12) if dark else QColor(0, 0, 0, 14)


class Timeline(QWidget):
    seek_requested = Signal(float)
    hint = Signal(str)
    zoom_to_clip_requested = Signal()
    context_action = Signal(str, float)   # action id, time

    def __init__(self, doc: ProjectDocument, parent=None):
        super().__init__(parent)
        self.doc = doc
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._row_sizes = ROW_SIZES[1][1:]
        self.position = 0.0
        self.thumb_times: np.ndarray | None = None
        self.thumb_source: list[QImage] = []   # as decoded (72 px tall)
        self.thumb_images: list[QImage] = []   # scaled to the current row height
        self._thumb_h_cached = 0
        self.wave: np.ndarray | None = None
        self.wave_bps = DEFAULT_BPS
        self._drag: dict | None = None
        _sync_colors()
        self._lanes: dict[str, int] = {}
        self._lane_count = 1
        self._apply_min_height()
        doc.markers_changed.connect(self.update)
        doc.clips_changed.connect(self._relayout)
        doc.view_changed.connect(self.update)
        doc.selection_changed.connect(self.update)
        doc.loaded.connect(self._on_loaded)
        doc.index_ready.connect(self.update)

    # ---- data feeds ----------------------------------------------------------------------
    def _on_loaded(self):
        self.thumb_times, self.thumb_source, self.thumb_images, self.wave = None, [], [], None
        self._thumb_h_cached = 0
        self.position = self.doc.project.view.position
        self._relayout()

    def set_thumbnails(self, times: np.ndarray, images: np.ndarray) -> None:
        self.thumb_times = times
        self.thumb_source = []
        for img in images:
            h, w, _ = img.shape
            self.thumb_source.append(QImage(img.data, w, h, w * 3, QImage.Format.Format_RGB888).copy())
        self._thumb_h_cached = 0
        self.update()

    def _scaled_thumbs(self, h: int) -> list[QImage]:
        """Thumbnails scaled to the row height in *device* pixels (crisp on HiDPI screens)."""
        dpr = self.devicePixelRatioF()
        key = (h, round(dpr, 3))
        if key != self._thumb_h_cached:
            mode = Qt.TransformationMode.SmoothTransformation
            images = []
            for q in self.thumb_source:
                s = q.scaledToHeight(max(1, round(h * dpr)), mode)
                s.setDevicePixelRatio(dpr)
                images.append(s)
            self.thumb_images = images
            self._thumb_h_cached = key
        return self.thumb_images

    def set_waveform(self, peaks: np.ndarray | None, bps: int = DEFAULT_BPS) -> None:
        self.wave = peaks
        self.wave_bps = bps
        # Auto gain: downmixed 5.1 / quiet masters otherwise use only a fraction of the row.
        if peaks is not None and len(peaks):
            ref = float(np.percentile(peaks, WAVE_REF_PERCENTILE))
            self.wave_ref = max(8.0, ref)
        else:
            self.wave_ref = 255.0
        self.update()

    def set_position(self, t: float, follow: bool = False) -> None:
        old_x = self.t2x(self.position)
        self.position = t
        if follow and self.doc.has_video:
            x = self.t2x(t)
            if x < 0 or x > self.width() - 20:
                pps = self.pps
                self.doc._set_view(self.doc.project.view.pps, self._clamp_scroll(t - 0.1 * self.width() / pps, pps))
                return
        new_x = self.t2x(t)
        lo, hi = min(old_x, new_x) - 8, max(old_x, new_x) + 8
        self.update(int(lo), 0, int(hi - lo) + 1, self.height())

    # ---- view mapping --------------------------------------------------------------------
    @property
    def duration(self) -> float:
        return self.doc.index.end_time if self.doc.index else 1.0

    @property
    def fit_pps(self) -> float:
        return max(1e-3, (self.width() - 8) / max(0.04, self.duration))

    @property
    def max_pps(self) -> float:
        fps = float(self.doc.index.fps) if self.doc.index else 24.0
        return MAX_PX_PER_FRAME * fps

    @property
    def pps(self) -> float:
        v = self.doc.project.view.pps
        return self.fit_pps if v <= 0 else max(self.fit_pps, min(v, self.max_pps))

    @property
    def scroll(self) -> float:
        return self._clamp_scroll(self.doc.project.view.scroll if self.doc.project.view.pps > 0 else 0.0, self.pps)

    def _clamp_scroll(self, s: float, pps: float) -> float:
        return max(0.0, min(s, max(0.0, self.duration - (self.width() - 8) / pps)))

    def t2x(self, t: float) -> float:
        return (t - self.scroll) * self.pps

    def x2t(self, x: float) -> float:
        return self.scroll + x / self.pps

    # ---- zoom / scroll (undoable) ----------------------------------------------------------
    def zoom_at(self, factor: float, x: float | None = None) -> None:
        if not self.doc.has_video:
            return
        x = self.width() / 2 if x is None else x
        t = self.x2t(x)
        new_pps = max(self.fit_pps, min(self.pps * factor, self.max_pps))
        if new_pps <= self.fit_pps * 1.0001:
            self.doc.set_view(0.0, 0.0)
            return
        self.doc.set_view(new_pps, self._clamp_scroll(t - x / new_pps, new_pps))

    def zoom_fit(self) -> None:
        self.doc.set_view(0.0, 0.0, tr("Fit timeline"))

    def zoom_range(self, t0: float, t1: float) -> None:
        span = max(0.2, t1 - t0)
        pps = max(self.fit_pps, min((self.width() - 40) / span, self.max_pps))
        self.doc.set_view(pps, self._clamp_scroll(t0 - 20 / pps, pps), tr("Zoom to clip"))

    def scroll_by(self, px: float) -> None:
        if self.doc.project.view.pps <= 0:
            return
        pps = self.pps
        self.doc.set_view(self.doc.project.view.pps, self._clamp_scroll(self.scroll + px / pps, pps),
                          tr("Scroll timeline"))

    # ---- layout --------------------------------------------------------------------------
    def _relayout(self):
        """Greedy lane assignment so overlapping clips never hide each other."""
        lanes_end: list[int] = []
        self._lanes = {}
        for c in sorted(self.doc.project.clips, key=lambda c: c.in_frame):
            end = c.out_frame if c.out_frame is not None else c.in_frame + 1
            for i, e in enumerate(lanes_end):
                if c.in_frame >= e:
                    lanes_end[i] = end
                    self._lanes[c.id] = i
                    break
            else:
                lanes_end.append(end)
                self._lanes[c.id] = len(lanes_end) - 1
        self._lane_count = max(1, len(lanes_end))
        self.update()  # height never follows the clips: extra lanes are squeezed into the lane area

    def configure_for_screen(self, logical_screen_height: int) -> None:
        """Pick fixed row heights for this display (called once by the main window)."""
        for min_h, th, wh in ROW_SIZES:
            if logical_screen_height >= min_h:
                self._row_sizes = (th, wh)
                break
        self._apply_min_height()

    def _apply_min_height(self) -> None:
        """Rows above the lanes are fixed; only the clip-lane area below them may grow (splitter)."""
        self.setMinimumHeight(self._clips_y + DEFAULT_LANES * (LANE_H + LANE_GAP) + 4)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

    def sizeHint(self):
        return self.minimumSizeHint()

    def minimumSizeHint(self):
        s = super().minimumSizeHint()
        s.setHeight(self.minimumHeight())
        return s

    @property
    def _lanes_h(self) -> int:
        """Height of the clip-lane area (at least DEFAULT_LANES full-size lanes)."""
        return max(DEFAULT_LANES * (LANE_H + LANE_GAP), self.height() - self._clips_y - 4)

    def _lane_geom(self) -> tuple[float, float]:
        """(lane pitch, lane height): full size while the lanes fit, squeezed when clips overlap more."""
        pitch = min(float(LANE_H + LANE_GAP), self._lanes_h / max(1, self._lane_count))
        height = max(6.0, pitch - min(LANE_GAP, pitch * 0.15))
        return pitch, height

    def _rows(self) -> tuple[int, int]:
        """(thumbnail height, waveform height) — fixed, independent of the widget height."""
        return getattr(self, "_row_sizes", ROW_SIZES[1][1:])

    @property
    def _thumbs_y(self) -> int:
        return RULER_H

    @property
    def _wave_y(self) -> int:
        return RULER_H + self._rows()[0]

    @property
    def _clips_y(self) -> int:
        th, wh = self._rows()
        return RULER_H + th + wh + 6

    def _clip_rect(self, c) -> QRectF:
        x0 = self.t2x(self.doc.time_of(c.in_frame))
        if c.out_frame is None:
            x1 = max(x0 + 2, self.t2x(self.position))
        else:
            x1 = self.t2x(self.doc.time_of(c.out_frame))
        lane = self._lanes.get(c.id, 0)
        pitch, height = self._lane_geom()
        y = self._clips_y + lane * pitch
        return QRectF(x0, y, max(2.0, x1 - x0), height)

    # ---- painting ------------------------------------------------------------------------
    def paintEvent(self, ev):
        trace.vlog("paint", f"timeline begin rect={ev.rect().x()},{ev.rect().width()} pps={self.pps:.2f}")
        self._paint(ev)
        trace.vlog("paint", "timeline end")

    def _paint(self, ev):
        _sync_colors()
        self._dirty_rect = ev.rect()
        p = QPainter(self)
        p.fillRect(self.rect(), C_BG)
        if not self.doc.has_video:
            p.setPen(C_DIM)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       tr("Open a video to see its timeline. Press I to start a clip, O to end it."))
            return
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        self._paint_thumbs(p)
        self._paint_wave(p)
        self._paint_ruler(p)
        self._paint_lanes(p)
        self._paint_grid(p)
        self._paint_clips(p)
        self._paint_markers(p)
        self._paint_playhead(p)

    def _visible_range(self) -> tuple[float, float]:
        return self.x2t(0), self.x2t(self.width())

    def _paint_ruler(self, p: QPainter):
        w = self.width()
        p.fillRect(0, 0, w, RULER_H, C_RULER)
        pps = self.pps
        step = next((s for s in TICK_STEPS if s * pps >= 90), TICK_STEPS[-1])
        t0, t1 = self._visible_range()
        p.setFont(self._font(9))
        minor = step / 5
        k = math.floor(t0 / minor)
        while True:
            t = k * minor
            if t > t1:
                break
            x = int(self.t2x(t))
            major = abs(t / step - round(t / step)) < 1e-6
            p.setPen(C_TEXT if major else C_DIM)
            p.drawLine(x, RULER_H - (10 if major else 5), x, RULER_H)
            if major:
                ms = step < 1
                p.drawText(x + 3, 13, fmt_time(t, ms=ms)[3:] if t < 3600 else fmt_time(t, ms=ms))
            k += 1
        # keyframes
        idx = self.doc.index
        keys = idx.keyframes if idx is not None else np.zeros(0)
        if len(keys) and idx is not None:
            f0 = idx.frame_at(t0)
            f1 = idx.frame_at(t1) + 1
            lo, hi = np.searchsorted(keys, [f0, f1])
            vis = keys[lo:hi]
            if len(vis) < w / 3:
                p.setPen(QPen(C_KEY, 1))
                for f in vis:
                    x = int(self.t2x(idx.time_of(int(f))))
                    p.drawLine(x, RULER_H - 4, x, RULER_H)

    def _paint_grid(self, p: QPainter):
        idx = self.doc.index
        if idx is None or self.pps * idx.frame_duration < 8:
            return
        t0, t1 = self._visible_range()
        f0, f1 = idx.frame_at(t0), idx.frame_at(t1) + 1
        p.setPen(QPen(C_GRID, 1))
        top, bottom = RULER_H, self.height()
        for f in range(f0, min(f1 + 1, idx.frame_count)):
            x = int(self.t2x(idx.time_of(f)))
            p.drawLine(x, top, x, bottom)

    def _font(self, pt: int) -> QFont:
        f = QFont(self.font())
        f.setPointSize(pt)
        return f

    def _paint_thumbs(self, p: QPainter):
        y = self._thumbs_y
        th = self._rows()[0]
        p.fillRect(0, y, self.width(), th, C_THUMBS_BG)
        if self.thumb_times is None or not self.thumb_source:
            p.setPen(C_DIM)
            p.drawText(QRectF(0, y, self.width(), th), Qt.AlignmentFlag.AlignCenter,
                       tr("generating thumbnails…") if self.doc.has_video else "")
            return
        images = self._scaled_thumbs(th)
        tw = images[0].deviceIndependentSize().width()
        x = 0.0
        times = self.thumb_times
        while x < self.width():
            t = self.x2t(x + tw / 2)
            i = int(np.searchsorted(times, t, side="right")) - 1
            i = max(0, min(i, len(images) - 1))
            p.drawImage(QPointF(x, y), images[i])
            x += tw

    def wave_envelope(self, x0: float, x1: float, step: float) -> tuple[np.ndarray, np.ndarray]:
        """Peak values (0..255) sampled every `step` logical px between x0 and x1.

        Zoomed out (several buckets per sample) -> maximum over the covered buckets;
        zoomed in (a bucket spans several samples) -> linear interpolation, so the
        envelope stays smooth instead of turning into stairs.
        """
        wave = self.wave
        n = len(wave)
        xs = np.arange(x0, x1 + step, step, dtype=np.float64)
        if len(xs) < 2 or n == 0:
            return xs, np.zeros(len(xs))
        b = (self.scroll + xs / self.pps) * self.wave_bps          # fractional bucket positions
        span = (b[-1] - b[0]) / (len(b) - 1)
        if span >= 1.0:
            # max over [start_i, start_{i+1}); the last column stops at the right edge, not at n
            end = int(min(n, max(1, np.ceil(b[-1]))))
            starts = np.clip(np.floor(b[:-1]).astype(np.int64), 0, end - 1)
            starts = np.maximum.accumulate(starts)
            uniq = np.unique(starts)
            maxima = np.maximum.reduceat(wave[:end], uniq)
            vals = maxima[np.searchsorted(uniq, starts)].astype(np.float64)
            vals = np.append(vals, vals[-1])
        else:
            centers = np.arange(n, dtype=np.float64) + 0.5
            vals = np.interp(b, centers, wave.astype(np.float64), left=0.0, right=0.0)
        vals[(b < 0) | (b > n)] = 0
        return xs, vals

    def _paint_wave(self, p: QPainter):
        y = self._wave_y
        h = self._rows()[1]
        p.fillRect(0, y, self.width(), h, C_WAVE_BG)
        if self.wave is None or len(self.wave) == 0:
            return
        r = getattr(self, "_dirty_rect", self.rect())
        x0, x1 = max(0, r.left() - 2), min(self.width(), r.right() + 3)
        step = 1.0 / max(1.0, self.devicePixelRatioF())     # one sample per *device* pixel
        xs, vals = self.wave_envelope(x0, x1, step)
        # The waveform is symmetric, so only its upper half is drawn, rising from the bottom edge:
        # the full row height becomes amplitude (twice the detail of a mirrored drawing).
        base = y + h - 1
        x = vals / getattr(self, "wave_ref", 255.0)
        # linear up to the knee, then a smooth tanh shoulder that approaches (never hits) the top:
        # loud passages keep their shape instead of turning into a flat clipped band
        k = WAVE_KNEE
        y = np.where(x < k, x, k + (1 - k) * np.tanh((x - k) / (1 - k)))
        amp = np.clip(y, 0.0, 1.0) * (h - 3)
        top = [QPointF(x, base - a) for x, a in zip(xs, amp)]
        poly = QPolygonF(top + [QPointF(xs[-1], base), QPointF(xs[0], base)])
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(C_WAVE)
        p.drawPolygon(poly)
        p.setPen(QPen(C_WAVE.darker(140) if theme.current().name == "light" else C_WAVE.lighter(130), 0.8))
        p.drawLine(QPointF(x0, base), QPointF(x1, base))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def _paint_lanes(self, p: QPainter):
        """Clip-lane area: one stripe per lane slot, so it reads as room for clips, not empty space."""
        y0 = self._clips_y
        w = self.width()
        p.fillRect(0, y0 - 3, w, self.height() - y0 + 3, C_LANES_BG)
        pitch, height = self._lane_geom()
        slots = max(self._lane_count, int((self._lanes_h + LANE_GAP) // pitch))
        used = set(self._lanes.values())
        p.setFont(self._font(9))
        hint_done = False
        for i in range(slots):
            r = QRectF(0, y0 + i * pitch, w, height)
            p.fillRect(r, C_LANE)
            if i in used or hint_done or height < 14:
                continue
            hint_done = True
            p.setPen(C_DIM)
            text = (tr("Clip lanes — press I to start a clip, O to end it") if not used
                    else tr("Free clip lane — overlapping clips are placed here"))
            p.drawText(r.adjusted(12, 0, -12, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)

    def _paint_clips(self, p: QPainter):
        sel = set(self.doc.selected_clips)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setFont(self._font(10))
        for c in self.doc.project.clips:
            r = self._clip_rect(c)
            if self._drag and self._drag.get("cid") == c.id and "preview" in self._drag:
                pin, pout = self._drag["preview"]
                x0 = self.t2x(self.doc.time_of(pin))
                x1 = self.t2x(self.doc.time_of(pout)) if pout is not None else r.right()
                r = QRectF(x0, r.y(), max(2.0, x1 - x0), r.height())
            if r.right() < 0 or r.left() > self.width():
                continue
            col = QColor(c.color or "#4f9dff")
            fill = QColor(col)
            fill.setAlpha(150 if c.id in sel else 95)
            path = QPainterPath()
            path.addRoundedRect(r, 3, 3)
            p.fillPath(path, fill)
            pen = QPen(C_OUTLINE if c.id in sel else col.lighter(130), 2 if c.id in sel else 1)
            if c.out_frame is None:
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawPath(path)
            self._paint_edge_brackets(p, r, col, c.out_frame is not None)
            label = c.name + ("  🔇" if c.muted else "") + ("  CC" if c.burn_subs else "")
            if r.width() > 30:
                p.setPen(C_CLIP_TEXT)
                p.setFont(self._font(10))
                p.drawText(r.adjusted(10, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                           label)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    def _paint_edge_brackets(self, p: QPainter, r: QRectF, col: QColor, closed: bool) -> None:
        """Clear "[" / "]" handles at the clip edges, plus In / Out tags when there is room."""
        solid = QColor(col)
        solid.setAlpha(255)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(solid)
        bw, tick = 4.0, 7.0
        # [ at the In edge
        p.drawRect(QRectF(r.left(), r.top(), bw, r.height()))
        p.drawRect(QRectF(r.left(), r.top(), tick, 2.5))
        p.drawRect(QRectF(r.left(), r.bottom() - 2.5, tick, 2.5))
        if closed:  # ] at the Out edge
            p.drawRect(QRectF(r.right() - bw, r.top(), bw, r.height()))
            p.drawRect(QRectF(r.right() - tick, r.top(), tick, 2.5))
            p.drawRect(QRectF(r.right() - tick, r.bottom() - 2.5, tick, 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if r.width() > 140:  # explicit tags only when the clip is wide enough to read them
            p.setFont(self._font(8))
            p.setPen(solid.lighter(150) if theme.current().name == "dark" else solid.darker(160))
            flags = Qt.AlignmentFlag.AlignVCenter
            p.drawText(r.adjusted(0, 0, -10, 0), flags | Qt.AlignmentFlag.AlignRight,
                       tr("Out") if closed else "…")

    def _paint_markers(self, p: QPainter):
        sel = set(self.doc.selected_markers)
        for m in self.doc.project.markers:
            frame = m.frame
            if self._drag and self._drag.get("mid") == m.id and "preview" in self._drag:
                frame = self._drag["preview"]
            x = self.t2x(self.doc.time_of(frame))
            if x < -10 or x > self.width() + 10:
                continue
            col = QColor(m.color or C_MARKER)
            selected = m.id in sel
            p.setPen(QPen(col, 2 if selected else 1, Qt.PenStyle.DashLine))
            p.drawLine(int(x), RULER_H, int(x), self.height())
            flag = QPolygonF([QPointF(x, 1), QPointF(x + 12, 8), QPointF(x, 15)])
            p.setBrush(col)  # the marker keeps its own colour; selection = white outline
            p.setPen(QPen(QColor("#ffffff"), 2) if selected else Qt.PenStyle.NoPen)
            p.drawPolygon(flag)
            if m.label:
                p.setFont(self._font(9))
                p.setPen(col)
                p.drawText(int(x) + 14, 13, m.label)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_playhead(self, p: QPainter):
        x = self.t2x(self.position)
        if x < -5 or x > self.width() + 5:
            return
        p.setPen(QPen(C_PLAYHEAD, 1))
        p.drawLine(int(x), 0, int(x), self.height())
        p.setBrush(C_PLAYHEAD)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPolygon(QPolygonF([QPointF(x - 6, 0), QPointF(x + 6, 0), QPointF(x, 8)]))
        p.setBrush(Qt.BrushStyle.NoBrush)

    # ---- hit testing & snapping ---------------------------------------------------------
    def _hit(self, pos: QPointF) -> dict:
        x, y = pos.x(), pos.y()
        if y < RULER_H:
            for m in reversed(self.doc.project.markers):
                mx = self.t2x(m.time)
                if mx - 3 <= x <= mx + 13 and y <= 17:
                    return {"kind": "marker", "mid": m.id}
            return {"kind": "ruler"}
        for c in reversed(self.doc.project.clips):
            r = self._clip_rect(c)
            if r.top() <= y <= r.bottom() and r.left() - EDGE_PX <= x <= r.right() + EDGE_PX:
                if abs(x - r.left()) <= EDGE_PX:
                    return {"kind": "clip_in", "cid": c.id}
                if c.out_frame is not None and abs(x - r.right()) <= EDGE_PX:
                    return {"kind": "clip_out", "cid": c.id}
                if r.left() <= x <= r.right():
                    return {"kind": "clip", "cid": c.id}
        for m in self.doc.project.markers:
            if abs(self.t2x(m.time) - x) <= 3:
                return {"kind": "marker", "mid": m.id}
        return {"kind": "empty"}

    def _snap_frame(self, x: float, exclude_cid: str | None = None, exclude_mid: str | None = None,
                    no_snap: bool = False) -> int:
        idx = self.doc.index
        f = idx.nearest_frame(self.x2t(x))
        if no_snap:
            return f
        best, best_d = f, SNAP_PX + 1
        cands = [idx.frame_at(self.position)]
        cands += [m.frame for m in self.doc.project.markers if m.id != exclude_mid]
        for c in self.doc.project.clips:
            if c.id != exclude_cid:
                cands.append(c.in_frame)
                if c.out_frame is not None:
                    cands.append(c.out_frame)
        k = idx.keyframes
        if len(k):
            i = int(np.searchsorted(k, f))
            cands += [int(v) for v in k[max(0, i - 1):i + 1]]
        for cf in cands:
            d = abs(self.t2x(idx.time_of(cf)) - x)
            if d < best_d:
                best, best_d = cf, d
        return best

    # ---- mouse ---------------------------------------------------------------------------
    def mousePressEvent(self, ev):
        if not self.doc.has_video:
            return
        if ev.button() == Qt.MouseButton.MiddleButton:
            self._drag = {"kind": "pan", "x": ev.position().x(), "scroll": self.scroll}
            return
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._hit(ev.position())
        kind = hit["kind"]
        ctrl = bool(ev.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if kind in ("clip_in", "clip_out"):
            c = self.doc.project.clip_by_id(hit["cid"])
            self.doc.select_clips([c.id])
            self._drag = {"kind": kind, "cid": c.id, "preview": (c.in_frame, c.out_frame),
                          "x": ev.position().x(), "moved": False}
        elif kind == "clip":
            ids = list(self.doc.selected_clips)
            if ctrl:
                ids = [i for i in ids if i != hit["cid"]] if hit["cid"] in ids else ids + [hit["cid"]]
            else:
                ids = [hit["cid"]]
            self.doc.select_clips(ids)
            self._seek_x(ev.position().x())
            self._drag = {"kind": "scrub"}
        elif kind == "marker":
            m = self.doc.project.marker_by_id(hit["mid"])
            self.doc.select_markers([m.id])
            self._drag = {"kind": "marker", "mid": m.id, "preview": m.frame}
        else:
            if not ctrl:
                self.doc.clear_selection()
            self._seek_x(ev.position().x())
            self._drag = {"kind": "scrub"}
        self.update()

    def _seek_x(self, x: float):
        idx = self.doc.index
        f = idx.nearest_frame(self.x2t(max(0.0, x)))
        t = idx.time_of(f)
        self.position = t
        self.seek_requested.emit(t)
        self.update()

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        if not self.doc.has_video:
            return
        no_snap = bool(ev.modifiers() & Qt.KeyboardModifier.AltModifier)
        d = self._drag
        if d is None:
            self._update_hover(pos)
            return
        kind = d["kind"]
        if kind == "scrub":
            self._seek_x(pos.x())
        elif kind == "pan":
            pps = self.pps
            self.doc._set_view(self.doc.project.view.pps,
                               self._clamp_scroll(d["scroll"] - (pos.x() - d["x"]) / pps, pps))
        elif kind in ("clip_in", "clip_out"):
            if not d["moved"] and abs(pos.x() - d["x"]) < CLICK_PX:
                return   # still a click: don't nudge the edge by a frame
            d["moved"] = True
            c = self.doc.project.clip_by_id(d["cid"])
            f = self._snap_frame(pos.x(), exclude_cid=c.id, no_snap=no_snap)
            pin, pout = d["preview"]
            if kind == "clip_in":
                limit = (pout - 1) if pout is not None else self.doc.frame_count - 1
                pin = max(0, min(f, limit))
            else:
                pout = max(pin + 1, min(f, self.doc.frame_count))
            d["preview"] = (pin, pout)
            self.hint.emit(f"In {fmt_time(self.doc.time_of(pin))}  Out "
                           f"{fmt_time(self.doc.time_of(pout)) if pout is not None else '…'}")
            self.update()
        elif kind == "marker":
            d["preview"] = self._snap_frame(pos.x(), exclude_mid=d["mid"], no_snap=no_snap)
            self.update()

    def mouseReleaseEvent(self, ev):
        d = self._drag
        self._drag = None
        if d is None:
            return
        if d["kind"] in ("clip_in", "clip_out"):
            pin, pout = d["preview"]
            c = self.doc.project.clip_by_id(d["cid"])
            if c is not None and not d["moved"]:
                # click on an edge: jump to the clip's first frame / last frame (Out is exclusive)
                f = c.in_frame if d["kind"] == "clip_in" or c.out_frame is None else c.out_frame - 1
                t = self.doc.time_of(f)
                self.position = t
                self.seek_requested.emit(t)
            elif c is not None:
                fields = {}
                if pin != c.in_frame:
                    fields["in_frame"] = pin
                if pout != c.out_frame:
                    fields["out_frame"] = pout
                if fields:
                    self.doc.set_clip_fields(c.id, fields, tr("Move clip edge"))
        elif d["kind"] == "marker":
            self.doc.move_marker(d["mid"], d["preview"])
        elif d["kind"] == "pan":
            # The drag moved the view live; record start -> end as a single undoable step.
            v = self.doc.project.view
            final = v.scroll
            self.doc._set_view(v.pps, d["scroll"])
            self.doc.set_view(v.pps, final, tr("Scroll timeline"))
        self.update()

    def mouseDoubleClickEvent(self, ev):
        hit = self._hit(ev.position())
        if hit["kind"] == "marker":
            self._drag = None
            self._rename_marker(hit["mid"])
            return
        if hit["kind"] in ("clip", "clip_in", "clip_out"):
            c = self.doc.project.clip_by_id(hit["cid"])
            t = self.doc.time_of(c.in_frame)
            self.position = t
            self.seek_requested.emit(t)

    def _update_hover(self, pos: QPointF):
        hit = self._hit(pos)
        k = hit["kind"]
        if k in ("clip_in", "clip_out"):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self.hint.emit(tr("Click — go to this clip edge · drag — move it · Alt — no snapping"))
        elif k == "marker":
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self.hint.emit(tr("Drag to move the marker · Del — delete"))
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)
            if self.doc.has_video:
                t = self.x2t(pos.x())
                f = self.doc.index.nearest_frame(t)
                self.hint.emit(f"{fmt_time(self.doc.time_of(f))}  #{f}   ·   "
                               + tr("Ctrl+wheel — zoom · wheel — scroll · click — seek · right click — menu"))

    def leaveEvent(self, _ev):
        self.hint.emit("")

    def wheelEvent(self, ev):
        if not self.doc.has_video:
            return
        dy = ev.angleDelta().y() or ev.angleDelta().x()
        steps = dy / 120.0
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_at(1.25 ** steps, ev.position().x())
        else:
            self.scroll_by(-steps * self.width() * 0.12)
        ev.accept()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.update()

    def contextMenuEvent(self, ev):
        if not self.doc.has_video:
            return
        pos = QPointF(ev.pos())
        t = self.doc.time_of(self.doc.index.nearest_frame(self.x2t(pos.x())))
        hit = self._hit(pos)
        menu = QMenu(self)

        def add(text: str, action: str):
            a = QAction(text, menu)
            a.triggered.connect(lambda: self.context_action.emit(action, t))
            menu.addAction(a)

        add(tr("Add marker here"), "marker")
        add(tr("Set In here (new clip)"), "set_in")
        add(tr("Set Out here"), "set_out")
        if hit["kind"] in ("clip", "clip_in", "clip_out"):
            self.doc.select_clips([hit["cid"]])
            menu.addSeparator()
            add(tr("Zoom to clip"), "zoom_clip")
            add(tr("Delete clip"), "delete")
        elif hit["kind"] == "marker":
            mid = hit["mid"]
            self.doc.select_markers([mid])
            menu.addSeparator()
            menu.addAction(tr("Rename marker…"), lambda: self._rename_marker(mid))
            colors = menu.addMenu(tr("Marker colour"))
            current = (self.doc.project.marker_by_id(mid).color or "").lower()
            for name, value in zip(MARKER_COLOR_NAMES, MARKER_COLORS):
                a = colors.addAction(self._swatch(value), tr(name))
                a.setCheckable(True)
                a.setChecked(value.lower() == current)
                a.triggered.connect(lambda _c=False, v=value: self.doc.set_marker_fields(
                    mid, {"color": v}, tr("Marker colour")))
            colors.addSeparator()
            colors.addAction(tr("Other colour…"), lambda: self._pick_marker_color(mid))
            add(tr("Delete marker"), "delete")
        menu.addSeparator()
        add(tr("Fit whole video"), "zoom_fit")
        menu.exec(ev.globalPos())

    @staticmethod
    def _swatch(color: str) -> QIcon:
        pm = QPixmap(14, 14)
        pm.fill(QColor(color))
        return QIcon(pm)

    def _rename_marker(self, mid: str) -> None:
        m = self.doc.project.marker_by_id(mid)
        if m is None:
            return
        text, ok = QInputDialog.getText(self, tr("Rename marker"), tr("Marker label:"), text=m.label)
        if ok:
            self.doc.set_marker_fields(mid, {"label": text.strip()}, tr("Rename marker"))

    def _pick_marker_color(self, mid: str) -> None:
        m = self.doc.project.marker_by_id(mid)
        if m is None:
            return
        c = QColorDialog.getColor(QColor(m.color or "#f5c542"), self, tr("Marker colour"))
        if c.isValid():
            self.doc.set_marker_fields(mid, {"color": c.name()}, tr("Marker colour"))
