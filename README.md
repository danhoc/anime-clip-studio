# Anime Clip Studio

Frame-accurate clipping and frame extraction for locally stored anime episodes (H.264 / HEVC, including 10-bit)
on Windows. Built with Python, Qt (PySide6), an embedded **libmpv** player and **FFmpeg**.

> 🇷🇺 Кратко: настольное приложение для покадровой нарезки клипов и массовой выгрузки кадров из серий аниме.
> Метки и несколько клипов на одном таймлайне, smart cut без потери качества, перекодирование в
> x264 / x265 / SVT-AV1 / NVENC, GIF и WebP, выгрузка кадров с отсевом дублей. Интерфейс на английском и русском.

## Features

- **Frame-accurate navigation**: step single frames, jump between keyframes, markers and clip edges;
  the exact frame index is built from packet timestamps (VFR-safe). Timecode shows `frame within second / frames in that second`.
- **Timeline** with keyframe ticks, thumbnail strip, audio waveform (quality adapts to the display),
  zoom down to single frames, coloured markers, any number of clips; everything undoable (Ctrl+Z / Ctrl+Y).
- **Per-clip settings**: mute, audio track, burned-in subtitles (ASS with attached fonts), crop (auto-detect),
  downscale-only resolution, export preset.
- **Export**
  - *Original (smart cut)*: only the partial GOPs at the clip edges are re-encoded, the middle is stream-copied
    bit-exactly; the result is verified (frame count, decode check, PSNR at every splice) with automatic fallback.
  - Re-encode with libx264, libx265, SVT-AV1, libaom, rav1e, VP9, VVC, or NVENC (H.264 / HEVC / AV1).
  - Target-size presets (two-pass) with optional automatic downscaling, e.g. *Discord 10 MB / 50 MB*.
  - GIF and animated WebP with quality presets and *fit into N MB* search with lockable parameters / minimums.
- **Frame extraction**: every N seconds or one per scene change, optional subtitles, two duplicate-filter strategies,
  PNG (8/16-bit), JPEG, WebP; resumable.
- **Library** of folders with episodes, project files (`.acsproj`), autosave, crash recovery (snapshots before
  heavy tasks, resumable export queue), dark / light theme, interface scaling, event trace for diagnostics.

## Requirements

- Windows 10/11 x64
- [uv](https://docs.astral.sh/uv/) (installs Python 3.13 and the dependencies)
- **FFmpeg** (`ffmpeg.exe`, `ffprobe.exe`) — <https://ffmpeg.org/download.html>, e.g. `winget install --id Gyan.FFmpeg -e`
- **libmpv** (`libmpv-2.dll`, 64-bit) — <https://mpv.io/installation/> → libmpv builds for Windows

The app finds both automatically (Settings → External tools → Auto-detect) and never downloads anything itself.
See [vendor/README.md](vendor/README.md).

## Run

```bat
git clone <this repo>
cd anime-clip-studio
uv sync
run.bat            :: or: uv run python -m acs
```

`run_debug.bat` starts a diagnostic session: verbose event trace, full libmpv log and console output in
`%APPDATA%\AnimeClipStudio\logs`.

## Keyboard (defaults)

| Key | Action | Key | Action |
|---|---|---|---|
| Space | play / pause | I / O | clip start / end |
| ← / → | ±1 frame | M | marker |
| Shift+← / → | ±1 s | Ctrl+E | export clips |
| Ctrl+← / → | keyframes | Ctrl+Shift+E | extract frames |
| ↑ / ↓ | markers / clip edges | Ctrl+wheel | zoom timeline |
| Ctrl+Z / Ctrl+Y | undo / redo | F1 | all shortcuts |

## Development

```bat
uv run pytest            :: fast unit tests
uv run pytest -m slow    :: integration tests (need ffmpeg; encode synthetic clips)
```

```
src/acs/core   media logic without UI: probe, frame index, smart cut, ffmpeg commands, export / frames
               runners, job queue, project files, recovery, presets, tools detection
src/acs/ui     PySide6 widgets: main window, player (libmpv), timeline, panels, dialogs
src/acs/i18n   English source strings + Russian dictionary
tests          pytest suite
```

Data locations: settings, presets, autosaves and logs in `%APPDATA%\AnimeClipStudio`, thumbnail / waveform cache in
`%LOCALAPPDATA%\AnimeClipStudio\Cache`.

## Notes

- FFmpeg and libmpv are separate programs with their own licenses (LGPL/GPL depending on the build); they are not
  part of this repository.
- On Windows the app does not enable Python's `faulthandler`: LuaJIT inside libmpv raises SEH exceptions as normal
  control flow and faulthandler reacting to them corrupts the interpreter.
