"""ProjectDocument: the live project (model + media info + frame index + undo stack + selection)."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QUndoStack

from acs.core.commands import (AddClipCmd, AddMarkerCmd, MoveMarkerCmd, RemoveItemsCmd, SetClipFieldsCmd,
                               SetMarkerFieldsCmd, SetProjectFieldsCmd, SetViewCmd)
from acs.core.model import Clip, Marker, Project
from acs.core.probe import MediaInfo
from acs.core.timebase import FrameIndex
from acs.i18n import tr


class ProjectDocument(QObject):
    markers_changed = Signal()
    clips_changed = Signal()
    clip_changed = Signal(str)
    view_changed = Signal()
    selection_changed = Signal()
    project_changed = Signal()
    loaded = Signal()
    index_ready = Signal()
    dirty_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = Project()
        self.info: MediaInfo | None = None
        self.index: FrameIndex | None = None
        self.project_path: str | None = None
        self.undo = QUndoStack(self)
        self.undo.cleanChanged.connect(self._on_clean_changed)
        self.selected_clips: list[str] = []
        self.selected_markers: list[str] = []

    def _on_clean_changed(self, clean: bool) -> None:
        self.dirty_changed.emit(not clean)

    # ---- state ----------------------------------------------------------------------------
    @property
    def has_video(self) -> bool:
        return self.info is not None and self.index is not None

    @property
    def dirty(self) -> bool:
        return not self.undo.isClean()

    def load(self, project: Project, info: MediaInfo | None, index: FrameIndex | None,
             project_path: str | None) -> None:
        self.project = project
        self.info = info
        self.index = index
        self.project_path = project_path
        self.undo.clear()
        self.selected_clips, self.selected_markers = [], []
        self.loaded.emit()
        self.markers_changed.emit()
        self.clips_changed.emit()
        self.view_changed.emit()
        self.selection_changed.emit()

    def set_index(self, index: FrameIndex) -> None:
        """Exact index arrived (packet scan): re-derive frame numbers from stored times."""
        self.index = index
        p = self.project
        for m in p.markers:
            m.frame = index.nearest_frame(m.time)
            m.time = index.time_of(m.frame)
        for c in p.clips:
            c.in_frame = index.nearest_frame(c.in_time)
            c.in_time = index.time_of(c.in_frame)
            if c.out_time is not None:
                c.out_frame = max(c.in_frame + 1, index.nearest_frame(c.out_time))
                c.out_time = index.time_of(c.out_frame)
        self.index_ready.emit()
        self.markers_changed.emit()
        self.clips_changed.emit()

    def mark_saved(self) -> None:
        self.undo.setClean()

    def time_of(self, f: int) -> float:
        return self.index.time_of(f) if self.index else 0.0

    def frame_at(self, t: float) -> int:
        return self.index.frame_at(t) if self.index else 0

    @property
    def frame_count(self) -> int:
        return self.index.frame_count if self.index else 0

    def clamp_frame(self, f: int) -> int:
        return max(0, min(int(f), max(0, self.frame_count - 1)))

    # ---- private mutators (commands only) -------------------------------------------------
    def _changed(self) -> None:
        self.project_changed.emit()

    def _insert_marker(self, m: Marker, index: int | None = None) -> None:
        m.time = self.time_of(m.frame)
        ms = self.project.markers
        if index is None:
            ms.append(m)
            ms.sort(key=lambda x: x.frame)
        else:
            ms.insert(min(index, len(ms)), m)
        self.markers_changed.emit()
        self._changed()

    def _delete_marker(self, mid: str) -> None:
        self.project.markers = [m for m in self.project.markers if m.id != mid]
        if mid in self.selected_markers:
            self.selected_markers.remove(mid)
            self.selection_changed.emit()
        self.markers_changed.emit()
        self._changed()

    def _move_marker(self, mid: str, frame: int) -> None:
        m = self.project.marker_by_id(mid)
        if m is None:
            return
        m.frame = frame
        m.time = self.time_of(frame)
        self.project.markers.sort(key=lambda x: x.frame)
        self.markers_changed.emit()
        self._changed()

    def _set_marker_fields(self, mid: str, fields: dict) -> None:
        m = self.project.marker_by_id(mid)
        if m is None:
            return
        for k, v in fields.items():
            setattr(m, k, v)
        self.markers_changed.emit()
        self._changed()

    def _insert_clip(self, c: Clip, index: int | None = None) -> None:
        c.in_time = self.time_of(c.in_frame)
        c.out_time = self.time_of(c.out_frame) if c.out_frame is not None else None
        cs = self.project.clips
        cs.insert(len(cs) if index is None else min(index, len(cs)), c)
        self.clips_changed.emit()
        self._changed()

    def _delete_clip(self, cid: str) -> None:
        self.project.clips = [c for c in self.project.clips if c.id != cid]
        if cid in self.selected_clips:
            self.selected_clips.remove(cid)
            self.selection_changed.emit()
        self.clips_changed.emit()
        self._changed()

    def _set_clip_fields(self, cid: str, fields: dict) -> None:
        c = self.project.clip_by_id(cid)
        if c is None:
            return
        for k, v in fields.items():
            setattr(c, k, v)
        if "in_frame" in fields:
            c.in_time = self.time_of(c.in_frame)
        if "out_frame" in fields:
            c.out_time = self.time_of(c.out_frame) if c.out_frame is not None else None
        self.clip_changed.emit(cid)
        self.clips_changed.emit()
        self._changed()

    def _set_project_fields(self, fields: dict) -> None:
        for k, v in fields.items():
            setattr(self.project, k, v)
        self.clips_changed.emit()
        self._changed()

    def _set_view(self, pps: float, scroll: float) -> None:
        self.project.view.pps = pps
        self.project.view.scroll = scroll
        self.view_changed.emit()

    # ---- public editing API (undoable) ----------------------------------------------------
    def add_marker(self, frame: int, label: str = "") -> Marker:
        m = Marker(frame=self.clamp_frame(frame), time=0.0, label=label,
                   color=self.project.take_marker_color())
        self.undo.push(AddMarkerCmd(self, m))
        return m

    def set_marker_fields(self, mid: str, fields: dict, text: str = "Edit marker") -> None:
        m = self.project.marker_by_id(mid)
        if m is None or all(getattr(m, k) == v for k, v in fields.items()):
            return
        self.undo.push(SetMarkerFieldsCmd(self, mid, fields, text))

    def move_marker(self, mid: str, frame: int) -> None:
        m = self.project.marker_by_id(mid)
        if m is not None and m.frame != frame:
            self.undo.push(MoveMarkerCmd(self, mid, self.clamp_frame(frame), m.frame))

    def delete_selected(self) -> bool:
        if not self.selected_clips and not self.selected_markers:
            return False
        self.undo.push(RemoveItemsCmd(self, list(self.selected_markers), list(self.selected_clips)))
        return True

    def delete_items(self, marker_ids: list[str], clip_ids: list[str]) -> None:
        if marker_ids or clip_ids:
            self.undo.push(RemoveItemsCmd(self, marker_ids, clip_ids))

    def open_clip(self) -> Clip | None:
        return next((c for c in self.project.clips if c.out_frame is None), None)

    def selected_clip(self) -> Clip | None:
        if len(self.selected_clips) == 1:
            return self.project.clip_by_id(self.selected_clips[0])
        return None

    def add_clip(self, in_frame: int, out_frame: int | None) -> Clip:
        p = self.project
        c = Clip(in_frame=self.clamp_frame(in_frame), out_frame=out_frame, name=p.next_clip_name(),
                 color=p.next_clip_color())
        self.undo.push(AddClipCmd(self, c))
        return c

    def set_in(self, frame: int) -> Clip:
        """I key: move In of the open/selected clip, or start a new open clip."""
        frame = self.clamp_frame(frame)
        open_c = self.open_clip()
        if open_c is not None:
            self.set_clip_fields(open_c.id, {"in_frame": frame}, tr("Set In"))
            return open_c
        sel = self.selected_clip()
        if sel is not None and sel.out_frame is not None and frame < sel.out_frame:
            self.set_clip_fields(sel.id, {"in_frame": frame}, tr("Set In"))
            return sel
        return self.add_clip(frame, None)

    def set_out(self, frame: int) -> Clip | None:
        """O key: close the open clip (Out is inclusive of the current frame)."""
        out = min(self.clamp_frame(frame) + 1, self.frame_count)
        target = self.open_clip() or self.selected_clip()
        if target is None:
            return None
        if out <= target.in_frame:
            return None
        self.set_clip_fields(target.id, {"out_frame": out}, tr("Set Out"))
        self.select_clips([target.id])
        return target

    def set_clip_fields(self, cid: str, fields: dict, text: str = "Edit clip") -> None:
        c = self.project.clip_by_id(cid)
        if c is None:
            return
        if all(getattr(c, k) == v for k, v in fields.items()):
            return
        self.undo.push(SetClipFieldsCmd(self, cid, fields, text))

    def update_clips(self, cids: list[str], fields: dict, text: str = "Edit clips") -> None:
        cids = [c for c in cids if self.project.clip_by_id(c) is not None]
        if not cids:
            return
        if len(cids) == 1:
            self.set_clip_fields(cids[0], fields, text)
            return
        self.undo.beginMacro(tr(text))
        for cid in cids:
            self.set_clip_fields(cid, fields, text)
        self.undo.endMacro()

    def set_project_fields(self, fields: dict, text: str = "Project settings") -> None:
        if all(getattr(self.project, k) == v for k, v in fields.items()):
            return
        self.undo.push(SetProjectFieldsCmd(self, fields, text))

    def set_view(self, pps: float, scroll: float, text: str = "Zoom timeline") -> None:
        v = self.project.view
        if abs(v.pps - pps) < 1e-9 and abs(v.scroll - scroll) < 1e-9:
            return
        self.undo.push(SetViewCmd(self, pps, scroll, v.pps, v.scroll, text))

    # ---- selection (not undoable) ---------------------------------------------------------
    def select_clips(self, ids: list[str]) -> None:
        if ids != self.selected_clips or self.selected_markers:
            self.selected_clips = list(ids)
            self.selected_markers = []
            self.selection_changed.emit()

    def select_markers(self, ids: list[str]) -> None:
        if ids != self.selected_markers or self.selected_clips:
            self.selected_markers = list(ids)
            self.selected_clips = []
            self.selection_changed.emit()

    def clear_selection(self) -> None:
        if self.selected_clips or self.selected_markers:
            self.selected_clips, self.selected_markers = [], []
            self.selection_changed.emit()
