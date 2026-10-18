"""Build the archive for passing the app on without GitHub: dist/AnimeClipStudio-<version>.zip.

The committed files of HEAD (git archive: no .venv, caches, settings or untracked files) plus
vendor/libmpv-2.dll and its license (packaging/licenses/mpv -> vendor/licenses/mpv), so the receiver
only needs install.bat. FFmpeg is not bundled: install.bat offers to install it.

    uv run python packaging/make_dist.py            # with libmpv
    uv run python packaging/make_dist.py --no-libmpv
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "AnimeClipStudio/"
DLL = ROOT / "vendor" / "libmpv-2.dll"
LICENSES = ROOT / "packaging" / "licenses" / "mpv"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def main() -> int:
    with_mpv = "--no-libmpv" not in sys.argv[1:]
    version = re.search(r'__version__ = "([^"]+)"', (ROOT / "src" / "acs" / "__init__.py").read_text()).group(1)
    if git("status", "--porcelain", "--untracked-files=no").strip():
        print("note: uncommitted changes are NOT included (the archive is built from HEAD)")
    out = ROOT / "dist" / f"AnimeClipStudio-{version}.zip"
    out.parent.mkdir(exist_ok=True)
    git("archive", "--format=zip", f"--prefix={PREFIX}", "-o", str(out), "HEAD")
    if with_mpv:
        if not DLL.is_file():
            print(f"error: {DLL} not found (or use --no-libmpv)")
            return 1
        notice = (LICENSES / "README.txt").read_text(encoding="utf-8")
        digest = hashlib.sha256(DLL.read_bytes()).hexdigest()
        if digest not in notice:
            print("error: vendor/libmpv-2.dll is not the build described in packaging/licenses/mpv/README.txt "
                  f"(SHA-256 {digest}); update that file for the new build first")
            return 1
        with zipfile.ZipFile(out, "a", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            z.write(DLL, PREFIX + "vendor/libmpv-2.dll")
            for f in sorted(LICENSES.iterdir()):
                z.write(f, PREFIX + "vendor/licenses/mpv/" + f.name)
    with zipfile.ZipFile(out) as z:
        n = len(z.namelist())
    print(f"{out}  ({out.stat().st_size / 2**20:.1f} MB, {n} entries, commit {git('rev-parse', '--short', 'HEAD').strip()}"
          f"{', with libmpv' if with_mpv else ''})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
