# РМ-управление — операционная система реабилитационного центра

Локальная операционная система реабилитационного центра: расписание, получатели, представители, специалисты, счета баланса, email-подтверждения, мобильный экран специалиста, табель, грант-отчёт, аудит, soft-delete и фоновые задачи.

## Стек

- **Django 5.2 LTS** — основной фреймворк.
- **SQLite по умолчанию** для локального запуска и демонстрации без Docker, файл `data/rehab.sqlite3`.
- **PostgreSQL 17** как основной серверный бэкенд через `DATABASE_URL`.
- **whitenoise** для раздачи статики в одном контейнере.
- **django-tasks** (Django 5.2 native, `DatabaseBackend`) — фоновые email-отправки.
- **django-auditlog** — журнал изменений по 13 ключевым моделям.
- **django-htmx** — динамические подтверждения без перезагрузки.
- **HTMX 1.9, Bootstrap 5, Bootstrap Icons, Alpine** как локальные static-файлы без CDN.
- **pytest + factory-boy + ruff + mypy + pre-commit** как dev-инструменты.

Локальный стенд запускается одним кликом на Windows **без Docker Desktop**.

## Основные экраны

- `/` — панель администратора (счётчики «требуют внимания»).
- `/tomorrow/` — экран «Завтра» (занятия, ожидают списания, заявки на отпуск, низкие балансы).
- `/schedule/` — расписание на день по специалистам.
- `/work-queue/` — очередь задач администратора.
- `/recipients/`, `/recipients/<id>/` — получатели и их балансы/расписания.
- `/appointments/<id>/` — карточка занятия (списание, подтверждение по email, перенос, отмена).
- `/specialist/` — кабинет специалиста (неделя, отметки, графики, заявки на отпуск).
- `/staff/<id>/timesheet/` — табель специалиста с CSV-выгрузкой.
- `/staff/<id>/mass-reschedule/` — массовый перенос (HTMX-подтверждение).
- `/grants/`, `/grants/<id>/` — грант-отчёт по источнику финансирования.
- `/balances/`, `/payments/new/<account_id>/` — счета и пополнения.
- `/recommendations/`, `/documents/`, `/consents/` — клинические артефакты.

## Быстрый старт

### 1. Демо двойным кликом (Windows)

`START_DEMO.bat` сам:

- проверит, что Python 3.11+ установлен;
- создаст `.venv` и установит проект с dev-зависимостями;
- применит миграции (SQLite создастся в `data/rehab.sqlite3`);
- загрузит демо-данные;
- откроет `http://localhost:8000/`.

На Linux/macOS используйте `./start_demo.sh`.

### 2. Ручной запуск (любая ОС)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate    # Linux/macOS

pip install -e ".[dev]"
python manage.py migrate
python manage.py seed_demo
python manage.py runserver
```

Откройте `http://localhost:8000/`.

### 3. Тестовые логины

- админ: `admin` / `admin12345`
- специалисты: `specialist1`..`specialist4` / `specialist123`

## Переключение на PostgreSQL

SQLite достаточно для демо. Для мини-сервера с PostgreSQL:

```bash
pip install -e ".[postgresql,dev]"
export DATABASE_URL=postgres://rehab:rehab_dev_password@localhost:5432/rehab_center
python manage.py migrate
python manage.py seed_demo
```

`settings.py` автоматически подключит `psycopg[binary]` и `django.contrib.postgres`. Миграция `0004_pg_only_constraints` добавит на уровне БД три EXCLUDE-constraint'а на `child_id`, `staff_member_id` и `room_id` (defense in depth поверх Python-проверки в `Appointment._validate_no_overlap`).

Старый `docker-compose` (`compose.yaml` + `Dockerfile`) сохранён для развертывания на сервере через Docker.

## Удаленная демонстрация

```powershell
.\START_REMOTE_DEMO.bat
```

Поднимет локальный проект и пробросит Cloudflare Quick Tunnel. В окне появится временный URL `https://....trycloudflare.com`, который можно отправить руководителю. Нужен `cloudflared` (`winget install --id Cloudflare.cloudflared`).

## Команды

```bash
# Миграции и демо-данные
python manage.py migrate
python manage.py seed_demo
python manage.py makemigrations  # только если меняли models.py

# Фоновые задачи (django-tasks) — отдельный процесс
# Демо-режим: запустите START_WORKER.bat вторым окном (или START_WORKER.sh на Linux/macOS)
python manage.py db_worker        # стандартный воркер django-tasks (DatabaseBackend)
python manage.py drain_tasks      # пакетный режим: обработать все задачи из очереди и выйти
                                   # удобно для тестов/CI: drain_tasks --once

# Тесты
pytest
pytest --cov

# Линт и типы
ruff check .
ruff format .
mypy rehab_center operations

# Pre-commit
pre-commit run --all-files

# Сборка переносимой demo-папки
python scripts/build_demo.py
# -> dist/RMcodex-demo/ (116 файлов, без data/, .env, .venv, __pycache__, dist/, staticfiles/)
```

## Бэкап демо-данных

SQLite-файл лежит в `data/rehab.sqlite3`. Чтобы сделать бэкап, достаточно скопировать эту папку.

## Сервисный слой

Бизнес-логика вынесена из views в `operations/services/`:

- `appointments` — создание, перенос, отмена, отметка посещения, синхронизация ledger.
- `billing` — решения по списанию, пополнение счетов, перенос между счетами, сверка.
- `scheduling` — поиск конфликтов, свободные слоты, проверка доступности специалиста, массовый перенос.
- `notifications` — формирование и отправка email-подтверждений.
- `reports` — экран «Завтра», табель, грант-отчёт.

## Документы

Основные документы лежат в `docs/`:

- `docs/01-prd.md` — продуктовые требования.
- `docs/02-tech-stack-research.md` — обоснование стека.
- `docs/decisions/ADR-001-*.md`, `ADR-002-*.md` — архитектурные решения.
- `docs/03-ux-ui-and-implementation-plan.md` — UX/UI и план реализации.
- `docs/05-domain-rules-mvp.md` — доменные правила MVP.
- `docs/06-mvp-technical-model.md` — техническая модель.

## Аудит

`django-auditlog` отслеживает изменения в 13 ключевых моделях: `Child`, `ParentGuardian`, `StaffMember`, `Service`, `Room`, `FundingSource`, `BalanceAccount`, `Appointment`, `Recommendation`, `Document`, `Consent`, `Payment`, `TimeOffRequest`. Лог доступен в `/admin/auditlog/logentry/`.

## Подготовка папки для флешки

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Prepare-UsbDemo.ps1
```

Скрипт собирает автономный дистрибутив в `dist\RMcodex-demo`. Скопируйте папку на флешку и запустите `START_DEMO.bat` на целевом ноутбуке — требуется только Python 3.11+ (Docker не нужен).
