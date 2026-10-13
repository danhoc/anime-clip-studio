"""Locating and validating the external tools (ffmpeg, ffprobe, libmpv).

Deliberately conservative so it never looks like malware to antivirus software and never surprises
the user:
  * only well-known install locations are inspected (no drive crawling; depth <= 2);
  * nothing is downloaded, installed, unpacked, or written to PATH / the registry;
  * ffmpeg/ffprobe are validated by running them with `-version` (read-only) and `-encoders`;
  * libmpv is validated by reading its PE header only — it is not loaded until the app restarts.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

from acs.core.util import run_capture

LIBMPV_NAMES = ("libmpv-2.dll", "mpv-2.dll")

OFFICIAL_LINKS = {
    "ffmpeg": [
        ("ffmpeg.org — official download page", "https://ffmpeg.org/download.html"),
        ("Windows builds by gyan.dev (linked from ffmpeg.org)", "https://www.gyan.dev/ffmpeg/builds/"),
        ("Windows builds by BtbN on GitHub (linked from ffmpeg.org)", "https://github.com/BtbN/FFmpeg-Builds/releases"),
    ],
    "libmpv": [
        ("mpv.io — official installation page", "https://mpv.io/installation/"),
        ("libmpv for Windows (mpv-dev-x86_64-*.7z, linked from mpv.io)",
         "https://sourceforge.net/projects/mpv-player-windows/files/libmpv/"),
        ("shinchiro builds on GitHub", "https://github.com/shinchiro/mpv-winbuild-cmake/releases"),
    ],
}
WINGET_FFMPEG = "winget install --id Gyan.FFmpeg -e"

FEATURE_ENCODERS = ("libx264", "libx265", "libsvtav1", "libaom-av1", "libvpx-vp9", "libvvenc",
                    "h264_nvenc", "hevc_nvenc", "av1_nvenc", "libopus", "libwebp_anim")


@dataclass
class ToolStatus:
    kind: str                     # ffmpeg | ffprobe | libmpv
    path: str = ""
    ok: bool = False
    version: str = ""
    error: str = ""
    features: dict[str, bool] = field(default_factory=dict)
    source: str = ""              # where it was found: settings / vendor / PATH / winget / ...

    @property
    def missing_features(self) -> list[str]:
        return [k for k, v in self.features.items() if not v]


# ---- candidate locations --------------------------------------------------------------------
def _env_path(var: str, *parts: str) -> Path | None:
    base = os.environ.get(var)
    return Path(base, *parts) if base else None


def _glob_dirs(base: Path | None, pattern: str) -> list[Path]:
    if base is None or not base.is_dir():
        return []
    try:
        return sorted((p for p in base.glob(pattern) if p.is_dir()), reverse=True)  # newest version first
    except OSError:
        return []


def ffmpeg_dirs(app_root: Path) -> list[tuple[Path, str]]:
    """Directories that may contain ffmpeg.exe / ffprobe.exe, in priority order."""
    dirs: list[tuple[Path, str]] = [(app_root / "vendor", "vendor")]
    winget = _env_path("LOCALAPPDATA", "Microsoft", "WinGet")
    if winget is not None:
        dirs.append((winget / "Links", "winget"))
        for pkg in ("Gyan.FFmpeg*", "BtbN.FFmpeg*"):
            dirs += [(d, "winget") for d in _glob_dirs(winget / "Packages", f"{pkg}/*/bin")]
    for base in (_env_path("SCOOP"), _env_path("USERPROFILE", "scoop")):
        if base is not None:
            dirs.append((base / "apps" / "ffmpeg" / "current" / "bin", "scoop"))
            dirs.append((base / "shims", "scoop"))
    choco = _env_path("ChocolateyInstall") or Path(r"C:\ProgramData\chocolatey")
    dirs.append((choco / "bin", "chocolatey"))
    dirs += [(d, "chocolatey") for d in _glob_dirs(choco / "lib" / "ffmpeg" / "tools", "ffmpeg*/bin")]
    for pf in (_env_path("ProgramFiles"), _env_path("ProgramFiles(x86)")):
        if pf is not None:
            dirs.append((pf / "ffmpeg" / "bin", "Program Files"))
            dirs.append((pf / "FFmpeg" / "bin", "Program Files"))
    for root in (Path(r"C:\FFmpeg"), Path(r"C:\ffmpeg"), _env_path("USERPROFILE", "ffmpeg")):
        if root is not None:
            dirs.append((root / "bin", "folder"))
            dirs.append((root, "folder"))
    return dirs


def libmpv_dirs(app_root: Path) -> list[tuple[Path, str]]:
    """Directories that may contain libmpv-2.dll (each also checked one level deeper)."""
    dirs: list[tuple[Path, str]] = [(app_root / "vendor", "vendor")]
    for base in (_env_path("SCOOP"), _env_path("USERPROFILE", "scoop")):
        if base is not None:
            dirs.append((base / "apps" / "mpv" / "current", "scoop"))
            dirs.append((base / "apps" / "mpv.net" / "current", "scoop"))
    for pf in (_env_path("ProgramFiles"), _env_path("ProgramFiles(x86)")):
        if pf is not None:
            dirs.append((pf / "mpv", "Program Files"))
            dirs.append((pf / "mpv.net", "Program Files"))
    local = _env_path("LOCALAPPDATA", "Programs")
    if local is not None:
        dirs.append((local / "mpv.net", "user programs"))
        dirs.append((local / "mpv", "user programs"))
    dirs.append((Path(r"C:\mpv"), "folder"))
    return dirs


def _path_dirs() -> list[tuple[Path, str]]:
    return [(Path(p), "PATH") for p in os.environ.get("PATH", "").split(os.pathsep) if p.strip()]


def _unique(items: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    seen: set[str] = set()
    out = []
    for p, src in items:
        key = os.path.normcase(str(p))
        if key not in seen:
            seen.add(key)
            out.append((p, src))
    return out


def find_ff_candidates(exe: str, app_root: Path) -> list[tuple[Path, str]]:
    name = exe if exe.lower().endswith(".exe") else exe + ".exe"
    found = []
    which = shutil.which(exe)
    dirs = ffmpeg_dirs(app_root)[:1] + ([(Path(which).parent, "PATH")] if which else []) + ffmpeg_dirs(app_root)[1:]
    for d, src in _unique(dirs):
        f = d / name
        if f.is_file():
            found.append((f, src))
    return _unique(found)


def find_libmpv_candidates(app_root: Path) -> list[tuple[Path, str]]:
    found = []
    dirs = libmpv_dirs(app_root)[:1] + _path_dirs() + libmpv_dirs(app_root)[1:]
    for d, src in _unique(dirs):
        if not d.is_dir():
            continue
        levels = [d]
        if src != "PATH":  # one level deeper for portable/unpacked bundles, never for PATH dirs
            try:
                levels += sorted((c for c in d.iterdir() if c.is_dir()), key=lambda c: c.name)[:40]
            except OSError:
                pass
        for level in levels:
            for name in LIBMPV_NAMES:
                f = level / name
                if f.is_file():
                    found.append((f, src))
    return _unique(found)


# ---- validation -----------------------------------------------------------------------------
_VERSION_RE = re.compile(r"^(ffmpeg|ffprobe) version (\S+)", re.MULTILINE)


def parse_ff_version(text: str, kind: str) -> str | None:
    m = _VERSION_RE.search(text)
    if not m or m.group(1) != kind:
        return None
    return m.group(2)


def parse_encoders(text: str) -> dict[str, bool]:
    names = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return {e: e in names for e in FEATURE_ENCODERS}


def check_ff(path: str | Path, kind: str, with_features: bool = True, timeout: float = 15) -> ToolStatus:
    st = ToolStatus(kind, str(path))
    p = Path(path)
    if not p.is_file():
        st.error = "file not found"
        return st
    try:
        res = run_capture([str(p), "-hide_banner", "-version"], timeout=timeout)
    except (OSError, Exception) as e:  # noqa: BLE001 - blocked by AV, not an executable, timeout...
        st.error = f"cannot run: {e}"
        return st
    ver = parse_ff_version(res.stdout or res.stderr, kind)
    if res.returncode != 0 or ver is None:
        st.error = f"not a working {kind} (exit code {res.returncode})"
        return st
    st.ok, st.version = True, ver
    if kind == "ffmpeg" and with_features:
        st.features["libass (burn-in subtitles)"] = "--enable-libass" in (res.stdout or "")
        try:
            enc = run_capture([str(p), "-hide_banner", "-encoders"], timeout=timeout)
            st.features.update(parse_encoders(enc.stdout))
        except Exception:  # noqa: BLE001
            pass
    return st


def pe_machine(path: str | Path) -> int | None:
    """Machine field of a PE file (0x8664 = x64) read from the header; None if not a PE DLL."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return None
    if len(head) < 0x40 or head[:2] != b"MZ":
        return None
    off = struct.unpack_from("<I", head, 0x3C)[0]
    if off + 6 > len(head) or head[off:off + 4] != b"PE\0\0":
        return None
    return struct.unpack_from("<H", head, off + 4)[0]


def check_libmpv(path: str | Path) -> ToolStatus:
    st = ToolStatus("libmpv", str(path))
    p = Path(path)
    if not p.is_file():
        st.error = "file not found"
        return st
    machine = pe_machine(p)
    if machine is None:
        st.error = "not a valid DLL"
        return st
    if machine != 0x8664 and sys.maxsize > 2**32:
        st.error = "not a 64-bit DLL (the app is 64-bit)"
        return st
    st.ok = True
    st.version = f"{p.stat().st_size / 2**20:.0f} MB"
    return st


def detect(kind: str, current: str, app_root: Path) -> ToolStatus:
    """Keep the current path if it works, otherwise take the first working candidate."""
    check = check_libmpv if kind == "libmpv" else (lambda p: check_ff(p, kind))
    if current:
        st = check(current)
        if st.ok:
            st.source = "settings"
            return st
    cands = find_libmpv_candidates(app_root) if kind == "libmpv" else find_ff_candidates(kind, app_root)
    first_error = None
    for path, src in cands:
        st = check(path)
        if st.ok:
            st.source = src
            return st
        first_error = first_error or st
    st = ToolStatus(kind, current, error="not found" if first_error is None else
                    f"found but not usable: {first_error.path} ({first_error.error})")
    return st


def quick_ok(kind: str, path: str) -> bool:
    """Cheap startup check (no process launch): does the configured file look right?"""
    if not path or not Path(path).is_file():
        return False
    if kind == "libmpv":
        return pe_machine(path) is not None
    return True
