"""Help → Check for updates: versions, safe archive installation, GitHub answer, Git checkouts."""

import io
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from acs import __version__
from acs.core import updates
from acs.core.updates import UpdateError


def test_versions():
    assert updates.parse_version("v1.2.10") == (1, 2, 10)
    assert updates.parse_version("0.3.0-beta") == (0, 3, 0)
    assert updates.parse_version("latest") == ()
    assert updates.is_newer("v0.2.0", "0.1.0")
    assert updates.is_newer("1.0.1", "1.0")
    assert not updates.is_newer("v0.1.0", "0.1")
    assert not updates.is_newer("0.1.0", "0.2.0")
    assert not updates.is_newer("", "0.1.0")


def test_repo_configured():
    assert updates.repo_configured("someone/anime-clip-studio")
    assert not updates.repo_configured("USERNAME/anime-clip-studio")
    assert not updates.repo_configured("not a repo")


def _zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return path


APP = {"owner-acs-abc123/pyproject.toml": "[project]", "owner-acs-abc123/src/acs/__init__.py": "v2",
       "owner-acs-abc123/src/acs/new.py": "new", "owner-acs-abc123/README.md": "readme",
       "owner-acs-abc123/vendor/README.md": "upstream vendor readme"}


def test_safe_extract_finds_the_app_folder(tmp_path):
    root = updates.safe_extract(_zip(tmp_path / "u.zip", APP), tmp_path / "x")
    assert root.name == "owner-acs-abc123"


@pytest.mark.parametrize("bad", ["../evil.py", "owner/../../evil.py"])
def test_safe_extract_refuses_paths_outside(tmp_path, bad):
    with pytest.raises(UpdateError) as e:
        updates.safe_extract(_zip(tmp_path / "u.zip", {**APP, bad: "x"}), tmp_path / "x")
    assert e.value.code == "bad_archive"
    assert not (tmp_path / "evil.py").exists()


def test_safe_extract_refuses_other_archives(tmp_path):
    with pytest.raises(UpdateError) as e:
        updates.safe_extract(_zip(tmp_path / "u.zip", {"something/readme.txt": "x"}), tmp_path / "x")
    assert e.value.code == "bad_archive"


def test_install_tree_keeps_local_folders_and_drops_removed_modules(tmp_path):
    app = tmp_path / "app"
    for rel, text in {"src/acs/__init__.py": "v1", "src/acs/old.py": "old", "tests/test_old.py": "t",
                      ".venv/lib.py": "venv", "vendor/libmpv-2.dll": "dll", "vendor/README.md": "local",
                      "src/acs/__pycache__/old.cpython-313.pyc": "pyc", "notes.txt": "mine"}.items():
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / rel).write_text(text)
    new = updates.safe_extract(_zip(tmp_path / "u.zip", APP), tmp_path / "x")
    updates.install_tree(new, app)
    assert (app / "src/acs/__init__.py").read_text() == "v2"
    assert (app / "src/acs/new.py").exists() and not (app / "src/acs/old.py").exists()
    assert not (app / "tests/test_old.py").exists()          # removed upstream
    assert (app / ".venv/lib.py").read_text() == "venv"
    assert (app / "vendor/libmpv-2.dll").exists() and (app / "vendor/README.md").read_text() == "local"
    assert (app / "notes.txt").exists()                      # files outside src / tests are left alone


class _Answer(io.BytesIO):
    headers: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_check_release(monkeypatch):
    answer = {"tag_name": "v99.0.0", "body": "line 1\nline 2", "html_url": "https://github.com/o/r/releases/v99",
              "zipball_url": "https://api.github.com/repos/o/r/zipball/v99.0.0"}
    monkeypatch.setattr(updates, "_get", lambda url, timeout=0: _Answer(json.dumps(answer).encode()))
    info = updates.check_release("o/r")
    assert info.available and info.latest == "99.0.0" and info.notes == "line 1\nline 2"
    answer["tag_name"] = f"v{__version__}"
    assert not updates.check_release("o/r").available
    with pytest.raises(UpdateError) as e:
        updates.check_release("USERNAME/anime-clip-studio")
    assert e.value.code == "not_configured"


def test_apply_release_from_archive(tmp_path, monkeypatch):
    src_zip = _zip(tmp_path / "release.zip", APP)
    monkeypatch.setattr(updates, "_download", lambda url, dest, cancel=None, on_progress=None:
                        shutil.copy(src_zip, dest))
    app = tmp_path / "app"
    (app / "src/acs").mkdir(parents=True)
    (app / "src/acs/__init__.py").write_text("v1")
    updates.apply_release(app, "https://api.github.com/repos/o/r/zipball/v2")
    assert (app / "src/acs/__init__.py").read_text() == "v2"
    with pytest.raises(UpdateError):
        updates.apply_release(app, "file:///etc/passwd")


# ---- Git checkouts ---------------------------------------------------------------------------
pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _run(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "origin.git"
    _run(tmp_path, "init", "--bare", "-b", "main", str(origin))
    dev = tmp_path / "dev"
    _run(tmp_path, "clone", str(origin), str(dev))
    (dev / "app.py").write_text("v1\n")
    _run(dev, "add", ".")
    _run(dev, "commit", "-m", "first")
    _run(dev, "push", "origin", "HEAD:main")
    user = tmp_path / "user"
    _run(tmp_path, "clone", str(origin), str(user))
    return dev, user


@pytestmark_git
def test_git_check_and_pull(repos):
    dev, user = repos
    assert updates.install_kind(user) == "git"
    assert not updates.check_git(user).available
    (dev / "app.py").write_text("v2\n")
    _run(dev, "commit", "-am", "second")
    _run(dev, "push", "origin", "HEAD:main")
    info = updates.check_git(user)
    assert info.available and len(info.commits) == 1 and info.commits[0].endswith("second")
    updates.apply(user, info)
    assert (user / "app.py").read_text() == "v2\n"
    assert not updates.check_git(user).available


@pytestmark_git
def test_git_refuses_local_changes(repos):
    dev, user = repos
    (dev / "app.py").write_text("v2\n")
    _run(dev, "commit", "-am", "second")
    _run(dev, "push", "origin", "HEAD:main")
    (user / "app.py").write_text("my change\n")
    (user / "untracked.txt").write_text("fine\n")
    with pytest.raises(UpdateError) as e:
        updates.apply_git(user)
    assert e.value.code == "dirty"
    assert (user / "app.py").read_text() == "my change\n"


@pytestmark_git
def test_git_without_upstream(tmp_path):
    _run(tmp_path, "init", "-b", "main")
    (tmp_path / "a.txt").write_text("a")
    _run(tmp_path, "add", ".")
    _run(tmp_path, "commit", "-m", "a")
    with pytest.raises(UpdateError) as e:
        updates.check_git(tmp_path)
    assert e.value.code == "no_upstream"
