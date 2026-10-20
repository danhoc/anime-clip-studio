"""Library dock: user folders scanned recursively, lazy ffprobe metadata, project badges.

Episode names wrap onto up to three lines in the tree (the rest elided in the middle, so the episode
number and extension at the end stay visible); the selected file is also shown in full in a details
strip under it, and hovering a name shows a tooltip with the full name, folder and metadata.
"""

from __future__ import annotations

import html
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QFontMetrics, QPalette, QTextLayout, QTextOption
from PySide6.QtWidgets import (QApplication, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMenu, QPushButton, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
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
META_ROLE = Qt.ItemDataRole.UserRole + 2     # FileMeta once probed
CLIPS_ROLE = Qt.ItemDataRole.UserRole + 3    # clip count of the autosave, None without one
NAME_LINES = 3                               # file names wrap onto at most this many lines
_BREAK_AFTER = set("._-)]}\\/, ")


def _autosave_clips(path: str) -> int | None:
    """Clip count of the video's autosave, None when it has none (safe on worker threads)."""
    auto = find_autosave(path)
    if auto is None:
        return None
    data = read_json(auto, {}) or {}
    return len(data.get("clips", []))


def _breakable(text: str) -> str:
    """Zero-width break points after separators, so a QLabel can wrap names without spaces."""
    return "".join(ch + "\u200b" if ch in _BREAK_AFTER else ch for ch in text)


def wrap_name(text: str, font: QFont, width: int, max_lines: int = NAME_LINES) -> list[str]:
    """`text` wrapped at word boundaries and separators into at most `max_lines` lines of `width` px;
    what does not fit is elided in the middle of the last line (the end of a name tells episodes apart)."""
    if width <= 0:
        return [text]
    full = _breakable(text)
    layout = QTextLayout(full, font)
    opt = QTextOption()
    opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(opt)
    spans = []
    layout.beginLayout()
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        spans.append((line.textStart(), line.textLength()))
    layout.endLayout()
    lines = [full[a:a + n] for a, n in spans[:max_lines]]
    if len(spans) > max_lines:
        rest = full[spans[max_lines - 1][0]:].replace("​", "")
        lines[-1] = QFontMetrics(font).elidedText(rest, Qt.TextElideMode.ElideMiddle, width)
    return [ln.replace("​", "") for ln in lines]


class _NameDelegate(QStyledItemDelegate):
    """Name column: files wrap onto up to NAME_LINES lines instead of being cut to one."""

    def __init__(self, tree: QTreeWidget):
        super().__init__(tree)
        self._tree = tree
        # Row heights are measured at `_hint_col` (the column width when they were last laid out)
        # until relayout() runs after a resize: while a splitter is dragged the tree asks for
        # hundreds of size hints per step, and wrapping every name each time made the drag stutter.
        self._hint_col: int | None = None
        self._hints: dict[tuple, QSize] = {}

    @staticmethod
    def _depth(index) -> int:
        depth = 1   # top-level items are indented too (decorated root)
        p = index.parent()
        while p.isValid():
            depth += 1
            p = p.parent()
        return depth

    def _text_width(self, index, col_width: int | None = None) -> int:
        margin = self._tree.style().pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin) + 1
        col = self._tree.columnWidth(0) if col_width is None else col_width
        return col - self._depth(index) * self._tree.indentation() - 2 * margin

    def _lines(self, opt: QStyleOptionViewItem, index, col_width: int | None = None,
               max_lines: int = NAME_LINES) -> list[str] | None:
        if index.column() != 0 or index.data(KIND_ROLE) != "file":
            return None
        return wrap_name(opt.text, opt.font, self._text_width(index, col_width), max_lines)

    def column_resized(self, old: int) -> None:
        """Column 0 changed width: keep the heights measured at the old width until relayout()."""
        if self._hint_col is None:
            self._hint_col = old

    def relayout(self) -> None:
        self._hint_col = None
        self._hints.clear()
        self._tree.doItemsLayout()

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt API
        col = self._hint_col if self._hint_col is not None else self._tree.columnWidth(0)
        font = index.data(Qt.ItemDataRole.FontRole)   # the opened episode is bold (wider)
        key = (index.data(), index.data(KIND_ROLE), font.key() if font is not None else "",
               self._depth(index), col)
        size = self._hints.get(key)
        if size is not None:
            return size
        size = super().sizeHint(option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        lines = self._lines(opt, index, col)
        if lines and len(lines) > 1:
            size = QSize(size.width(), size.height() + (len(lines) - 1) * QFontMetrics(opt.font).lineSpacing())
        if len(self._hints) > 5000:
            self._hints.clear()
        self._hints[key] = size
        return size

    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        # the current width, but no more lines than the row (possibly measured at the old width) holds
        fits = max(1, opt.rect.height() // QFontMetrics(opt.font).lineSpacing())
        lines = self._lines(opt, index, max_lines=min(NAME_LINES, fits))
        if lines is None:
            super().paint(painter, option, index)
            return
        opt.text = ""   # background, selection and focus come from the style, the text is drawn here
        style = opt.widget.style() if opt.widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, opt.widget)
        step = QFontMetrics(opt.font).lineSpacing()
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        painter.save()
        painter.setFont(opt.font)
        painter.setPen(opt.palette.color(QPalette.ColorRole.HighlightedText if selected
                                         else QPalette.ColorRole.Text))
        y = rect.top() + (rect.height() - len(lines) * step) / 2
        for ln in lines:
            painter.drawText(QRectF(rect.left(), y, rect.width(), step),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), ln)
            y += step
        painter.restore()


def _meta_text(m: FileMeta | None) -> str:
    if m is None:
        return ""
    if m.error:
        return tr("error")
    depth = " 10-bit" if m.bit_depth > 8 else ""
    parts = [fmt_duration(m.duration), f"{m.resolution()} {m.codec}{depth}".strip(), f"🔊{m.audio} 💬{m.subs}"]
    return " · ".join(p for p in parts if p)


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
        self._selected = ""     # file picked with one click
        self._reselect = ""     # ... picked again once a rescan has rebuilt the tree
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
        self._names = _NameDelegate(self.tree)
        self.tree.setItemDelegateForColumn(0, self._names)
        self.tree.setAlternatingRowColors(True)   # tells neighbouring multi-line names apart
        # wrapped names change their row height with the column width: re-measure rows after a resize
        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.setInterval(60)
        self._relayout_timer.timeout.connect(self._names.relayout)

        def resized(i: int, old: int, _new: int) -> None:
            if i == 0:
                self._names.column_resized(old)
                self._relayout_timer.start()
        hdr.sectionResized.connect(resized)
        self.setMinimumWidth(280)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        lay.addWidget(self.tree, 1)
        lay.addWidget(self._build_details())
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
        self.tree.currentItemChanged.connect(lambda cur, _prev: self._on_current(cur))
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
        self._reselect = self._selected
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
        it = self._items.get(os.path.normcase(self._reselect)) if self._reselect else None
        if it is not None:
            self._reselect = ""
            self.tree.setCurrentItem(it)

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
            parent.addChild(it)
            self._items[os.path.normcase(f)] = it
            clips, meta = info.get(f, (None, None))
            self._set_badge(it, clips)
            if meta is not None:
                self._show_meta(it, meta)
            else:
                self._file_tooltip(it)
                self._pool.submit(self._fetch_meta, f)

    def _fetch_meta(self, path: str) -> None:
        meta = self.meta_cache.fetch(path, self.settings.ffprobe)
        self._dispatch.post(self._on_meta, path, meta)

    def _on_meta(self, path: str, meta: FileMeta) -> None:
        it = self._items.get(os.path.normcase(path))
        if it is not None:
            self._show_meta(it, meta)
            if it is self.tree.currentItem():
                self._show_details(it)

    def _show_meta(self, it: QTreeWidgetItem, m: FileMeta) -> None:
        it.setData(0, META_ROLE, m)
        self._file_tooltip(it)
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

    @staticmethod
    def _file_tooltip(it: QTreeWidgetItem) -> None:
        """Full name first (the tree elides it), then the folder and what is known about the file."""
        path = it.data(0, PATH_ROLE)
        lines = [f"<b>{html.escape(os.path.basename(path))}</b>", html.escape(os.path.dirname(path))]
        meta = _meta_text(it.data(0, META_ROLE))
        if meta:
            lines.append(html.escape(meta))
        it.setToolTip(0, "<p style='white-space:pre'>" + "<br>".join(lines) + "</p>")

    @staticmethod
    def _set_badge(it: QTreeWidgetItem, clips: int | None) -> None:
        it.setData(0, CLIPS_ROLE, clips)
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
            if it is self.tree.currentItem():
                self._show_details(it)

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
        self._on_current(self.tree.currentItem())

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
            menu.addAction(tr("Copy file name"), lambda: QApplication.clipboard().setText(os.path.basename(path)))
        menu.addAction(tr("Show in Explorer"), lambda: self._reveal(path))
        if kind == "root":
            menu.addSeparator()
            menu.addAction(tr("Remove folder from library"), self._remove_folder)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ---- details of the selected file ----
    def _build_details(self) -> QFrame:
        box = QFrame()
        box.setObjectName("libdetails")
        t = theme.current()
        box.setStyleSheet(f"#libdetails {{ background: {t.alt_base}; border: 1px solid {t.border}; "
                          f"border-radius: 4px; }}")
        v = QVBoxLayout(box)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(2)
        self.d_name = QLabel()
        f = self.d_name.font()
        f.setBold(True)
        self.d_name.setFont(f)
        self.d_folder = QLabel()
        self.d_folder.setStyleSheet(theme.css_dim())
        self.d_meta = QLabel()
        self.d_meta.setStyleSheet(theme.css_info())
        for w in (self.d_name, self.d_folder, self.d_meta):
            w.setTextFormat(Qt.TextFormat.PlainText)
            w.setWordWrap(True)
            v.addWidget(w)
        row = QHBoxLayout()
        row.setContentsMargins(0, 2, 0, 0)
        self.d_hint = QLabel()
        self.d_hint.setStyleSheet(theme.css_dim())
        self.d_open = QPushButton(tr("Open"))
        self.d_open.clicked.connect(lambda: self._selected and self.open_requested.emit(self._selected))
        row.addWidget(self.d_hint, 1)
        row.addWidget(self.d_open)
        v.addLayout(row)
        box.hide()
        self.details = box
        return box

    def _on_current(self, it: QTreeWidgetItem | None) -> None:
        if it is not None and it.data(0, KIND_ROLE) == "file":
            self._selected = it.data(0, PATH_ROLE)
            self._reselect = ""
            self._show_details(it)
        else:
            self._selected = ""
            self.details.hide()

    def _show_details(self, it: QTreeWidgetItem) -> None:
        path = it.data(0, PATH_ROLE)
        self.d_name.setText(_breakable(os.path.basename(path)))
        self.d_folder.setText(_breakable(os.path.dirname(path)))
        meta = it.data(0, META_ROLE)
        parts = [_meta_text(meta) if meta is not None else tr("reading info…")]
        clips = it.data(0, CLIPS_ROLE)
        if clips is not None:
            parts.append(tr("saved work: {n} clips").format(n=clips))
        self.d_meta.setText(" · ".join(p for p in parts if p))
        self.d_meta.setToolTip(meta.error if meta is not None and meta.error else "")
        is_open = bool(self._current) and os.path.normcase(path) == os.path.normcase(self._current)
        self.d_hint.setText(tr("Open now") if is_open else tr("Double-click or Enter also opens it"))
        self.d_open.setVisible(not is_open)
        self.details.show()

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
