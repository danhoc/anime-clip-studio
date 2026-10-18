"""Checking for and installing app updates, only when the user asks for it.

Two kinds of installation:
  * a Git checkout (`.git` next to the app): `git fetch` shows the new commits of the tracked branch,
    the update is `git pull --ff-only` (refused while tracked files have local changes);
  * a downloaded copy (ZIP of a release or of the repository): the latest GitHub release is compared
    with __version__, the update downloads the release archive, checks it and copies it over the app
    folder. `.venv`, `vendor` and `.git` are never touched; files removed upstream are removed from
    `src` and `tests`.
Dependencies are synced by `uv run` when the app is started again through run.bat (the running app
keeps the libraries in .venv loaded, so they cannot be replaced while it runs).

Nothing here runs by itself: the UI calls check() on request and apply() after the user agreed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from acs import GITHUB_REPO, __version__
from acs.core.util import creation_flags

KEEP = {".venv", "vendor", ".git"}      # top-level entries of the app folder an update never changes
PRUNE = ("src", "tests")                # folders whose files removed upstream are deleted
MAX_ARCHIVE = 200 * 2**20
API = "https://api.github.com/repos/{repo}/releases/latest"
TIMEOUT = 20


class UpdateError(Exception):
    """`code` is shown to the user (translated by the UI), `detail` is technical."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class UpdateInfo:
    kind: str                       # git | release
    available: bool
    current: str = __version__
    latest: str = ""                # version of the release / short hash of the newest commit
    notes: str = ""                 # release notes or the list of new commits
    url: str = ""                   # page to open in the browser
    archive_url: str = ""           # release zip (kind == "release")
    commits: list[str] = field(default_factory=list)


# ---- versions --------------------------------------------------------------------------------
def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.10' / '1.2' / '0.3.0-beta' -> (1, 2, 10) / (1, 2) / (0, 3, 0); () if there are no numbers."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "")
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    n = max(len(a), len(b))
    return bool(a) and a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


# ---- installation kind -----------------------------------------------------------------------
def install_kind(root: Path) -> str:
    return "git" if (root / ".git").exists() and shutil.which("git") else "release"


def repo_configured(repo: str = GITHUB_REPO) -> bool:
    return bool(re.fullmatch(r"[\w.-]+/[\w.-]+", repo)) and not repo.startswith("USERNAME/")


def check(root: Path, repo: str = GITHUB_REPO) -> UpdateInfo:
    return check_git(root) if install_kind(root) == "git" else check_release(repo)


def apply(root: Path, info: UpdateInfo, cancel=None, on_progress=None) -> None:
    if info.kind == "git":
        apply_git(root)
    else:
        apply_release(root, info.archive_url, cancel=cancel, on_progress=on_progress)


# ---- Git checkout ----------------------------------------------------------------------------
def _git(root: Path, *args: str, timeout: float = 60) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")   # never wait for a password prompt
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env, creationflags=creation_flags(),
                           stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise UpdateError("no_git", str(e)) from e
    except subprocess.TimeoutExpired as e:
        raise UpdateError("network", f"git {args[0]}: timeout") from e
    if r.returncode != 0:
        raise UpdateError("git_failed", (r.stderr or r.stdout).strip()[-500:])
    return r.stdout


def check_git(root: Path) -> UpdateInfo:
    try:
        _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", timeout=15)
    except UpdateError as e:
        if e.code == "git_failed":
            raise UpdateError("no_upstream", e.detail) from e
        raise
    try:
        _git(root, "fetch", "--quiet")
    except UpdateError as e:
        raise UpdateError("network", e.detail) from e
    log = _git(root, "log", "--format=%h %s", "HEAD..@{u}").splitlines()
    newest = log[0].split(" ", 1)[0] if log else ""
    url = _git(root, "remote", "get-url", "origin", timeout=15).strip() if log else ""
    return UpdateInfo("git", bool(log), latest=newest, notes="\n".join(log[:15]), url=_web_url(url),
                      commits=log)


def _web_url(remote: str) -> str:
    """https://github.com/a/b(.git) or git@github.com:a/b(.git) -> https://github.com/a/b/commits."""
    m = re.match(r"(?:https://|git@)([^/:]+)[/:](.+?)(?:\.git)?/?$", remote)
    return f"https://{m.group(1)}/{m.group(2)}/commits" if m else ""


def apply_git(root: Path) -> None:
    if _git(root, "status", "--porcelain", "--untracked-files=no").strip():
        raise UpdateError("dirty")
    try:
        _git(root, "pull", "--ff-only", "--quiet", timeout=300)
    except UpdateError as e:
        if e.code == "git_failed" and ("diverg" in e.detail or "fast-forward" in e.detail):
            raise UpdateError("diverged", e.detail) from e
        raise


# ---- downloaded copy: GitHub releases --------------------------------------------------------
def _get(url: str, timeout: float = TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": f"AnimeClipStudio/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    try:
        return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - fixed https URLs only
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("no_release", url) from e
        raise UpdateError("network", f"HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError("network", str(getattr(e, "reason", e))) from e


def check_release(repo: str = GITHUB_REPO) -> UpdateInfo:
    if not repo_configured(repo):
        raise UpdateError("not_configured", repo)
    with _get(API.format(repo=repo)) as r:
        try:
            data = json.loads(r.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise UpdateError("network", f"bad answer: {e}") from e
    tag = str(data.get("tag_name") or "")
    notes = "\n".join(str(data.get("body") or "").strip().splitlines()[:15])
    return UpdateInfo("release", is_newer(tag, __version__), latest=tag.lstrip("v"), notes=notes,
                      url=str(data.get("html_url") or ""), archive_url=str(data.get("zipball_url") or ""))


def apply_release(root: Path, archive_url: str, cancel=None, on_progress=None) -> None:
    if not archive_url.startswith("https://"):
        raise UpdateError("bad_archive", archive_url)
    with tempfile.TemporaryDirectory(prefix="acs-update-") as tmp:
        zpath = Path(tmp) / "update.zip"
        _download(archive_url, zpath, cancel, on_progress)
        new_root = safe_extract(zpath, Path(tmp) / "x")
        install_tree(new_root, root)


def _download(url: str, dest: Path, cancel=None, on_progress=None) -> None:
    with _get(url, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if cancel is not None and cancel():
                raise UpdateError("cancelled")
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            done += len(chunk)
            if done > MAX_ARCHIVE:
                raise UpdateError("bad_archive", "too large")
            f.write(chunk)
            if on_progress is not None:
                on_progress(done, total)


def safe_extract(zpath: Path, dest: Path) -> Path:
    """Unpack the archive (refusing paths that leave `dest`) and return the folder of the app in it."""
    try:
        with zipfile.ZipFile(zpath) as z:
            base = dest.resolve()
            for name in z.namelist():
                target = (dest / name).resolve()
                if target != base and base not in target.parents:
                    raise UpdateError("bad_archive", f"unsafe path {name!r}")
            z.extractall(dest)
    except zipfile.BadZipFile as e:
        raise UpdateError("bad_archive", str(e)) from e
    # GitHub archives hold one folder "<owner>-<repo>-<commit>/"
    candidates = [dest] + [p for p in dest.iterdir() if p.is_dir()]
    for c in candidates:
        if (c / "pyproject.toml").is_file() and (c / "src" / "acs" / "__init__.py").is_file():
            return c
    raise UpdateError("bad_archive", "not an Anime Clip Studio archive")


def install_tree(new_root: Path, root: Path) -> None:
    """Copy the new version over the app folder, keeping KEEP; drop files removed from PRUNE folders."""
    new_files = {p.relative_to(new_root) for p in new_root.rglob("*") if p.is_file()}
    new_files = {p for p in new_files if p.parts[0] not in KEEP}
    for rel in sorted(new_files):
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(new_root / rel, dst)
    for top in PRUNE:
        base = root / top
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*"), reverse=True):   # files before their folders
            rel = p.relative_to(root)
            if "__pycache__" in rel.parts:
                continue
            if p.is_file() and rel not in new_files:
                p.unlink()
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()
