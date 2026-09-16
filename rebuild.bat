@echo off
cd /d C:\azerothcore-playerbots
powershell -NoProfile -ExecutionPolicy Bypass -File ".\rebuild-azerothcore.ps1"
pause