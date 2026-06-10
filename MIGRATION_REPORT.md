# Миграционный паспорт проекта RMcodex

> **Дата:** 2026-06-10  
> **Назначение:** Полная документация проекта для безопасного переноса на другую платформу разработки  
> **Проект:** Операционная система реабилитационного центра (расписание, балансы, отчёты)

---

## 1. Архитектурный паспорт

### 1.1 Общая архитектура

```
┌─────────────────────────────────────────────────────────────┐
│                    Django 5.2 LTS (WSGI)                     │
├─────────────────────────────────────────────────────────────┤
│  rehab_center/         ← проект-конфиг (settings, urls)     │
│  operations/           ← единственное приложение (все домены)│
├─────────────────────────────────────────────────────────────┤
│  Django Ninja API      ← FullCalendar JSON, справочники      │
│  Django Views (FBV)    ← HTMX-шаблоны с серверным рендером  │
│  django-tasks          ← фоновая отправка email             │
│  django-auditlog       ← аудит изменений на 13 моделях      │
├─────────────────────────────────────────────────────────────┤
│  services/             ← бизнес-логика (изолирована от views)│
│  forms.py              ← ModelForms для всех CRUD           │
│  api.py                ← Ninja Schemas + эндпоинты          │
│  templates/            ← 28 HTML (Django Templates)          │
│  static/               ← FullCalendar, app.css              │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 Ключевые архитектурные решения

| Решение | Обоснование |
|---|---|
| **Одно приложение `operations`** | Все доменные модели живут в одном Django-приложении. Это упрощение для MVP — нет разделения на bounded contexts |
| **Services слой** | Бизнес-логика вынесена в `operations/services/` (6 файлов). Views вызывают services, services работают с моделями |
| **Function-Based Views** | Все view — функции, никаких Class-Based Views. Это сделано намеренно для простоты |
| **HTMX вместо SPA** | Динамика на клиенте через HTMX 1.9 (атрибуты `hx-*` в HTML). Без React/Vue. JS — только FullCalendar |
| **Ninja API только для FullCalendar** | REST API (Ninja) нужен исключительно для подгрузки данных в календарь. Все остальные страницы — серверный рендеринг |
| **django-tasks вместо Celery** | Для MVP выбран лёгкий django-tasks с DatabaseBackend. Redis/Celery описаны в compose.yaml, но не используются |
| **Soft Delete через миксин** | `SoftDeleteMixin` + кастомный `QuerySet`/`Manager`. Работает через поле `archived_at`, а не глобальный фильтр |
| **auditlog на 13 моделях** | Отслеживание изменений через `django-auditlog`. Регистрация моделей — в `apps.py:ready()` |
| **PyInstaller для портативки** | Отдельная сборка `scripts/build_viewer.py` + `scripts/launcher.py` для запуска EXE без Python |

### 1.3 Схема связей модулей

```
┌──────────────┐     ┌──────────────────┐     ┌───────────────────┐
│  urls.py      │────▶│  views/          │────▶│  services/         │
│  (маршруты)   │     │  (контроллеры)   │     │  (бизнес-логика)   │
└──────────────┘     └────────┬─────────┘     └────────┬──────────┘
                              │                        │
                              ▼                        ▼
                      ┌──────────────┐     ┌───────────────────┐
                      │  templates/   │     │  models.py         │
                      │   (HTML)      │     │  (ORM-модели)      │
                      └──────────────┘     └────────┬──────────┘
                                                    │
                              ┌─────────────────────┼──────────────┐
                              │                     │              │
                              ▼                     ▼              ▼
                      ┌──────────────┐     ┌──────────────┐  ┌────────┐
                      │  admin.py    │     │  api.py      │  │  DB    │
                      │  (админка)   │     │  (Ninja API) │  │(PG/SQL)│
                      └──────────────┘     └──────────────┘  └────────┘
```

### 1.4 Паттерны

- **Service Layer** — бизнес-логика в `services/` (appointments, billing, scheduling, reports, notifications, pdf)
- **Repository (QuerySet)** — кастомные `QuerySet`/`Manager` для soft-delete
- **Dataclass DTO** — `@dataclass(frozen=True)` для передачи данных между слоями (TomorrowOverview, GrantReport, ConflictReport, MoveResult)
- **Atomic transaction** — `@transaction.atomic` на всех мутирущих service-функциях
- **FBV + HTMX** — Function-Based Views с HTMX-атрибутами в шаблонах

---

## 2. Спецификация стека

### 2.1 Production-зависимости (pyproject.toml)

| Пакет | Версия | Назначение |
|---|---|---|
| Python | >=3.11, рекомендовано 3.12 | Язык |
| Django | 5.2.14 | Веб-фреймворк |
| psycopg[binary] | 3.3.4 | PostgreSQL драйвер |
| gunicorn | 23.0.0 | WSGI-сервер (Linux) |
| python-dotenv | 1.0.1 | .env загрузка |
| dj-database-url | 2.2.0 | DATABASE_URL парсинг |
| whitenoise | 6.8.2 | Статика в production |
| django-auditlog | 3.0.0 | Аудит изменений |
| django-tasks | 0.6.0 | Фоновые задачи |
| django-htmx | 1.27.0 | HTMX middleware |
| django-ninja | >=1.6.0 | REST API |
| reportlab | 4.5.1 | PDF-генерация |

### 2.2 Dev-зависимости

| Пакет | Версия |
|---|---|
| pytest | 8.3.4 |
| pytest-django | 4.9.0 |
| pytest-cov | 6.0.0 |
| factory-boy | 3.3.1 |
| freezegun | 1.5.1 |
| ruff | 0.8.4 |
| mypy | 1.13.0 |
| django-stubs | 5.1.3 |
| pre-commit | 4.0.1 |
| pyinstaller | 6.12.0 |
| pyinstaller-hooks-contrib | 2024.11 |

### 2.3 Системные зависимости

- **База данных:** PostgreSQL 16+ (продакшн) / SQLite 3 (демо)
- **Docker:** compose.yaml поднимает web + PostgreSQL 17 + Redis 7
- **Redis:** Описан в compose.yaml, но НЕ используется в коде (задел на будущее)
- **ОС:** Linux (Docker/prod) + Windows (портативная сборка)

### 2.4 Фронтенд-зависимости

- **FullCalendar 6** — локально в `static/operations/fullcalendar/index.global.min.js` (276 KB, не CDN)
- **HTMX 1.9** — через django-htmx (шаблон входит в whitenoise статику)
- **Django Admin** — стандартный статический набор
- **No build step** — никаких npm/webpack/vite

### 2.5 Переменные окружения (.env)

| Переменная | Значение по умолчанию | Обязательная |
|---|---|---|
| `DJANGO_SECRET_KEY` | `dev-local-change-before-real-use` | Да (продакшн) |
| `DJANGO_DEBUG` | `1` | Да |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1,0.0.0.0,testserver` | Да |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `http://localhost:8000,...` | Да |
| `DATABASE_URL` | `postgres://rehab:...@localhost:5432/rehab_center` | Нет (если пусто — SQLite) |
| `EMAIL_BACKEND` | `console.EmailBackend` | Да |
| `DEFAULT_FROM_EMAIL` | `rehab-center@example.local` | Да |
| `EMAIL_HOST` / `EMAIL_PORT` | `""` / `587` | Для SMTP |
| `EMAIL_USE_TLS` | `1` | Для SMTP |
| `EMAIL_HOST_USER` / `PASSWORD` | `""` | Для SMTP |
| `DJANGO_SECURE_SSL_REDIRECT` | `0` | Для продакшна |
| `DJANGO_SECURE_HSTS_SECONDS` | `0` | Для продакшна |

---

## 3. Карта данных

### 3.1 Полный список моделей (20 шт.)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        ЯДРО (soft-delete: 8 моделей)                        │
├──────────────┬──────────┬──────────────────────────────────────────────────┤
│ ParentGuardian │ 95      │ Представители получателей                        │
│ Child          │ 128     │ Получатели услуг                                 │
│ StaffMember    │ 166     │ Специалисты (User → OneToOne)                    │
│ Service        │ 198     │ Услуги (категории, цены)                        │
│ Room           │ 225     │ Помещения (кабинет/зал/группа)                  │
│ FundingSource  │ 249     │ Источники финансирования                        │
│ BalanceAccount │ 285     │ Счета балансов (деньги/сессии)                  │
└──────────────┴──────────┴──────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│                   ОПЕРАЦИОННЫЕ (без soft-delete: 12 моделей)                 │
├──────────────┬──────────┬──────────────────────────────────────────────────┤
│ Appointment    │ 442     │ Занятия (основная сущность, 8 статусов)          │
│ AppointmentSeries │ 352   │ Серии занятий (регулярные по ДН)                │
│ LedgerEntry    │ 572     │ Проводки по балансам (двойная запись)           │
│ Note           │ 621     │ Заметки (journal — полиморфные FK)              │
│ AppointmentConfirmation │ 660 │ Подтверждения (токен, email, ответ)        │
│ StaffAvailability │ 724   │ Окна доступности специалиста                    │
│ TimeOffRequest │ 759     │ Заявки на отпуск/отгул                          │
│ Recommendation │ 808     │ Рекомендации (с acknowledge)                     │
│ Document       │ 879     │ Документы (FileField → media/)                  │
│ Consent        │ 931     │ Согласия (personal_data, photo_video, ...)      │
│ Payment        │ 976     │ Платежи (пополнения счетов)                     │
│ Discount       │ 1018    │ Скидки (по ребёнку или услуге)                  │
│ Certificate    │ 1040    │ Сертификаты (маткапитал, региональные)          │
└──────────────┴──────────┴──────────────────────────────────────────────────┘
```

### 3.2 Ключевые связи

```
                    ┌──────────────┐
                    │  User (auth) │
                    └──────┬───────┘
                           │ 1:1
                    ┌──────▼───────┐
         ┌─────────│  StaffMember  │◄───────────┐
         │         └──────┬───────┘             │
         │                │                     │
         │         ┌──────▼───────┐             │
         │         │  Appointment │───► AppointmentSeries
         │         └──┬──┬──┬──┬──┘             │
         │            │  │  │  │                │
         │    ┌───────┘  │  │  └──────────┐     │
         ▼    ▼          ▼  ▼             ▼     ▼
    ┌────────┐  ┌────────┐  ┌──────┐  ┌────────┐
    │ Child  │  │ Service│  │ Room │  │Balance │
    │        │  │        │  │      │  │Account │
    └───┬────┘  └────────┘  └──────┘  └───┬────┘
        │                                  │
        │ M:1                              │ M:1
        ▼                                  ▼
  ┌───────────┐                    ┌──────────────┐
  │ParentGuard│                    │FundingSource  │
  │  ian      │                    │               │
  └───────────┘                    └──────────────┘
                                          │
                                    ┌─────▼─────┐
                                    │ LedgerEntry│
                                    │ Payment    │
                                    └───────────┘
```

### 3.3 Статусные автоматы

**Appointment.status (8 состояний):**
```
draft ──→ proposed ──→ confirmed ──→ completed
              │            │              │
              │            ├──→ no_show ──┤
              │            │              │
              └──────┬─────┘              │
                     ▼                    ▼
               rescheduled ←─── cancelled
                    │
                    └──→ (source_appointment → новое Appointment)
```

**Appointment.billing_decision:** `undecided → charge | do_not_charge`

**BalanceAccount.status:** `active → paused | exhausted | expired`

### 3.4 Мягкое удаление

**SoftDelete (archived_at):** ParentGuardian, Child, StaffMember, Service, Room, FundingSource, BalanceAccount
- `qs.alive()` — только не удалённые (менеджер по умолчанию)
- `qs.dead()` — только удалённые
- `qs.delete()` — мягкое (ставит archived_at)
- `qs.hard_delete()` — физическое
- `instance.restore()` — снимает archived_at

**Жёсткое удаление:** AppointmentSeries, Appointment, LedgerEntry, Note, AppointmentConfirmation, StaffAvailability, TimeOffRequest, Recommendation, Document, Consent, Payment, Discount, Certificate

---

## 4. Точки входа и API

### 4.1 Маршруты (root urls.py)

```
HTTP-метод  │  Путь                     │  View-функция
────────────┼───────────────────────────┼──────────────────────────────
GET         │  /                        │  dashboard
GET         │  /admin/                  │  Django Admin
GET/POST    │  /login/                  │  auth_views.LoginView
GET         │  /logout/                 │  auth_views.LogoutView
            │  /api/                    │  Ninja API (см. 4.2)
            │  /*                       │  operations.urls (см. 4.3)
```

### 4.2 Ninja API (operations/api.py)

```
GET    /api/appointments/?start=&end=          → FullCalendar события
PATCH  /api/appointments/{pk}/move/            → Перенос (drag & drop)
GET    /api/staff/                             → Специалисты (цвета)
GET    /api/rooms/                             → Помещения
GET    /api/unavailability/?start=&end=        → Недоступность (отпуска)
GET    /api/services/                          → Услуги
GET    /api/discounts/                         → Скидки
GET    /api/certificates/                      → Сертификаты
```

- Swagger docs: `/api/docs`
- Все эндпоинты — публичные (без auth-декоратора; в production нужна защита)

### 4.3 HTML-маршруты (operations/urls.py)

```
GET     /                                    → dashboard
GET     /schedule/                           → schedule.html (FullCalendar)
GET     /work-queue/                         → work_queue (HTMX)
GET     /tomorrow/                           → tomorrow_overview
GET     /balances/                           → balances
GET/POST /recipients/new/                    → recipient_create
GET/POST /recipients/<id>/edit/              → recipient_edit
GET     /recipients/<id>/                    → recipient_detail
GET     /recipients/<id>/contract/           → recipient_contract_pdf
GET/POST /representatives/new/               → representative_create
GET/POST /balance-accounts/new/             → balance_account_create
GET/POST /appointments/new/                  → appointment_create
GET/POST /appointments/<id>/edit/            → appointment_edit
GET     /appointments/<id>/                  → appointment_detail
GET/POST /appointments/<id>/move/            → appointment_move
GET/POST /appointments/<id>/cancel/          → appointment_cancel
GET/POST /appointments/<id>/billing/         → appointment_billing
GET/POST /appointments/<id>/confirmations/send/ → send_confirmation
GET     /confirmations/<uuid:token>/         → public confirmation page
GET     /specialist/                         → specialist_home
GET/POST /specialist/appointments/<id>/mark/ → mark_appointment
GET/POST /staff/<id>/timesheet/              → staff_timesheet
GET/POST /staff/<id>/mass-reschedule/        → staff_mass_reschedule
GET     /grants/                             → grant_report
GET     /grants/<id>/                        → grant_report (by funding)
GET/POST /recommendations/new/               → recommendation_create
GET/POST /documents/new/                     → document_create
GET/POST /consents/new/                      → consent_create
GET/POST /payments/new/                      → payment_create
```

### 4.4 Точка входа приложения

```
manage.py  ──────────▶  rehab_center/wsgi.py
                        rehab_center/settings.py
                        rehab_center/urls.py
                            ├── operations/urls.py
                            └── operations/api.py

Портативная сборка:
scripts/launcher.py  ──▶  django.setup()
                          call_command("migrate")
                          call_command("seed_demo")
                          call_command("runserver")
```

---

## 5. Скрытые зависимости и логика

### 5.1 Фоновые задачи (django-tasks)

- **`send_appointment_confirmation_email(confirmation_id)`** — асинхронная отправка email-подтверждения
- Бэкенд: `DatabaseBackend` (таблица `django_taks_dbtaskresult`)
- Запуск воркера: `python manage.py db_worker` или `python manage.py drain_tasks --once`
- В `settings_viewer.py` задач нет (eager-режим, так как django-tasks не установлена)

### 5.2 Аудит (django-auditlog)

Регистрация 13 моделей в `operations/apps.py:ready()`:

```python
auditlog.register(model, exclude_fields=["updated_at", "created_at"])
```

Модели: Child, ParentGuardian, StaffMember, Service, Room, FundingSource, BalanceAccount, Appointment, Recommendation, Document, Consent, Payment, TimeOffRequest

### 5.3 PDF-генерация

- **`services/pdf.py`** — создаёт договор через `reportlab` (A4, Helvetica, табличная вёрстка)
- Поля: представитель, получатель, дата, 12 пунктов
- Вызывается через `recipient_contract_pdf` — HTTP-ответ с `Content-Type: application/pdf`
- **Все шрифты — Helvetica** (стандартный для PDF, кириллицу не поддерживает). **Это баг — кириллические буквы отображаются кракозябрами**

### 5.4 Валидация расписания

- `Appointment.save(validate_schedule=True)` вызывает `full_clean()` → `_validate_no_overlap()`
- Проверка пересечений: `services/scheduling.py:find_overlaps()` — child, staff_member, room одновременно
- `is_within_availability()` — проверка окон доступности и отпусков

### 5.5 Двойная запись (Ledger)

- `LedgerEntry.amount` — положительная для CREDIT, отрицательная для DEBIT
- `BalanceAccount.current_balance` = `initial_amount + SUM(ledger_entries.amount)`
- Списание через `apply_decision()`: меняет `billing_decision` + создаёт `LedgerEntry`
- Переводы: пара `LedgerEntry` (DEBIT + CREDIT) с типом `TRANSFER`
- `_default_amount_for()`: для SESSIONS → -1; для MONEY → `-service.default_price`

### 5.6 Загрузка файлов (Documents)

- Поле `file = FileField(upload_to=document_upload_path)` — `media/documents/<child_id>/<filename>`
- Пример: `media/documents/6/IMG_4112.jpg`
- **Без защиты от Path Traversal** — `document_upload_path` использует `child_id` из модели, но не санирует имя файла

### 5.7 Публичные ссылки

- `confirmations/<uuid:token>/` — публичная страница подтверждения занятия (без авторизации)
- Позволяет подтвердить/отклонить занятие по токену
- **Нет ограничения на количество попыток** — можно брутфорсить UUID (низкий риск, но стоит помнить)

### 5.8 Docker-сборка

- `Dockerfile` — копирует всё через `COPY . .`, включая `.venv-test`, `dist`, `media`, `docs`
- `.dockerignore` **отсутствует** — image будет >2GB
- `compose.yaml` включает Redis, но Redis не используется в коде

### 5.9 Портативная сборка (PyInstaller)

- `scripts/build_viewer.py` — собирает EXE через PyInstaller
- `scripts/launcher.py` — точка входа: определяет директории, запускает портативный PostgreSQL, Django setup, миграции, сидирование, runserver
- `rehab_center/settings_viewer.py` — отдельный файл настроек для портативной версии
- **PostgreSQL портативный** — скачивается `postgresql-16.4-1-windows-x64-binaries.zip` с enterprisedb.com
- `--db sqlite` — если PostgreSQL не нужен

---

## 6. Список технического долга

### 🔴 Критические (исправить до продакшна)

| # | Проблема | Где | Описание |
|---|---|---|---|
| 1 | **Нет авторизации на API** | `api.py` | Все Ninja-эндпоинты публичные. Любой может читать/менять расписание |
| 2 | **Path Traversal в Document** | `models.py:document_upload_path` | `upload_to` формируется из `instance.child_id`, но имя файла не санируется |
| 3 | **Helvetica ≠ кириллица** | `services/pdf.py` | В PDF договоре кириллица отображается кракозябрами. Нужен шрифт с кириллицей (DejaVu Sans, Noto) |
| 4 | **SECRET_KEY в .env hardcoded** | `.env` | Параметр `dev-local-change-before-real-use` — очевидная уязвимость, если забыть поменять |

### 🟡 Средние

| # | Проблема | Где | Описание |
|---|---|---|---|
| 5 | **Нет .dockerignore** | Корень | Docker image включает .venv-test (500MB+), dist, media, docs |
| 6 | **Огромный Docker image** | Dockerfile | `COPY . .` без .dockerignore, pip install без multistage |
| 7 | **django-tasks DatabaseBackend без очистки** | `tasks.py` | Таблица `DBTaskResult` растёт бесконечно. Нужен management command очистки |
| 8 | **seed_demo использует `get_or_create` без учёта изменений** | `seed_demo.py` | При повторном запуске пропускает существующие записи, даже если данные обновились |
| 9 | **Одно приложение `operations`** | Весь проект | Все модели в одном файле (44 KB), все view в одной папке. Для роста нужно разделение на домены |
| 10 | **Нет интеграционных тестов на API** | `tests/` | Ninja-эндпоинты не тестируются (только unit-тесты services + view) |

### 🟢 Низкие / Косметические

| # | Проблема | Где | Описание |
|---|---|---|---|
| 11 | **`tests.py` (legacy) и `tests/` (раздельные)** | Корень | Две системы тестов: старый `tests.py` и новый `tests/`. `pyproject.toml` ищет оба |
| 12 | **Нет `.gitignore` для `data/`** | Корень | SQLite БД (`data/rehab.sqlite3`) включена в репозиторий |
| 13 | **Русские комментарии + кириллица в коде** | Весь проект | `Сообщение`, `����������` — смесь кодировок. ruff настроен игнорить RUF001-003 |
| 14 | **`rehab_center/apps.py` не существует** | Ожидаемо | `operations/apps.py` — есть, а `rehab_center` не объявлен как App. Это нормально, но путает |
| 15 | **django_filters установлен не используется** | `settings_viewer.py` | Включен в INSTALLED_APPS, но нигде не применяется |
| 16 | **Auditlog middleware** | `settings.py` | `auditlog.middleware.AuditlogMiddleware` в MIDDLEWARE, но не используется (регистрация через registry) |
| 17 | **manage.py простая обёртка** | Корень | `execute_from_command_line(sys.argv)` — без проверок, без dev/prod переключения |
| 18 | **Нет типа для `request: Any`** | `services/` | Параметр `request: Any` в сигнатурах (должен быть `HttpRequest`) |

### 📋 Чек-лист для миграции

- [ ] Перенести все файлы проекта (см. дерево файлов в документе)
- [ ] Установить Python 3.11+ и зависимости из `pyproject.toml` (production + dev)
- [ ] Настроить `.env` с новыми значениями (особенно `SECRET_KEY`)
- [ ] Создать PostgreSQL БД (или оставить SQLite для разработки)
- [ ] `python manage.py migrate`
- [ ] `python manage.py seed_demo` (или своя загрузка данных)
- [ ] `python manage.py collectstatic`
- [ ] `python manage.py runserver 0.0.0.0:8000`
- [ ] Проверить тесты: `pytest`
- [ ] Проверить линтер: `ruff check .`
- [ ] Проверить типы: `mypy --install-types; mypy .`
- [ ] Собрать Docker: `docker compose build`
- [ ] Для портативной сборки: `python scripts/build_viewer.py`
