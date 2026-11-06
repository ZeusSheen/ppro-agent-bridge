@echo off
rem Start the Premiere Agent Bridge: the broker, and the panel loaded into Premiere through UXP Developer Tool
rem (started minimized when it is not running) - every time Premiere starts. Keep this window open while you edit.
rem Python: the installer's own (python\python.exe next to this file) when it is there, else the one on PATH (the
rem zip and a git checkout).
chcp 65001 >nul
rem Never run a python.exe from the current folder (cmd looks there before PATH): RA-3.
set "NoDefaultCurrentDirectoryInExePath=1"
title Premiere Agent Bridge
set "PAB_PYTHON=python"
if exist "%~dp0python\python.exe" set "PAB_PYTHON=%~dp0python\python.exe"
rem The installer's Python runs isolated (-I): its ._pth turns site on, which would add the user site (QA-8).
set "PAB_FLAGS="
if exist "%~dp0python\python.exe" set "PAB_FLAGS=-I"
cd /d "%~dp0broker"
"%PAB_PYTHON%" %PAB_FLAGS% broker.py --load-panel
echo.
echo The bridge stopped. If it says the port is in use, the bridge is already running.
pause
