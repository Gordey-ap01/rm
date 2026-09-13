@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0pilot\Start-Pilot.ps1" %*
set "pilotExit=%ERRORLEVEL%"
if not "%pilotExit%"=="0" pause
exit /b %pilotExit%
