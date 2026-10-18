# vendor/

Optional local copies of the external programs. Nothing in this folder is committed except this file.

| File | What it is | Where to get it |
|---|---|---|
| `libmpv-2.dll` | video player library (64-bit) | <https://mpv.io/installation/> → libmpv builds for Windows (`mpv-dev-x86_64-*.7z`) |
| `ffmpeg.exe`, `ffprobe.exe` | export, frame extraction, analysis | <https://ffmpeg.org/download.html> (Windows builds by gyan.dev or BtbN) |

An archive passed on by the author may already contain `libmpv-2.dll`; its license and source links are in
`licenses/mpv`.

You don't have to put them here: **Settings → External tools → Auto-detect** also finds them in PATH,
winget / scoop / chocolatey locations, Program Files, `C:\FFmpeg` and `C:\mpv`.
