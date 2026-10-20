"""ProjectDocument: the live project (model + media info + frame index + undo stack + selection)."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QUndoStack

from acs.core.commands import (VIEW_MERGE_ID, AddClipCmd, AddMarkerCmd, MoveMarkerCmd, RemoveItemsCmd,
                               SetClipFieldsCmd, SetMarkerFieldsCmd, SetProjectFieldsCmd, SetViewCmd)
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
    edge_edit_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = Project()
        self.info: MediaInfo | None = None
        self.index: FrameIndex | None = None
        self.project_path: str | None = None
        self.undo = QUndoStack(self)
        self._dirty = False
        self.undo.cleanChanged.connect(self._update_dirty)
        self.undo.indexChanged.connect(self._update_dirty)
        self.selected_clips: list[str] = []
        self.selected_markers: list[str] = []
        # (clip id, "in" | "out"): that edge follows the playhead until I / O sets it (not undoable)
        self.edge_edit: tuple[str, str] | None = None

    def _compute_dirty(self) -> bool:
        """Unsaved edits: a command other than a zoom / scroll step lies between the saved state and
        now (view steps stay undoable but do not make the project "changed")."""
        u = self.undo
        clean = u.cleanIndex()
        if clean < 0:
            return True
        lo, hi = sorted((clean, u.index()))
        return any(u.command(i).id() != VIEW_MERGE_ID for i in range(lo, hi))

    def _update_dirty(self, *_args) -> None:
        dirty = self._compute_dirty()
        if dirty != self._dirty:
            self._dirty = dirty
            self.dirty_changed.emit(dirty)

    # ---- state ----------------------------------------------------------------------------
    @property
    def has_video(self) -> bool:
        return self.info is not None and self.index is not None

    @property
    def dirty(self) -> bool:
        return self._compute_dirty()

    def load(self, project: Project, info: MediaInfo | None, index: FrameIndex | None,
             project_path: str | None) -> None:
        self.project = project
        self.info = info
        self.index = index
        self.project_path = project_path
        self.undo.clear()
        self.selected_clips, self.selected_markers = [], []
        self.edge_edit = None
        self.loaded.emit()
        self.edge_edit_changed.emit()
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
                c.out_frame = max(c.in_frame + 1, index.nearest_boundary(c.out_time))
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
        if self.edge_edit is not None and self.edge_edit[0] == cid:
            self.cancel_edge_edit()
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

    def set_markers_hidden(self, ids: list[str], hidden: bool) -> None:
        """Hide / show several markers as one undo step."""
        ids = [i for i in ids if (m := self.project.marker_by_id(i)) is not None and m.hidden != hidden]
        if not ids:
            return
        text = tr("Hide markers") if hidden else tr("Show markers")
        self.undo.beginMacro(text)
        for mid in ids:
            self.undo.push(SetMarkerFieldsCmd(self, mid, {"hidden": hidden}, text))
        self.undo.endMacro()

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

    def set_in(self, frame: int) -> Clip | None:
        """I key: move In of the open clip, or start a new open clip. A selected finished clip is left
        alone (its edges change by dragging or with start_edge_edit).

        While the In edge of a clip is being changed, sets that In instead (None when it would not be
        before the clip's Out; the edit then stays active)."""
        frame = self.clamp_frame(frame)
        edited = self._edge_edit_clip("in")
        if edited is not None:
            if edited.out_frame is not None and frame >= edited.out_frame:
                return None
            self.cancel_edge_edit()
            self.set_clip_fields(edited.id, {"in_frame": frame}, tr("Set In"))
            self.select_clips([edited.id])
            return edited
        open_c = self.open_clip()
        if open_c is not None:
            self.set_clip_fields(open_c.id, {"in_frame": frame}, tr("Set In"))
            return open_c
        return self.add_clip(frame, None)

    def set_out(self, frame: int) -> Clip | None:
        """O key: close the open clip (Out is inclusive of the current frame); None without one."""
        out = min(self.clamp_frame(frame) + 1, self.frame_count)
        edited = self._edge_edit_clip("out")
        if edited is not None:
            if out <= edited.in_frame:
                return None
            self.cancel_edge_edit()
            self.set_clip_fields(edited.id, {"out_frame": out}, tr("Set Out"))
            self.select_clips([edited.id])
            return edited
        target = self.open_clip()
        if target is None:
            return None
        if out <= target.in_frame:
            return None
        self.set_clip_fields(target.id, {"out_frame": out}, tr("Set Out"))
        self.select_clips([target.id])
        return target

    # ---- changing an edge of an existing clip by moving the playhead ------------------------
    def start_edge_edit(self, cid: str, edge: str) -> bool:
        """Make the In ("in") or Out ("out") edge of a closed clip follow the playhead, like the Out
        of a clip that was just started; set_in / set_out then fix the new position."""
        c = self.project.clip_by_id(cid)
        if c is None or c.out_frame is None or edge not in ("in", "out"):
            return False
        self.edge_edit = (cid, edge)
        self.select_clips([cid])
        self.edge_edit_changed.emit()
        return True

    def cancel_edge_edit(self) -> None:
        if self.edge_edit is not None:
            self.edge_edit = None
            self.edge_edit_changed.emit()

    def _edge_edit_clip(self, edge: str) -> Clip | None:
        """The clip whose `edge` is being changed. Pressing the other edge's key ends the edit and
        then works as usual."""
        if self.edge_edit is None:
            return None
        cid, e = self.edge_edit
        c = self.project.clip_by_id(cid)
        if c is None or e != edge:
            self.cancel_edge_edit()
            return None
        return c

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
