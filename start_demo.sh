#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo
echo "=== Rehab Center Demo Startup (local Python) ==="
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is not found in PATH. Install Python 3.12+ from https://www.python.org/downloads/"
  exit 1
fi

PY_VERSION="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
PY_MAJOR="${PY_VERSION%%.*}"
PY_MINOR="${PY_VERSION#*.}"

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 12 ]; }; then
  echo "Python 3.12+ is required, found $PY_VERSION."
  exit 1
fi

echo "Detected Python $PY_VERSION"

if [ ! -f ".env" ]; then
  echo "Creating .env from .env.example ..."
  cp .env.example .env
fi

if [ ! -x ".venv/bin/python" ]; then
  echo
  echo "Creating virtual environment in .venv ..."
  python3 -m venv .venv
fi

VENV_PY="$(pwd)/.venv/bin/python"

echo
echo "Upgrading pip ..."
"$VENV_PY" -m pip install --upgrade pip >/dev/null

echo "Installing project (with dev extras) ..."
"$VENV_PY" -m pip install -e ".[dev]"

mkdir -p data

echo
echo "Applying database migrations ..."
"$VENV_PY" manage.py migrate

echo
echo "Loading demo data (idempotent) ..."
"$VENV_PY" manage.py seed_demo

echo
echo "Demo is ready."
echo "URL:        http://localhost:8000/"
echo "Admin:      admin / admin12345"
echo "Specialist accounts: specialist1..specialist4 / specialist123"
echo
echo "Press Ctrl+C in this window to stop the server."
echo

"$VENV_PY" manage.py runserver 0.0.0.0:8000
