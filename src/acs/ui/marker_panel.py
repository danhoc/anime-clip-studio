"""Markers tab next to the player: every marker with its time and name, for navigation and editing.

The timeline shows a marker's name only while the mouse is over it (or the marker is selected), so the
full list lives here: click a row to go to the marker, double-click the name to rename it, untick it
to hide it from the timeline. Selection is shared with the timeline through the document."""

from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QColorDialog, QHBoxLayout, QHeaderView, QLabel, QMenu,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from acs.core.document import ProjectDocument
from acs.core.model import MARKER_COLOR_NAMES, MARKER_COLORS
from acs.core.util import fmt_time
from acs.i18n import tr
from acs.ui import theme
from acs.ui.hotkeys import key_for, with_key

COL_SHOW, COL_TIME, COL_NAME = range(3)


def swatch(color: str, size: int = 12) -> QIcon:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)
    p.end()
    return QIcon(pm)


class MarkerPanel(QWidget):
    seek_requested = Signal(float)
    add_requested = Signal()           # add a marker at the playhead (the main window knows where it is)

    def __init__(self, doc: ProjectDocument, parent=None):
        super().__init__(parent)
        self.doc = doc
        self._updating = False
        self._ids: list[str] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self.b_add = QPushButton("+ " + tr("Add"))
        self.b_add.setToolTip(with_key("Add marker at the playhead", "marker"))
        self.b_delete = QPushButton(tr("Delete"))
        self.b_delete.setToolTip(with_key("Delete selected markers", "delete"))
        self.b_toggle = QPushButton()
        for b in (self.b_add, self.b_delete, self.b_toggle):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            bar.addWidget(b)
        bar.addStretch(1)
        root.addLayout(bar)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["", tr("Time"), tr("Name")])
        self.table.horizontalHeaderItem(COL_SHOW).setToolTip(tr("Shown on the timeline"))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked
                                   | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        h = self.table.horizontalHeader()
        # fitted once per rebuild in refresh(): ResizeToContents re-measured every row after each cell
        h.setSectionResizeMode(COL_SHOW, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(COL_TIME, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)
        self.empty = QLabel(tr("No markers yet. Press {key} to add one at the playhead.").format(
            key=key_for("marker")))
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(theme.css_dim())
        root.addWidget(self.empty, 1)

        self.b_add.clicked.connect(self.add_requested)
        self.b_delete.clicked.connect(self._delete_selected)
        self.b_toggle.clicked.connect(self._toggle_all)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.itemClicked.connect(self._on_clicked)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.customContextMenuRequested.connect(self._context_menu)
        # One rebuild per pass of the event loop: an import from mpv (or undoing it) changes hundreds
        # of markers one by one, and rebuilding the table after each made that quadratic.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(0)
        self._refresh_timer.timeout.connect(self.refresh)
        doc.markers_changed.connect(self._refresh_timer.start)
        doc.selection_changed.connect(self._sync_selection)
        self.refresh()

    # ---- model -> view -------------------------------------------------------------------
    def refresh(self) -> None:
        self._refresh_timer.stop()
        markers = self.doc.project.markers
        self._updating = True
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(markers))
            self._ids = [m.id for m in markers]
            dim = QColor(theme.current().dim)
            for row, m in enumerate(markers):
                show = QTableWidgetItem()
                show.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                              | Qt.ItemFlag.ItemIsUserCheckable)
                show.setCheckState(Qt.CheckState.Unchecked if m.hidden else Qt.CheckState.Checked)
                show.setIcon(swatch(m.color))
                show.setToolTip(tr("Shown on the timeline"))
                t = self.doc.time_of(m.frame) if self.doc.has_video else m.time
                time_item = QTableWidgetItem(fmt_time(t))
                time_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                name = QTableWidgetItem(m.label)
                name.setToolTip(tr("Double-click to rename"))
                if m.hidden:
                    time_item.setForeground(dim)
                    name.setForeground(dim)
                self.table.setItem(row, COL_SHOW, show)
                self.table.setItem(row, COL_TIME, time_item)
                self.table.setItem(row, COL_NAME, name)
            for col in (COL_SHOW, COL_TIME):
                self.table.resizeColumnToContents(col)
        finally:
            self.table.setUpdatesEnabled(True)
            self._updating = False
        has = bool(markers)
        self.table.setVisible(has)
        self.empty.setVisible(not has)
        self.b_delete.setEnabled(has)
        self.b_toggle.setEnabled(has)
        any_shown = any(not m.hidden for m in markers)
        self.b_toggle.setText(tr("Hide all") if any_shown or not has else tr("Show all"))
        self.b_add.setEnabled(self.doc.has_video)
        self._sync_selection()

    def _sync_selection(self) -> None:
        if self._refresh_timer.isActive():
            return   # the rows are about to be rebuilt (and selected) by refresh()
        sel = set(self.doc.selected_markers)
        self._updating = True
        try:
            sm = self.table.selectionModel()
            sm.clearSelection()
            first = None
            for row, mid in enumerate(self._ids):
                if mid in sel:
                    sm.select(self.table.model().index(row, 0), QItemSelectionModel.SelectionFlag.Select
                              | QItemSelectionModel.SelectionFlag.Rows)
                    first = row if first is None else first
            if first is not None:
                self.table.scrollToItem(self.table.item(first, COL_TIME))
        finally:
            self._updating = False

    # ---- view -> model -------------------------------------------------------------------
    def _selected_ids(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        return [self._ids[r] for r in rows if r < len(self._ids)]

    def _on_selection(self) -> None:
        if not self._updating:
            self.doc.select_markers(self._selected_ids())

    def _on_clicked(self, item: QTableWidgetItem) -> None:
        if item.column() == COL_SHOW or item.row() >= len(self._ids):
            return   # the tick box only toggles visibility
        m = self.doc.project.marker_by_id(self._ids[item.row()])
        if m is not None:
            self.seek_requested.emit(self.doc.time_of(m.frame))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or item.row() >= len(self._ids):
            return
        mid = self._ids[item.row()]
        m = self.doc.project.marker_by_id(mid)
        if m is None:
            return
        # The change rebuilds the table, so it is applied after this signal (the item that sent it
        # must not be deleted under it).
        if item.column() == COL_SHOW:
            hidden = item.checkState() != Qt.CheckState.Checked
            # ticking one of several selected rows applies to all of them
            ids = self._selected_ids()
            ids = ids if mid in ids else [mid]
            QTimer.singleShot(0, lambda: self.doc.set_markers_hidden(ids, hidden))
        elif item.column() == COL_NAME:
            text = item.text().strip()
            if text != m.label:
                QTimer.singleShot(0, lambda: self.doc.set_marker_fields(mid, {"label": text}, tr("Rename marker")))

    def _delete_selected(self) -> None:
        ids = self._selected_ids()
        if ids:
            self.doc.delete_items(ids, [])

    def _toggle_all(self) -> None:
        markers = self.doc.project.markers
        hide = any(not m.hidden for m in markers)
        self.doc.set_markers_hidden([m.id for m in markers], hide)

    def _context_menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None or item.row() >= len(self._ids):
            return
        mid = self._ids[item.row()]
        if mid not in self.doc.selected_markers:
            self.doc.select_markers([mid])
        ids = self._selected_ids() or [mid]
        m = self.doc.project.marker_by_id(mid)
        menu = QMenu(self)
        menu.addAction(tr("Go to marker"), lambda: self.seek_requested.emit(self.doc.time_of(m.frame)))
        menu.addAction(tr("Rename marker…"), lambda: self.table.editItem(self.table.item(item.row(), COL_NAME)))
        colors = menu.addMenu(tr("Marker colour"))
        for name, value in zip(MARKER_COLOR_NAMES, MARKER_COLORS):
            a = colors.addAction(swatch(value, 14), tr(name))
            a.setCheckable(True)
            a.setChecked(value.lower() == (m.color or "").lower())
            a.triggered.connect(lambda _c=False, v=value: self._set_color(ids, v))
        colors.addSeparator()
        colors.addAction(tr("Other colour…"), lambda: self._pick_color(ids, m.color))
        hidden = all(self.doc.project.marker_by_id(i).hidden for i in ids)
        menu.addAction(tr("Show on the timeline") if hidden else tr("Hide from the timeline"),
                       lambda: self.doc.set_markers_hidden(ids, not hidden))
        menu.addSeparator()
        menu.addAction(tr("Delete marker"), lambda: self.doc.delete_items(ids, []))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _set_color(self, ids: list[str], color: str) -> None:
        self.doc.undo.beginMacro(tr("Marker colour"))
        for mid in ids:
            self.doc.set_marker_fields(mid, {"color": color}, tr("Marker colour"))
        self.doc.undo.endMacro()

    def _pick_color(self, ids: list[str], current: str) -> None:
        c = QColorDialog.getColor(QColor(current or MARKER_COLORS[0]), self, tr("Marker colour"))
        if c.isValid():
            self._set_color(ids, c.name())
