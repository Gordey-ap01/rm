@echo off
chcp 65001 >nul
setlocal EnableExtensions

cd /d "%~dp0"
title Rehab Center Remote Demo

echo.
echo === Rehab Center Remote Demo ===
echo.
echo This starts the local Docker app and opens a temporary public Cloudflare link.
echo This is the phone-ready path. Keep this window open while the demo is running.
echo.

where docker >nul 2>nul
if errorlevel 1 (
  echo Docker Desktop was not found on this computer.
  echo Run the project on a computer where Docker Desktop is installed, or use screen sharing instead.
  pause
  exit /b 1
)

docker info >nul 2>nul
if errorlevel 1 (
  echo Docker is installed, but the Docker service is not running. Opening Docker Desktop...
  if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" (
    start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
  )
  echo Waiting up to 2 minutes for Docker Desktop...
  for /l %%i in (1,1,24) do (
    timeout /t 5 /nobreak >nul
    docker info >nul 2>nul
    if not errorlevel 1 goto docker_ready
  )
  echo Docker Desktop did not start. Open it manually and run START_REMOTE_DEMO.bat again.
  pause
  exit /b 1
)

:docker_ready
if not exist ".env" (
  echo Creating .env from .env.example...
  copy ".env.example" ".env" >nul
)

echo.
echo Starting local app...
docker compose up --build -d
if errorlevel 1 (
  echo Docker Compose failed.
  pause
  exit /b 1
)

docker compose exec -T web python manage.py migrate
if errorlevel 1 (
  echo Migration failed.
  pause
  exit /b 1
)

docker compose exec -T web python manage.py seed_demo
if errorlevel 1 (
  echo Demo data loading failed.
  pause
  exit /b 1
)

set "CLOUDFLARED=cloudflared"
where cloudflared >nul 2>nul
if errorlevel 1 (
  if exist "tools\cloudflared.exe" (
    set "CLOUDFLARED=tools\cloudflared.exe"
  ) else (
    echo.
    echo cloudflared was not found.
    echo Download cloudflared.exe and put it into tools\cloudflared.exe, or install it:
    echo winget install --id Cloudflare.cloudflared
    echo.
    echo Local demo is still available at http://localhost:8000/
    start "" "http://localhost:8000/"
    pause
    exit /b 1
  )
)

echo.
echo Local demo is ready:
echo http://localhost:8000/
echo.
echo Public link will appear below as https://....trycloudflare.com
echo Send that link to the director or open it on a smartphone.
echo Demo login: admin / admin12345
echo.
start "" "http://localhost:8000/"
%CLOUDFLARED% tunnel --protocol http2 --url http://localhost:8000
pause
