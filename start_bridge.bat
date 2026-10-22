@echo off
rem Start the Premiere Agent Bridge: the broker, and the panel loaded into Premiere through UXP Developer Tool
rem (started minimized when it is not running) - every time Premiere starts. Keep this window open while you edit.
chcp 65001 >nul
title Premiere Agent Bridge
cd /d "%~dp0broker"
python broker.py --load-panel
echo.
echo The bridge stopped. If it says the port is in use, the bridge is already running.
pause
