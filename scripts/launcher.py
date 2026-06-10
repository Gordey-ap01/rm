#!/usr/bin/env python
"""RMcodex Viewer — портативный лаунчер.

Запускает Django + (опционально) портативный PostgreSQL, выполняет миграции,
сидирование и открывает браузер.
Поддерживает PyInstaller (frozen) и обычный Python-запуск.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import webbrowser
from pathlib import Path

# ── Определяем директории ──────────────────────────────────────────────────
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    APP_DIR = Path(sys._MEIPASS).resolve()          # _internal/
    DATA_DIR = Path(sys.executable).parent.resolve() # рядом с EXE
else:
    APP_DIR = Path(__file__).resolve().parent.parent # корень проекта
    DATA_DIR = APP_DIR / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)

os.chdir(str(APP_DIR))
sys.path.insert(0, str(APP_DIR))
os.environ["DJANGO_SETTINGS_MODULE"] = "rehab_center.settings_viewer"

# ── Логирование ────────────────────────────────────────────────────────────
log = logging.getLogger("rmcodex")
_log_initialized = False

def _init_logging(log_file: str | None = None) -> None:
    global _log_initialized
    if _log_initialized:
        return
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        lf = Path(log_file)
        lf.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(str(lf), encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    _log_initialized = True

# ── PostgreSQL management ──────────────────────────────────────────────────

PG_BIN = APP_DIR / "pgsql" / "bin"
PG_DATA = DATA_DIR / "pgdata"
PG_LOG_PATH = DATA_DIR / "pg.log"


def _find_pg_ctl() -> Path | None:
    candidates = [
        PG_BIN / "pg_ctl.exe",
        Path(r"C:\Program Files\PostgreSQL\17\bin\pg_ctl.exe"),
        Path(r"C:\Program Files\PostgreSQL\16\bin\pg_ctl.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    for p in os.environ.get("PATH", "").split(";"):
        candidate = Path(p) / "pg_ctl.exe"
        if candidate.is_file():
            return candidate
    return None


def _ensure_pg_stopped(pg_ctl: Path) -> None:
    """Останавливает PostgreSQL, если уже запущен (чистим мусор)."""
    try:
        subprocess.run(
            [str(pg_ctl), "-D", str(PG_DATA), "-w", "stop", "-m", "fast"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        pass


def _start_postgres(pg_ctl: Path) -> None:
    """Инициализирует и запускает локальный PostgreSQL."""
    _ensure_pg_stopped(pg_ctl)
    PG_DATA.mkdir(parents=True, exist_ok=True)
    PG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Чистим битый pgdata, если есть
    stale_pid = PG_DATA / "postmaster.pid"
    if stale_pid.exists():
        log.info("Обнаружен битый pgdata, очистка...")
        shutil.rmtree(str(PG_DATA), ignore_errors=True)
        PG_DATA.mkdir(parents=True, exist_ok=True)

    if not (PG_DATA / "PG_VERSION").is_file():
        initdb = pg_ctl.parent / "initdb.exe"
        if not initdb.is_file():
            raise RuntimeError(f"initdb не найден: {initdb}")
        log.info("Инициализация PostgreSQL...")
        subprocess.run(
            [str(initdb), "-D", str(PG_DATA), "--username=rehab", "--auth=trust"],
            check=True, capture_output=True, text=True, timeout=15,
        )

    log.info("Запуск PostgreSQL...")
    subprocess.run(
        [str(pg_ctl), "-D", str(PG_DATA), "-l", str(PG_LOG_PATH), "-w", "start",
         "-o", "-p 5433"],
        check=True, capture_output=True, text=True, timeout=15,
    )

    createdb = pg_ctl.parent / "createdb.exe"
    if createdb.is_file():
        try:
            subprocess.run(
                [str(createdb), "-h", "127.0.0.1", "-p", "5433", "-U", "rehab", "rehab_viewer"],
                capture_output=True, text=True,
            )
        except subprocess.CalledProcessError:
            pass


def _stop_postgres(pg_ctl: Path) -> None:
    log.info("Остановка PostgreSQL...")
    try:
        subprocess.run(
            [str(pg_ctl), "-D", str(PG_DATA), "-w", "stop", "-m", "fast"],
            check=True, capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        log.warning(f"Ошибка остановки PostgreSQL: {e}")


# ── Парсинг аргументов ДО Django setup ─────────────────────────────────────
parser = argparse.ArgumentParser(description="RMcodex Viewer")
parser.add_argument("--port", type=int, default=0, help="Порт сервера")
parser.add_argument("--no-browser", action="store_true", help="Не открывать браузер")
parser.add_argument(
    "--db", choices=["postgres", "sqlite", "auto"], default="sqlite",
    help="База данных (sqlite — быстро, postgres — если нужен PG, auto — PG если доступен)",
)
parser.add_argument(
    "--log-file", type=str, default=None,
    help="Путь к файлу лога (по умолчанию только stdout)",
)
_args, _ = parser.parse_known_args()

_init_logging(_args.log_file)
log.info("RMcodex Viewer запускается...")
log.info(f"  APP_DIR = {APP_DIR}")
log.info(f"  DATA_DIR = {DATA_DIR}")

# ── Определяем БД ДО django.setup() ────────────────────────────────────────
pg_ctl = _find_pg_ctl() if _args.db in ("auto", "postgres") else None
using_pg = False

if _args.db == "postgres" and not pg_ctl:
    log.error("PostgreSQL не найден. Установите PostgreSQL или используйте --db=sqlite")
    sys.exit(1)

if pg_ctl and _args.db in ("auto", "postgres"):
    try:
        _start_postgres(pg_ctl)
        os.environ["VIEWER_DB"] = "postgres"
        os.environ["VIEWER_PG_HOST"] = "127.0.0.1"
        os.environ["VIEWER_PG_PORT"] = "5433"
        os.environ["VIEWER_PG_USER"] = "rehab"
        os.environ["VIEWER_PG_PASSWORD"] = "rehab_dev_password"
        using_pg = True
        log.info("PostgreSQL запущен успешно")
    except (subprocess.TimeoutExpired, Exception) as e:
        log.warning(f"Не удалось запустить PostgreSQL: {e}")
        if _args.db == "postgres":
            sys.exit(1)
        log.info("Переключение на SQLite...")

if not using_pg:
    os.environ["VIEWER_DB"] = "sqlite"
    log.info("Используется SQLite")

# ── Инициализируем Django (теперь с правильной БД) ─────────────────────────
import django  # noqa: E402
django.setup()

# ── Явные импорты для PyInstaller ──────────────────────────────────────────
import rehab_center.wsgi  # noqa: F401, E402
import rehab_center.urls  # noqa: F401, E402
from django.conf import settings  # noqa: E402
import operations.models  # noqa: F401, E402
import operations.api  # noqa: F401, E402
import operations.forms  # noqa: F401, E402
import operations.admin  # noqa: F401, E402
import operations.urls  # noqa: F401, E402
import operations.apps  # noqa: F401, E402
import operations.tasks  # noqa: F401, E402
import operations.views._common  # noqa: F401, E402
import operations.views.schedule  # noqa: F401, E402
import operations.views.appointments  # noqa: F401, E402
import operations.views.balances  # noqa: F401, E402
import operations.views.confirmations  # noqa: F401, E402
import operations.views.consents  # noqa: F401, E402
import operations.views.dashboard  # noqa: F401, E402
import operations.views.documents  # noqa: F401, E402
import operations.views.payments  # noqa: F401, E402
import operations.views.recipients  # noqa: F401, E402
import operations.views.recommendations  # noqa: F401, E402
import operations.views.reports  # noqa: F401, E402
import operations.views.scheduling_helpers  # noqa: F401, E402
import operations.views.specialist  # noqa: F401, E402
import operations.views.tomorrow  # noqa: F401, E402

# Третьесторонние библиотеки
import ninja  # noqa: F401, E402
import django_htmx  # noqa: F401, E402
import whitenoise  # noqa: F401, E402
import reportlab  # noqa: F401, E402


# ── Основная логика ────────────────────────────────────────────────────────

def _find_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def main() -> int:
    from django.core.management import call_command

    port = _args.port or _find_free_port()

    try:
        # Миграции
        log.info("Применение миграций...")
        call_command("migrate", "--run-syncdb", verbosity=0)
        call_command("migrate", verbosity=0)

        # Сидирование
        log.info("Загрузка демо-данных...")
        call_command("seed_demo", verbosity=0)

        # ── Сервер ─────────────────────────────────────────────────────────
        url = f"http://127.0.0.1:{port}/"
        if not _args.no_browser:
            webbrowser.open(url)
            log.info(f"Открываем браузер: {url}")

        log.info(f"RMcodex Viewer запущен: {url}")
        log.info("Для остановки нажмите Ctrl+C.")
        print(f"  Админка: {url}admin/")
        print(f"  Логин: admin / admin12345")
        print(f"  Специалисты: specialist1..specialist4 / specialist123")

        def _shutdown(*args):
            log.info("Завершение...")
            if using_pg:
                _stop_postgres(pg_ctl)
            sys.exit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        call_command("runserver", f"0.0.0.0:{port}", use_reloader=False)
    except Exception:
        log.exception("Критическая ошибка")
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        if using_pg:
            _stop_postgres(pg_ctl)

    return 0


if __name__ == "__main__":
    sys.exit(main())
