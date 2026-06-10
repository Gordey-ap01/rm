#!/usr/bin/env bash
# Запуск фонового воркера django-tasks (аналог START_WORKER.bat).
# Обрабатывает очередь задач (отправка email-подтверждений).
# Запускайте в отдельном терминале, не закрывайте.

set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ ! -x ".venv/bin/python" ]; then
    echo "[ERROR] .venv/bin/python not found. Run start_demo.sh first."
    exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "============================================================"
echo " DJANGO-TASKS WORKER"
echo " Press Ctrl+C to stop"
echo "============================================================"

exec python manage.py db_worker "$@"
