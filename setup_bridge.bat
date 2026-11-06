@echo off
rem Set up the Premiere Agent Bridge on this machine. Opens the setup window - python -m ppro_bridge.setup_ui -
rem which checks Python, the Python packages, Premiere, UXP Developer Tool and developer mode, and runs the
rem fixes from buttons. Without Python 3.10 or newer it opens ppro_bridge\setup_ui.html, which explains how
rem to install it.
rem setup_bridge.bat --console = the text check with y/n questions: python -m ppro_bridge.doctor --interactive
rem Python: the installer's own (python\python.exe next to this file) when it is there, else the one on PATH (the
rem zip and a git checkout).
rem No labels or goto: cmd's label search is unreliable in a batch file with LF line endings.
chcp 65001 >nul
rem Never run a python.exe from the current folder (cmd looks there before PATH): RA-3.
set "NoDefaultCurrentDirectoryInExePath=1"
title Premiere Agent Bridge setup
cd /d "%~dp0"
set "PAB_PYTHON=python"
set "PYW="
if exist "%~dp0python\python.exe" set "PAB_PYTHON=%~dp0python\python.exe"
if exist "%~dp0python\pythonw.exe" set "PYW=%~dp0python\pythonw.exe"
rem The installer's Python runs isolated (-I): its ._pth turns site on, which would add the user site (QA-8).
set "PAB_FLAGS="
if exist "%~dp0python\python.exe" set "PAB_FLAGS=-I"
rem Without Python: the page in an Edge app window, else in the default browser. A successful start keeps
rem the errorlevel it found, so it is cleared before Edge is started.
"%PAB_PYTHON%" %PAB_FLAGS% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo Python 3.10 or newer was not found.
    echo The setup page that opens now explains how to install it.
    cmd /c exit 0
    start "" msedge --app="file:///%~dp0ppro_bridge/setup_ui.html" --window-size=1120,780
    if errorlevel 1 (
        start "" "%~dp0ppro_bridge\setup_ui.html"
    )
    echo.
    pause
    exit /b 1
)
if /i "%~1"=="--console" (
    "%PAB_PYTHON%" %PAB_FLAGS% -m ppro_bridge.doctor --interactive
    echo.
    pause
    exit /b
)
rem The window's server runs under the pythonw.exe next to this Python, so no console stays open (it shows a
rem message box if it cannot start). Without pythonw.exe it runs here, with the text check as the fallback.
if not defined PYW for /f "delims=" %%p in ('python -c "import os, sys; p = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'); print(p if os.path.isfile(p) else '')"') do set "PYW=%%p"
if defined PYW (
    start "" "%PYW%" %PAB_FLAGS% -m ppro_bridge.setup_ui
    exit /b 0
)
echo Opening the setup window. This console closes with it.
"%PAB_PYTHON%" %PAB_FLAGS% -m ppro_bridge.setup_ui
if errorlevel 1 (
    echo.
    echo The setup window could not start. Running the text check instead.
    echo.
    "%PAB_PYTHON%" %PAB_FLAGS% -m ppro_bridge.doctor --interactive
    echo.
    pause
)
