@echo off
rem Anime Clip Studio launcher (runs from source via uv; a file path can be passed as an argument).
rem First time: run install.bat.
chcp 65001 >nul
cd /d "%~dp0"
set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links;%USERPROFILE%\.local\bin"
where uv >nul 2>nul || goto no_uv
start "" uv run pythonw -m acs %*
exit /b 0
:no_uv
echo uv was not found: run install.bat first.
echo uv не найден: сначала запустите install.bat.
pause
exit /b 1
