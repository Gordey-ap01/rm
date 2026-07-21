# Certificate Backfill Readiness Work Queue Contract

Дата: 2026-07-21

Статус: implemented

Основание:
- `docs/42-certificate-balance-ledger-contract.md`
- `docs/43-certificate-balance-backfill-preflight-contract.md`
- `docs/44-certificate-balance-backfill-command-contract.md`
- `docs/45-certificate-account-labels-contract.md`

## Цель

Показать администратору в рабочей очереди готовность сертификатов к backfill счетов
баланса: сколько сертификатов уже связано, сколько можно безопасно связать командой,
какие данные нужно исправить вручную и где есть дубликаты номеров.

Это операционный read-only слой перед запуском backfill на staging/production.

## Границы

Разрешено:
- вызвать существующий read-only `certificate_balance_preflight_report()` на GET рабочей очереди;
- показать счетчики сертификатов, кандидатов, нулевых остатков, проблем и дублей;
- показать sample ID сертификатов для ручного разбора;
- добавить summary tile в рабочую очередь;
- добавить регрессионные тесты, что UI не создает счета/ledger.

Запрещено:
- запускать backfill из UI;
- создавать `BalanceAccount`;
- создавать `LedgerEntry`;
- менять `Certificate.balance_account`;
- менять `Certificate.remaining_amount`;
- исправлять сертификаты, funding source, даты, суммы или дубли автоматически;
- менять `Payment`, appointments, payroll, grants, contracts, schedules или statuses;
- добавлять миграции или DB constraints.

## UX Rules

- Панель должна выглядеть как существующие sections рабочей очереди.
- Счетчики показывать компактно: всего, связано, кандидатов, нулевой остаток, проблем.
- Если есть проблемы данных или дубли, tone `danger`.
- Если проблем нет, но есть кандидаты или нулевые остатки, tone `warning`.
- Если действий нет, tone `success`.
- Sample ID показывать технически, без ФИО и без raw персональных данных.
- UI должен ссылаться на безопасные ручные действия: карточка сертификата/балансы/контракт команды,
  но не иметь POST apply-кнопки.

## Acceptance Criteria

- `/work-queue/` содержит summary item `Сертификаты`.
- `/work-queue/#queue-certificates` показывает preflight counters.
- Проблемы данных отображаются с code/label/count и sample certificate IDs.
- Дубликаты номеров отображаются как технические groups без ФИО.
- GET рабочей очереди не создает `BalanceAccount` или `LedgerEntry`.
- Нет миграций.
- Проверки: Ruff, Django check, migration dry-run, focused tests, full pytest, Browser QA desktop/mobile.

## Implementation Notes

- `/work-queue/` calls `certificate_balance_preflight_report()` with a small sample limit.
- The summary tile `Сертификаты` points to `#queue-certificates`.
- The panel shows readiness counters, issue rows, duplicate groups and candidate IDs.
- Duplicate certificate numbers are intentionally masked in the work queue; only technical recipient id
  and group count are shown.
- Candidate and issue sample IDs link to certificate edit pages for manual admin cleanup.
- The panel has no forms and no POST apply action.
