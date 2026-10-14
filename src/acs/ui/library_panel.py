"""Library dock: user folders scanned recursively, lazy ffprobe metadata, project badges."""

from __future__ import annotations

import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, Qt, QTimer, Signal
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QPushButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from acs.core import trace
from acs.core.atomic import read_json
from acs.core.library import FileMeta, FolderNode, MetaCache, scan_folder
from acs.core.project_io import find_autosave
from acs.core.util import fmt_duration
from acs.ui import theme
from acs.i18n import tr
from acs.ui.dispatch import MainThreadDispatcher

PATH_ROLE = Qt.ItemDataRole.UserRole
KIND_ROLE = Qt.ItemDataRole.UserRole + 1


def _autosave_clips(path: str) -> int | None:
    """Clip count of the video's autosave, None when it has none (safe on worker threads)."""
    auto = find_autosave(path)
    if auto is None:
        return None
    data = read_json(auto, {}) or {}
    return len(data.get("clips", []))


def _files(node: FolderNode):
    yield from node.files
    for sub in node.folders:
        yield from _files(sub)


class LibraryPanel(QWidget):
    open_requested = Signal(str)
    frames_requested = Signal(str)
    folders_changed = Signal(list)

    def __init__(self, settings, dispatch: MainThreadDispatcher | None = None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.meta_cache = MetaCache()
        # Worker results come back through a plain queue (see ui/dispatch.py), not Qt signals.
        self._dispatch = dispatch or MainThreadDispatcher(self)
        self._scan_gen = 0
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="meta")
        self._items: dict[str, QTreeWidgetItem] = {}
        self._current = ""
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(lambda _p: self._rescan_timer.start())
        self._rescan_timer = QTimer(self)
        self._rescan_timer.setSingleShot(True)
        self._rescan_timer.setInterval(1500)
        self._rescan_timer.timeout.connect(self.refresh)
        self._save_timer = QTimer(self)
        self._save_timer.setInterval(5000)
        self._save_timer.timeout.connect(self.meta_cache.save)
        self._save_timer.start()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        row = QHBoxLayout()
        add = QPushButton(tr("+ Add folder…"))
        add.setToolTip(tr("Add a folder with videos to the library (scanned with subfolders)"))
        self.remove = QPushButton(tr("Remove"))
        self.remove.setToolTip(tr("Remove the selected top-level folder from the library (files stay on disk)"))
        refresh = QPushButton("⟳")
        refresh.setFixedWidth(30)
        refresh.setToolTip(tr("Rescan folders"))
        row.addWidget(add)
        row.addWidget(self.remove)
        row.addStretch(1)
        row.addWidget(refresh)
        lay.addLayout(row)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr("Filter by name…"))
        self.search.setClearButtonEnabled(True)
        lay.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Name"), tr("Length"), tr("Video"), tr("Tracks"), tr("Project")])
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setMinimumSectionSize(30)
        for i, w in ((1, 62), (2, 118), (3, 58), (4, 36)):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
            self.tree.setColumnWidth(i, w)
        # Episode names matter most: technical columns are hidden by default (shown in the name tooltip,
        # can be enabled from the header's context menu).
        self.tree.setColumnHidden(2, True)
        self.tree.setColumnHidden(3, True)
        hdr.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        hdr.customContextMenuRequested.connect(self._header_menu)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.setMinimumWidth(340)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        lay.addWidget(self.tree, 1)
        self.empty = QLabel(tr("Add a folder with your episodes,\nor drag a video file into the window."))
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet(theme.css_dim())
        lay.addWidget(self.empty)

        add.clicked.connect(self._add_folder)
        self.remove.clicked.connect(self._remove_folder)
        refresh.clicked.connect(self.refresh)
        self.search.textChanged.connect(self._apply_filter)
        # itemActivated fires on double-click *and* Enter; also connecting itemDoubleClicked
        # opened the same episode twice per double-click.
        self.tree.itemActivated.connect(self._activated)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.refresh()

    # ---- folders ----
    def _add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, tr("Add folder"), self.settings.last_open_dir or "")
        if not d:
            return
        d = os.path.normpath(d)
        if d not in self.settings.library_folders:
            self.settings.library_folders.append(d)
            self.folders_changed.emit(self.settings.library_folders)
        self.refresh()

    def _remove_folder(self) -> None:
        it = self.tree.currentItem()
        while it is not None and it.parent() is not None:
            it = it.parent()
        if it is None:
            return
        root = it.data(0, PATH_ROLE)
        self.settings.library_folders = [f for f in self.settings.library_folders if f != root]
        self.folders_changed.emit(self.settings.library_folders)
        self.refresh()

    def refresh(self) -> None:
        self.tree.clear()
        self._items.clear()
        dirs = self._watcher.directories()
        if dirs:
            self._watcher.removePaths(dirs)
        folders = list(self.settings.library_folders)
        self.empty.setVisible(not folders)
        self.tree.setVisible(bool(folders))
        exts = list(self.settings.video_exts)
        self._scan_gen += 1
        gen = self._scan_gen
        post, on_scanned, meta_cache = self._dispatch.post, self._on_scanned, self.meta_cache
        for root in folders:
            item = QTreeWidgetItem([os.path.basename(root) or root, "", "", "", ""])
            item.setData(0, PATH_ROLE, root)
            item.setData(0, KIND_ROLE, "root")
            item.setToolTip(0, root)
            item.setText(1, tr("scanning…"))
            self.tree.addTopLevelItem(item)
            self._items["root:" + root] = item

            def work(r=root):
                trace.log("library", f"scan started {r!r}")
                node = scan_folder(r, exts)
                # Disk lookups per file (autosave badge, cached metadata) happen here, not on the GUI thread.
                info = {f: (_autosave_clips(f), meta_cache.get(f)) for f in _files(node)}
                trace.log("library", f"scan finished {r!r}: {node.count()} videos")
                post(on_scanned, r, node, gen, info)

            threading.Thread(target=work, daemon=True).start()

    def _on_scanned(self, root: str, node: FolderNode, gen: int, info: dict) -> None:
        if gen != self._scan_gen:
            return  # a newer refresh replaced the tree
        item = self._items.get("root:" + root)
        if item is None:
            return
        item.setText(1, "")
        if Path(root).is_dir():
            self._watcher.addPath(root)
        self.tree.setUpdatesEnabled(False)
        try:
            self._fill(item, node, info)
            item.setExpanded(True)
        finally:
            self.tree.setUpdatesEnabled(True)
        self._apply_filter(self.search.text())
        self.highlight(self._current)

    def _fill(self, parent: QTreeWidgetItem, node: FolderNode, info: dict) -> None:
        for sub in node.folders:
            it = QTreeWidgetItem([sub.name, "", "", "", ""])
            it.setData(0, PATH_ROLE, sub.path)
            it.setData(0, KIND_ROLE, "folder")
            it.setToolTip(0, sub.path)
            parent.addChild(it)
            if Path(sub.path).is_dir():
                self._watcher.addPath(sub.path)
            self._fill(it, sub, info)
        for f in node.files:
            it = QTreeWidgetItem([os.path.basename(f), "", "", "", ""])
            it.setData(0, PATH_ROLE, f)
            it.setData(0, KIND_ROLE, "file")
            it.setToolTip(0, f)
            parent.addChild(it)
            self._items[os.path.normcase(f)] = it
            clips, meta = info.get(f, (None, None))
            self._set_badge(it, clips)
            if meta is not None:
                self._show_meta(it, meta)
            else:
                self._pool.submit(self._fetch_meta, f)

    def _fetch_meta(self, path: str) -> None:
        meta = self.meta_cache.fetch(path, self.settings.ffprobe)
        self._dispatch.post(self._on_meta, path, meta)

    def _on_meta(self, path: str, meta: FileMeta) -> None:
        it = self._items.get(os.path.normcase(path))
        if it is not None:
            self._show_meta(it, meta)

    def _show_meta(self, it: QTreeWidgetItem, m: FileMeta) -> None:
        if m.error:
            it.setText(2, tr("error"))
            it.setToolTip(2, m.error)
            return
        it.setText(1, fmt_duration(m.duration))
        depth = " 10-bit" if m.bit_depth > 8 else ""
        video = f"{m.height}p {m.codec}{depth}" if m.height else m.codec
        tracks = f"🔊{m.audio} 💬{m.subs}"
        it.setText(2, video)
        it.setToolTip(2, f"{m.resolution()} {m.codec}{depth}")
        it.setText(3, tracks)
        it.setToolTip(3, tr("Audio tracks / subtitle tracks"))
        path = it.data(0, PATH_ROLE)
        it.setToolTip(0, f"{path}\n{m.resolution()} {m.codec}{depth} · {fmt_duration(m.duration)} · {tracks}")

    @staticmethod
    def _set_badge(it: QTreeWidgetItem, clips: int | None) -> None:
        if clips is None:
            it.setText(4, "")
            return
        it.setText(4, f"● {clips}")
        it.setToolTip(4, tr("Has saved work (autosave) with this many clips"))

    def update_badge(self, path: str, clips: int | None = None) -> None:
        """`clips` = number of clips just autosaved; None = read it from the autosave file."""
        it = self._items.get(os.path.normcase(path))
        if it is not None:
            self._set_badge(it, _autosave_clips(path) if clips is None else clips)

    def highlight(self, path: str) -> None:
        self._current = path
        for key, it in self._items.items():
            if key.startswith("root:"):
                continue
            f = it.font(0)
            f.setBold(key == os.path.normcase(path))
            it.setFont(0, f)
        it = self._items.get(os.path.normcase(path)) if path else None
        if it is not None:
            self.tree.scrollToItem(it)

    def _apply_filter(self, text: str) -> None:
        text = text.strip().casefold()

        def visit(item: QTreeWidgetItem) -> bool:
            kind = item.data(0, KIND_ROLE)
            if kind == "file":
                ok = not text or text in item.text(0).casefold()
                item.setHidden(not ok)
                return ok
            any_child = False
            for i in range(item.childCount()):
                any_child = visit(item.child(i)) or any_child
            item.setHidden(bool(text) and not any_child and kind != "root")
            return any_child

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))

    def _activated(self, item: QTreeWidgetItem, _col: int = 0) -> None:
        if item.data(0, KIND_ROLE) == "file":
            self.open_requested.emit(item.data(0, PATH_ROLE))

    def _menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        path = item.data(0, PATH_ROLE)
        kind = item.data(0, KIND_ROLE)
        menu = QMenu(self)
        if kind == "file":
            menu.addAction(tr("Open"), lambda: self.open_requested.emit(path))
            menu.addAction(tr("Extract frames…"), lambda: self.frames_requested.emit(path))
        menu.addAction(tr("Show in Explorer"), lambda: self._reveal(path))
        if kind == "root":
            menu.addSeparator()
            menu.addAction(tr("Remove folder from library"), self._remove_folder)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _header_menu(self, pos) -> None:
        menu = QMenu(self)
        for col in (1, 2, 3, 4):
            a = menu.addAction(self.tree.headerItem().text(col))
            a.setCheckable(True)
            a.setChecked(not self.tree.isColumnHidden(col))
            a.toggled.connect(lambda on, c=col: self.tree.setColumnHidden(c, not on))
        menu.exec(self.tree.header().mapToGlobal(pos))

    @staticmethod
    def _reveal(path: str) -> None:
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            subprocess.Popen(["explorer", os.path.normpath(path)])

    def shutdown(self) -> None:
        self.meta_cache.save()
        self._pool.shutdown(wait=False, cancel_futures=True)
