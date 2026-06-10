@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

pushd "%~dp0."

echo.
echo === Rehab Center Demo Startup ===
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo Python is not found in PATH.
  echo Install Python 3.11 or newer from https://www.python.org/downloads/
  echo During installation, tick "Add python.exe to PATH".
  pause
  exit /b 1
)

python --version

if not exist ".env" (
  echo Creating .env from .env.example...
  copy ".env.example" ".env" >nul
)

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Creating virtual environment in .venv ...
  python -m venv .venv
  if errorlevel 1 (
    echo Failed to create venv.
    pause
    exit /b 1
  )
)

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

echo.
echo Upgrading pip ...
"%VENV_PY%" -m pip install --upgrade pip >nul

echo Installing project ...
"%VENV_PY%" -m pip install -e ".[dev]"
if errorlevel 1 (
  echo.
  echo pip install failed.
  pause
  exit /b 1
)

if not exist "data" mkdir data

echo.
echo Applying database migrations ...
"%VENV_PY%" manage.py migrate
if errorlevel 1 (
  echo.
  echo Migration failed.
  pause
  exit /b 1
)

echo.
echo Loading demo data ...
"%VENV_PY%" manage.py seed_demo
if errorlevel 1 (
  echo.
  echo Demo data loading failed.
  pause
  exit /b 1
)

echo.
echo ============================================
echo   Demo ready!
echo   URL:      http://127.0.0.1:8000/
echo   Admin:    admin / admin12345
echo ============================================
echo.

start "" "http://127.0.0.1:8000/"
"%VENV_PY%" manage.py runserver 0.0.0.0:8000

endlocal
