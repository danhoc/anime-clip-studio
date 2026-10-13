"""Collect every UI string that goes through tr(): literals passed to tr()/with_key() plus the
English tables that are translated at runtime (texts, hotkeys, tour, presets, codec help)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "acs"


def literal_strings() -> set[str]:
    found: set[str] = set()
    for py in SRC.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                pos = {"tr": 0, "with_key": 0, "tb": 1, "_act": 2}.get(name)
                if pos is not None and len(node.args) > pos:
                    a = node.args[pos]
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        found.add(a.value)
    return found


def table_strings() -> set[str]:
    from acs.core.presets import builtin_presets
    from acs.ui import texts
    from acs.ui.help import TOUR_STEPS
    from acs.ui.hotkeys import HOTKEYS, MOUSE_HINTS
    from acs.ui.preset_editor import CODEC_HELP
    from acs.ui.recovery_dialog import REASONS

    from acs.core.model import MARKER_COLOR_NAMES
    from acs.core.tools import OFFICIAL_LINKS

    s: set[str] = set(MARKER_COLOR_NAMES) | {"Edit marker"}
    for links in OFFICIAL_LINKS.values():
        s.update(text for text, _url in links)
    s.update(["file not found", "not found", "not a valid DLL", "not a 64-bit DLL (the app is 64-bit)"])
    for d in (texts.REASONS, texts.WARNINGS, texts.MODES, texts.STATUS, CODEC_HELP, REASONS):
        s.update(d.values())
    for _a, _k, text, group in HOTKEYS:
        s.update((text, group))
    for k, t in MOUSE_HINTS:
        s.update((k, t))
    for _k, title, text in TOUR_STEPS:
        s.update((title, text))
    s.update(p.name for p in builtin_presets())
    s.update(["Mouse", "No crop", "Auto (detect black bars)", "Manual", "My video preset", "My GIF preset",
              "My WebP preset", "PNG (lossless)", "PNG 16-bit (keeps 10-bit detail)", "JPEG", "WebP",
              "Height", "Frame rate", "Colours", "Dithering", "WebP quality",
              # default undo-command texts (commands.py / document.py)
              "Add clip", "Add marker", "Delete", "Move marker", "Edit clip", "Edit clips", "Project settings",
              "Zoom timeline", "Set In", "Set Out"])
    s.update(["#", "Name", "In", "Out", "Duration", "Audio", "Subs", "Size", "Preset", "Mode"])
    return s


def all_strings() -> set[str]:
    return {x for x in literal_strings() | table_strings() if x.strip() and any(c.isalpha() for c in x)}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(SRC.parent))
    for x in sorted(all_strings()):
        print(repr(x))
