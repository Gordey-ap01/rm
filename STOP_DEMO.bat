@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Rehab Center Demo Stop

echo Stopping demo containers...
docker compose down
echo.
echo Done. Docker volume data is preserved.
pause
