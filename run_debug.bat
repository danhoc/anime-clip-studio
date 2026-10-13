@echo off
rem Diagnostic launch: verbose event trace (every paint / dispatch / playback tick) + full libmpv log,
rem console output mirrored to logs\stderr-last.log. Logs: %APPDATA%\AnimeClipStudio\logs
rem NOTE: no "-X dev" / "-X faulthandler" here: on Windows faulthandler reacts to LuaJIT's
rem first-chance exceptions inside libmpv and itself crashes the interpreter.
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
set PYTHONFAULTHANDLER=
if not exist "%APPDATA%\AnimeClipStudio\logs" mkdir "%APPDATA%\AnimeClipStudio\logs"
echo Anime Clip Studio - diagnostic mode. Logs: %APPDATA%\AnimeClipStudio\logs
uv run python -m acs --debug %* 2> "%APPDATA%\AnimeClipStudio\logs\stderr-last.log"
echo.
echo Exit code: %ERRORLEVEL%
pause
