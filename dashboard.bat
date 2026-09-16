@echo off
cd /d C:\azerothcore-playerbots
title AzerothCore Local Dashboard

where py >nul 2>nul
if errorlevel 1 (
    echo Le lanceur Python "py" n'est pas trouve dans le PATH.
    echo Python utilise auparavant sur ce PC : py
    pause
    exit /b 1
)

start "" http://127.0.0.1:8765/
py ".\dashboard_server.py"
pause
