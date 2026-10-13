@echo off
rem Anime Clip Studio launcher (runs from source via uv; a file path can be passed as an argument)
cd /d "%~dp0"
start "" uv run pythonw -m acs %*
