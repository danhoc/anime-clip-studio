@echo off
rem Anime Clip Studio - first-time setup for running from source.
rem Asks before installing anything (uv, FFmpeg via winget); never changes PATH or the registry itself.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Anime Clip Studio - setup
rem uv / FFmpeg installed by winget a moment ago are not in this window's PATH yet
set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links;%USERPROFILE%\.local\bin"
echo ==== Anime Clip Studio: setup / установка ====
echo.

rem ---- 1. uv: installs Python 3.13 and the libraries -------------------------------------------
where uv >nul 2>nul && goto have_uv
echo uv (Python package manager) is not installed.
echo uv (менеджер пакетов Python) не установлен.
where winget >nul 2>nul || goto uv_manual
echo.
echo   [1] Install uv with winget now / Установить uv через winget сейчас
echo   [2] No, I will install it myself / Нет, установлю сам
choice /C 12 /N /M "> "
if errorlevel 2 goto uv_manual
winget install --id astral-sh.uv -e --accept-source-agreements --accept-package-agreements
where uv >nul 2>nul && goto have_uv
:uv_manual
echo.
echo Install uv: https://docs.astral.sh/uv/getting-started/installation/ - then run install.bat again.
echo Установите uv: https://docs.astral.sh/uv/getting-started/installation/ - и запустите install.bat снова.
goto fail

:have_uv
for /f "delims=" %%v in ('uv --version') do echo [OK] %%v
echo.
echo Installing Python 3.13 and the libraries (first time: a few minutes)...
echo Установка Python 3.13 и библиотек (в первый раз - несколько минут)...
uv sync || goto fail
echo [OK] Python + PySide6, python-mpv, numpy, platformdirs

rem ---- 2. FFmpeg and libmpv: found the same way as Tools - External tools - Auto-detect ----------
echo.
uv run python -c "import sys; from pathlib import Path; from acs.core.tools import find_ff_candidates as ff, find_libmpv_candidates as mpv; r = Path('.').resolve(); sys.exit((0 if ff('ffmpeg', r) and ff('ffprobe', r) else 1) + (0 if mpv(r) else 2))"
set /a "MISSING=%errorlevel%"
set /a "NO_FF=MISSING & 1"
set /a "NO_MPV=MISSING & 2"
if %NO_FF%==0 (echo [OK] FFmpeg) else call :ffmpeg
if %NO_MPV%==0 (echo [OK] libmpv) else call :libmpv

rem ---- 3. Desktop shortcut ---------------------------------------------------------------------
echo.
echo   [1] Create a desktop shortcut / Создать ярлык на рабочем столе
echo   [2] No / Нет
choice /C 12 /N /M "> "
if errorlevel 2 goto done
powershell -NoProfile -Command "$s = (New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop') + '\Anime Clip Studio.lnk'); $s.TargetPath = '%~dp0run.bat'; $s.WorkingDirectory = '%~dp0'; $s.IconLocation = '%~dp0.venv\Scripts\pythonw.exe,0'; $s.WindowStyle = 7; $s.Save()" && echo [OK] Anime Clip Studio.lnk

:done
echo.
echo Done. Start the app with run.bat (or the desktop shortcut).
echo Готово. Запускайте приложение через run.bat (или ярлык на рабочем столе).
if not %MISSING%==0 echo The app can also find FFmpeg / libmpv later: Tools - External tools - Auto-detect.
if not %MISSING%==0 echo FFmpeg / libmpv можно указать и позже: Инструменты - Внешние программы - Найти автоматически.
pause
exit /b 0

:fail
echo.
echo Setup did not finish. / Установка не завершена.
pause
exit /b 1

:ffmpeg
echo FFmpeg (ffmpeg.exe + ffprobe.exe) was not found. / FFmpeg не найден.
where winget >nul 2>nul || goto ffmpeg_manual
echo   [1] Install FFmpeg with winget now (Gyan.FFmpeg) / Установить FFmpeg через winget сейчас
echo   [2] No, I will install it myself / Нет, установлю сам
choice /C 12 /N /M "> "
if errorlevel 2 goto ffmpeg_manual
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements && exit /b 0
:ffmpeg_manual
echo   https://www.gyan.dev/ffmpeg/builds/ - "ffmpeg-release-full.7z": put ffmpeg.exe and ffprobe.exe
echo   into the vendor folder / положите ffmpeg.exe и ffprobe.exe в папку vendor
exit /b 0

:libmpv
echo libmpv-2.dll (video player) was not found. / libmpv-2.dll (видеоплеер) не найден.
echo   Download "mpv-dev-x86_64-...7z", unpack libmpv-2.dll into the vendor folder:
echo   Скачайте "mpv-dev-x86_64-...7z" и распакуйте libmpv-2.dll в папку vendor:
echo   https://sourceforge.net/projects/mpv-player-windows/files/libmpv/
echo   [1] Open the download page / Открыть страницу загрузки
echo   [2] No / Нет
choice /C 12 /N /M "> "
if errorlevel 2 exit /b 0
start "" "https://sourceforge.net/projects/mpv-player-windows/files/libmpv/"
exit /b 0
