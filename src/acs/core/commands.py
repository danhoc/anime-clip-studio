"""QUndoCommand implementations. Every model mutation goes through one of these."""

from __future__ import annotations

import copy
import time
from typing import TYPE_CHECKING

from PySide6.QtGui import QUndoCommand

from acs.core.model import Clip, Marker
from acs.i18n import tr

if TYPE_CHECKING:
    from acs.core.document import ProjectDocument

VIEW_MERGE_ID = 1001
VIEW_MERGE_WINDOW = 1.2  # seconds: consecutive zoom/scroll steps collapse into one undo entry


class AddMarkerCmd(QUndoCommand):
    def __init__(self, doc: "ProjectDocument", marker: Marker, text: str = "Add marker"):
        super().__init__(tr(text))
        self.doc, self.marker = doc, marker

    def redo(self):
        self.doc._insert_marker(copy.deepcopy(self.marker))

    def undo(self):
        self.doc._delete_marker(self.marker.id)


class RemoveItemsCmd(QUndoCommand):
    def __init__(self, doc: "ProjectDocument", marker_ids: list[str], clip_ids: list[str], text: str = "Delete"):
        super().__init__(tr(text))
        self.doc = doc
        p = doc.project
        self.markers = [(i, copy.deepcopy(m)) for i, m in enumerate(p.markers) if m.id in marker_ids]
        self.clips = [(i, copy.deepcopy(c)) for i, c in enumerate(p.clips) if c.id in clip_ids]

    def redo(self):
        for _, m in self.markers:
            self.doc._delete_marker(m.id)
        for _, c in self.clips:
            self.doc._delete_clip(c.id)

    def undo(self):
        for i, m in self.markers:
            self.doc._insert_marker(copy.deepcopy(m), i)
        for i, c in self.clips:
            self.doc._insert_clip(copy.deepcopy(c), i)


class MoveMarkerCmd(QUndoCommand):
    def __init__(self, doc: "ProjectDocument", marker_id: str, frame: int, old_frame: int, text: str = "Move marker"):
        super().__init__(tr(text))
        self.doc, self.mid, self.frame, self.old = doc, marker_id, frame, old_frame

    def redo(self):
        self.doc._move_marker(self.mid, self.frame)

    def undo(self):
        self.doc._move_marker(self.mid, self.old)


class SetMarkerFieldsCmd(QUndoCommand):
    """Rename / recolour a marker."""

    def __init__(self, doc: "ProjectDocument", marker_id: str, new: dict, text: str = "Edit marker"):
        super().__init__(tr(text))
        self.doc, self.mid = doc, marker_id
        m = doc.project.marker_by_id(marker_id)
        self.new = dict(new)
        self.old = {k: getattr(m, k) for k in new} if m else {}

    def redo(self):
        self.doc._set_marker_fields(self.mid, self.new)

    def undo(self):
        self.doc._set_marker_fields(self.mid, self.old)


class AddClipCmd(QUndoCommand):
    def __init__(self, doc: "ProjectDocument", clip: Clip, text: str = "Add clip"):
        super().__init__(tr(text))
        self.doc, self.clip = doc, clip

    def redo(self):
        self.doc._insert_clip(copy.deepcopy(self.clip))
        self.doc.select_clips([self.clip.id])

    def undo(self):
        self.doc._delete_clip(self.clip.id)


class SetClipFieldsCmd(QUndoCommand):
    def __init__(self, doc: "ProjectDocument", clip_id: str, new: dict, text: str = "Edit clip"):
        super().__init__(tr(text))
        self.doc, self.cid = doc, clip_id
        clip = doc.project.clip_by_id(clip_id)
        self.new = copy.deepcopy(new)
        self.old = {k: copy.deepcopy(getattr(clip, k)) for k in new} if clip else {}

    def redo(self):
        self.doc._set_clip_fields(self.cid, copy.deepcopy(self.new))

    def undo(self):
        self.doc._set_clip_fields(self.cid, copy.deepcopy(self.old))


class SetProjectFieldsCmd(QUndoCommand):
    def __init__(self, doc: "ProjectDocument", new: dict, text: str = "Project settings"):
        super().__init__(tr(text))
        self.doc = doc
        self.new = dict(new)
        self.old = {k: getattr(doc.project, k) for k in new}

    def redo(self):
        self.doc._set_project_fields(self.new)

    def undo(self):
        self.doc._set_project_fields(self.old)


class SetViewCmd(QUndoCommand):
    """Timeline zoom / scroll. Mergeable so a burst of wheel steps is one undo entry."""

    def __init__(self, doc: "ProjectDocument", pps: float, scroll: float, old_pps: float, old_scroll: float,
                 text: str = "Zoom timeline"):
        super().__init__(tr(text))
        self.doc = doc
        self.pps, self.scroll = pps, scroll
        self.old_pps, self.old_scroll = old_pps, old_scroll
        self.stamp = time.monotonic()

    def id(self) -> int:
        return VIEW_MERGE_ID

    def mergeWith(self, other: QUndoCommand) -> bool:
        if not isinstance(other, SetViewCmd) or other.stamp - self.stamp > VIEW_MERGE_WINDOW:
            return False
        self.pps, self.scroll, self.stamp = other.pps, other.scroll, other.stamp
        return True

    def redo(self):
        self.doc._set_view(self.pps, self.scroll)

    def undo(self):
        self.doc._set_view(self.old_pps, self.old_scroll)
