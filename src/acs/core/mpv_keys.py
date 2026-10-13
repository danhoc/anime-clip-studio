"""Key bindings of the mpv marker script: defaults, Qt -> mpv key names, conflict detection."""

from __future__ import annotations

import re
from dataclasses import dataclass

# action -> (script option, default key, English description)
ACTIONS: dict[str, tuple[str, str, str]] = {
    "marker": ("key_marker", "Alt+m", "Marker"),
    "in": ("key_clip_start", "Alt+i", "Clip start"),
    "out": ("key_clip_end", "Alt+o", "Clip end"),
    "undo": ("key_undo", "Alt+u", "Undo the last mark"),
    "list": ("key_list", "Alt+l", "List marks"),
}
SCRIPT_NAME = "acs_markers"

_QT_TO_MPV = {
    "backspace": "BS", "return": "ENTER", "enter": "KP_ENTER", "del": "DEL", "delete": "DEL", "ins": "INS",
    "insert": "INS", "space": "SPACE", "esc": "ESC", "escape": "ESC", "tab": "TAB", "left": "LEFT",
    "right": "RIGHT", "up": "UP", "down": "DOWN", "pgup": "PGUP", "pgdown": "PGDWN", "home": "HOME",
    "end": "END", "pause": "PAUSE", "print": "PRINT",
}
_MODS = ("Ctrl", "Alt", "Shift", "Meta")


def default_keys() -> dict[str, str]:
    return {a: d for a, (_o, d, _t) in ACTIONS.items()}


def qt_to_mpv(seq: str) -> str:
    """'Alt+M' (QKeySequence text) -> 'Alt+m' (mpv). Empty stays empty."""
    seq = (seq or "").split(",")[0].strip()
    if not seq:
        return ""
    parts = seq.split("+") if seq != "+" else ["+"]
    if parts[-1] == "" and len(parts) > 1:      # "Ctrl++"
        parts = parts[:-2] + ["+"]
    *mods, key = parts
    mods = [m.capitalize() if m.lower() != "meta" else "Meta" for m in mods]
    k = _QT_TO_MPV.get(key.lower(), key)
    if len(k) == 1 and k.isalpha():
        if "Shift" in mods:
            mods = [m for m in mods if m != "Shift"]
            k = k.upper()
        else:
            k = k.lower()
    elif re.fullmatch(r"[Ff]\d{1,2}", k):
        k = k.upper()
    return "+".join([m for m in _MODS if m in mods] + [k])


_MPV_TO_QT = {"BS": "Backspace", "ENTER": "Return", "KP_ENTER": "Enter", "DEL": "Del", "INS": "Ins",
              "SPACE": "Space", "ESC": "Esc", "TAB": "Tab", "LEFT": "Left", "RIGHT": "Right", "UP": "Up",
              "DOWN": "Down", "PGUP": "PgUp", "PGDWN": "PgDown", "HOME": "Home", "END": "End"}


def mpv_to_qt(key: str) -> str:
    """'Alt+m' -> 'Alt+M', 'Alt+BS' -> 'Alt+Backspace', 'Alt+M' -> 'Alt+Shift+M' (for QKeySequence)."""
    if not key:
        return ""
    parts = key.split("+") if key != "+" else ["+"]
    if parts[-1] == "" and len(parts) > 1:
        parts = parts[:-2] + ["+"]
    *mods, k = parts
    mods = [m.capitalize() for m in mods]
    if len(k) == 1 and k.isalpha():
        if k.isupper() and "Shift" not in mods:
            mods.append("Shift")
        k = k.upper()
    else:
        k = _MPV_TO_QT.get(k.upper(), k)
    order = ["Ctrl", "Alt", "Shift", "Meta"]
    return "+".join([m for m in order if m in mods] + [k])


def normalize(key: str) -> str:
    """Canonical form for comparing mpv key names ('alt+shift+m' == 'Alt+M')."""
    if not key:
        return ""
    parts = key.split("+") if key != "+" else ["+"]
    if parts[-1] == "" and len(parts) > 1:
        parts = parts[:-2] + ["+"]
    *mods, k = parts
    mods = {m.capitalize() for m in mods}
    if len(k) == 1 and k.isalpha():
        if "Shift" in mods:
            mods.discard("Shift")
            k = k.upper()
    else:
        k = k.upper()
    return "+".join([m for m in _MODS if m in mods] + [k])


@dataclass
class Conflict:
    action: str
    key: str
    kind: str          # input_conf | builtin | script | duplicate
    owner: str         # script / section name, or the other action for duplicates
    command: str

    def consequence(self) -> str:
        """English text (translated by the UI)."""
        return {
            "input_conf": "Your input.conf binding wins — this marker key will NOT work in mpv.",
            "builtin": "The marker script takes over this key — mpv's built-in action will no longer be available on it.",
            "script": "Another script uses this key — only one of the two will react.",
            "duplicate": "The same key is chosen for two marker actions — only one of them will work.",
        }[self.kind]


def find_conflicts(keys: dict[str, str], bindings: list[dict]) -> list[Conflict]:
    """Compare chosen keys with mpv's active bindings (property `input-bindings`)."""
    out: list[Conflict] = []
    seen: dict[str, str] = {}
    for action, key in keys.items():
        n = normalize(key)
        if not n:
            continue
        if n in seen:
            out.append(Conflict(action, key, "duplicate", seen[n], ""))
        seen.setdefault(n, action)
    for action, key in keys.items():
        n = normalize(key)
        if not n:
            continue
        for b in bindings:
            if normalize(str(b.get("key", ""))) != n:
                continue
            owner = str(b.get("owner") or "")
            section = str(b.get("section") or "")
            if owner == SCRIPT_NAME or section.startswith(f"input_{SCRIPT_NAME}"):
                continue  # our own previous installation
            cmd = str(b.get("cmd") or "")
            if cmd.startswith(f"script-binding {SCRIPT_NAME}/"):
                continue
            if b.get("is_weak") and section == "default":
                kind, who = "builtin", "mpv"
            elif owner or section not in ("default", ""):
                kind, who = "script", owner or section.removeprefix("input_")
            else:
                kind, who = "input_conf", "input.conf"
            out.append(Conflict(action, key, kind, who, str(b.get("comment") or cmd)))
    return out


def render_script(source: str, keys: dict[str, str]) -> str:
    """The Lua script with the chosen keys as its option defaults."""
    text = source
    for action, (opt, _d, _t) in ACTIONS.items():
        value = keys.get(action, "").replace("\\", "\\\\").replace('"', '\\"')
        text, n = re.subn(rf'({opt}\s*=\s*)"[^"]*"', lambda m, v=value: f'{m.group(1)}"{v}"', text, count=1)
        if n != 1:
            raise ValueError(f"option {opt} not found in the script")
    return text
