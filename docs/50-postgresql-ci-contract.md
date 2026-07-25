# Контракт: CI PostgreSQL 17

Дата: 2026-07-25

Статус: implemented

## Цель

Сделать проверку миграций, exclusion constraints и конкурентных write-path
обязательной для каждого push и pull request. SQLite остается быстрым локальным
контуром, но не единственным доказательством корректности расписания и финансов.

## Workflow

`.github/workflows/ci.yml` запускается на `ubuntu-24.04` и использует
PostgreSQL 17 service container с одноразовой базой `rehab_ci`.

Порядок шагов:

1. checkout и Python 3.12;
2. установка приложения и dev-зависимостей из `pyproject.toml`;
3. `python manage.py migrate --noinput` на чистой PostgreSQL;
4. `check` и `makemigrations --check --dry-run`;
5. Ruff;
6. полный `pytest` с `DATABASE_URL` PostgreSQL.

Контейнер использует только ephemeral CI-учетную запись и `trust` внутри
изолированной сети GitHub runner. Production-пароли, `.env` и персональные
данные не передаются workflow и не требуются.

## Acceptance Criteria

- workflow синтаксически валиден и не хранит секреты;
- свежая PostgreSQL 17 принимает всю migration chain;
- PostgreSQL-only `TransactionTestCase` не пропускаются;
- failure любого этапа останавливает проверку pull request;
- workflow не выполняет deploy, backup или изменение production.

## Первичная валидация

Первый удаленный run обнаружил PostgreSQL-несовместимое блокирование nullable
`LEFT JOIN` в ранее неисполнявшихся SQLite-путях. Исправление ограничивает
`FOR UPDATE` строкой-владельцем либо исключает ненужный join; после него полный
локальный PostgreSQL 17 suite проходит (`705 passed`). Повторный remote run
остается обязательным перед признанием CI-приемки завершенной.

## Границы

CI не заменяет отдельную production-приемку: restore drill, SMTP, мониторинг,
реальные права GitHub branch protection и production financial-integrity run
остаются следующими эксплуатационными задачами.
