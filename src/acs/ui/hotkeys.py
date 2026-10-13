"""Default keyboard shortcuts. Used both for QActions and the F1 cheat sheet."""

from __future__ import annotations

from acs.i18n import tr

# (action id, key sequence(s), English description, group)
HOTKEYS: list[tuple[str, str, str, str]] = [
    ("play", "Space", "Play / pause", "Playback"),
    ("frame_next", "Right", "Next frame", "Playback"),
    ("frame_prev", "Left", "Previous frame", "Playback"),
    ("sec_next", "Shift+Right", "Forward 1 second", "Playback"),
    ("sec_prev", "Shift+Left", "Back 1 second", "Playback"),
    ("key_next", "Ctrl+Right", "Next keyframe", "Playback"),
    ("key_prev", "Ctrl+Left", "Previous keyframe", "Playback"),
    ("mark_next", "Down", "Next marker / clip boundary", "Playback"),
    ("mark_prev", "Up", "Previous marker / clip boundary", "Playback"),
    ("shuttle_back", "J", "Shuttle: back 1 second", "Playback"),
    ("shuttle_stop", "K", "Shuttle: pause", "Playback"),
    ("shuttle_fwd", "L", "Shuttle: play / faster", "Playback"),
    ("ep_prev", "Ctrl+PgUp", "Previous episode in folder", "Playback"),
    ("ep_next", "Ctrl+PgDown", "Next episode in folder", "Playback"),
    ("set_in", "I", "Set clip In (starts a new clip)", "Clips"),
    ("set_out", "O", "Set clip Out (current frame included)", "Clips"),
    ("marker", "M", "Add marker", "Clips"),
    ("delete", "Delete", "Delete selected clips / markers", "Clips"),
    ("undo", "Ctrl+Z", "Undo", "Edit"),
    ("redo", "Ctrl+Y", "Redo (also Ctrl+Shift+Z)", "Edit"),
    ("zoom_in", "Ctrl+=", "Zoom timeline in (also Ctrl+wheel)", "Timeline"),
    ("zoom_out", "Ctrl+-", "Zoom timeline out", "Timeline"),
    ("zoom_fit", "Ctrl+0", "Fit whole video", "Timeline"),
    ("zoom_clip", "Z", "Zoom to selected clip", "Timeline"),
    ("library", "Ctrl+L", "Show / hide library", "View"),
    ("queue", "Ctrl+Shift+Q", "Show / hide export queue", "View"),
    ("open_video", "Ctrl+O", "Open video", "File"),
    ("open_project", "Ctrl+Shift+O", "Open project", "File"),
    ("save", "Ctrl+S", "Save project", "File"),
    ("save_as", "Ctrl+Shift+S", "Save project as", "File"),
    ("export", "Ctrl+E", "Export clips", "File"),
    ("frames", "Ctrl+Shift+E", "Extract frames", "File"),
    ("help", "F1", "Keyboard shortcuts", "Help"),
]

MOUSE_HINTS: list[tuple[str, str]] = [
    ("Ctrl + wheel", "Zoom timeline around the cursor"),
    ("Wheel / Shift + wheel", "Scroll timeline"),
    ("Click / drag on timeline", "Seek (scrub)"),
    ("Drag clip edge", "Move In / Out (snaps to frames, markers, keyframes)"),
    ("Alt while dragging", "Disable snapping"),
    ("Ctrl + click clip", "Add clip to selection"),
    ("Double-click clip", "Jump to clip start"),
    ("Right click", "Context menu"),
]


def key_for(action_id: str) -> str:
    return next((k for a, k, _, _ in HOTKEYS if a == action_id), "")


def with_key(text: str, action_id: str) -> str:
    """Tooltip text with the shortcut appended: 'Set clip In (I)'."""
    k = key_for(action_id)
    return f"{tr(text)} ({k})" if k else tr(text)
