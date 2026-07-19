# Certificate Balance Backfill Command Contract

Дата: 2026-07-19

Статус: implemented on 2026-07-19

Основание:
- `docs/42-certificate-balance-ledger-contract.md`
- `docs/43-certificate-balance-backfill-preflight-contract.md`
- `docs/decisions/ADR-002-balance-accounts-ledger.md`

## Цель

Добавить управляемую операционную команду для создания `BalanceAccount` и opening
`LedgerEntry(CREDIT)` по уже заведенным сертификатам, но сделать запуск безопасным:
по умолчанию команда ничего не пишет, массовая запись требует явного подтверждения,
а найденные проблемы данных блокируют apply без отдельного override.

## Границы

Команда может создавать счет только для сертификата, который:

- не связан со счетом баланса;
- имеет источник финансирования;
- имеет `total_amount >= 0`;
- имеет `remaining_amount > 0`;
- имеет `remaining_amount <= total_amount`;
- не имеет противоречия дат.

Команда не должна:

- менять `Certificate.remaining_amount`;
- создавать `Payment`;
- создавать назначения, занятия, payroll accruals, grant allocations, contracts,
  schedules или status changes;
- исправлять сертификаты без источника финансирования;
- исправлять отрицательные суммы, даты или дубли номеров;
- создавать счета для нулевого остатка в первом apply-срезе;
- добавлять DB constraints;
- переименовывать поля.

## Интерфейс команды

Dry-run по умолчанию:

```powershell
.\.venv-test\Scripts\python.exe manage.py backfill_certificate_balance_accounts
```

Apply требует оба флага:

```powershell
.\.venv-test\Scripts\python.exe manage.py backfill_certificate_balance_accounts --apply --confirm
```

Если preflight нашел проблемы, apply блокируется. Для осознанного частичного запуска
только по валидным кандидатам нужен отдельный флаг:

```powershell
.\.venv-test\Scripts\python.exe manage.py backfill_certificate_balance_accounts --apply --confirm --allow-existing-issues
```

Точечный запуск:

```powershell
.\.venv-test\Scripts\python.exe manage.py backfill_certificate_balance_accounts --certificate-id 10 --certificate-id 11
```

## Поведение

Dry-run:
- считает preflight;
- показывает candidate IDs;
- не создает `BalanceAccount`;
- не создает `LedgerEntry`;
- не меняет сертификаты.

Apply:
- повторно считает preflight перед записью;
- без `--confirm` завершается ошибкой;
- при найденных проблемах завершается ошибкой, если нет `--allow-existing-issues`;
- берет только валидных кандидатов с положительным остатком;
- для каждого кандидата вызывает idempotent `ensure_certificate_balance_account()`;
- создает opening `LedgerEntry(CREDIT)` через существующий сертификатный сервис;
- повторный запуск не создает дублей.

## Acceptance Criteria

- Есть сервисный backfill helper с dry-run/apply режимами.
- Есть management command `backfill_certificate_balance_accounts`.
- Без флагов команда не пишет в БД.
- `--apply` без `--confirm` не пишет в БД.
- Apply с проблемами данных блокируется без `--allow-existing-issues`.
- Apply по валидным кандидатам создает ровно один money `BalanceAccount` и один opening
  `LedgerEntry(CREDIT)` на сертификат.
- Повторный apply идемпотентен.
- Тесты доказывают, что `Payment`, appointments, payroll, grants, contracts, schedules
  и statuses не создаются командой.
- Проверки: Ruff, Django check, migration dry-run `No changes detected`, focused tests,
  full pytest.

## Deferred

- Запуск на production/staging.
- Политика для нулевых остатков.
- Ручная чистка проблемных сертификатов.
- Rename `Certificate.remaining_amount`.
- DB constraints по суммам/датам.
- Уникальность номера сертификата внутри получателя.
