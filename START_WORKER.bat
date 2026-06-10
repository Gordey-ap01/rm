@echo off
REM Запуск фонового воркера django-tasks.
REM Обрабатывает очередь задач в SQLite-базе (отправка email-подтверждений).
REM Откройте это окно ОТДЕЛЬНО от runserver и не закрывайте.

setlocal
set "ROOT=%~dp0.."
cd /d "%ROOT%"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv\Scripts\python.exe not found. Run START_DEMO.bat first.
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat || (
    echo [ERROR] failed to activate venv
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   DJANGO-TASKS WORKER
echo   Press Ctrl+C to stop
echo ============================================================
echo.

python manage.py db_worker %*

endlocal
