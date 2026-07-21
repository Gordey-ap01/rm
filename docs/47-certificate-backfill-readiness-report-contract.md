# Certificate Backfill Readiness Report Contract

Дата: 2026-07-21

Статус: implemented

Основание:
- `docs/42-certificate-balance-ledger-contract.md`
- `docs/43-certificate-balance-backfill-preflight-contract.md`
- `docs/44-certificate-balance-backfill-command-contract.md`
- `docs/46-certificate-backfill-readiness-work-queue-contract.md`

## Цель

Добавить отдельный read-only отчет администратора по готовности сертификатов к
созданию счетов баланса. Рабочая очередь остается кратким сигналом, а отчет дает
достаточно деталей для ручной чистки данных и подготовки dry-run/apply backfill на
staging/production.

## Границы

Разрешено:
- переиспользовать существующий `certificate_balance_preflight_report()`;
- добавить общие read-only query helpers для проблем и сертификатов с нулевым остатком;
- показать summary counters, issue rows, duplicate groups, candidates и zero-balance records;
- ссылаться на ручное редактирование сертификата;
- добавить ссылку из `/work-queue/#queue-certificates`;
- добавить regression tests на read-only и маскирование чувствительных данных.

Запрещено:
- запускать backfill из UI;
- создавать `BalanceAccount`;
- создавать `LedgerEntry`;
- менять `Certificate.balance_account`, `remaining_amount`, даты, суммы или funding source;
- показывать raw duplicate certificate numbers;
- показывать ФИО получателя в отчете готовности;
- менять платежи, расписание, гранты, payroll, contracts или statuses;
- добавлять миграции.

## UX Rules

- Отчет строится как утилитарный админский экран: summary cards сверху, затем таблицы деталей.
- Номера сертификатов не выводятся; для ручного разбора используются certificate ID и child ID.
- Кандидаты и zero-balance certificates показываются в таблицах с technical ID, типом, источником и суммами.
- Все действия являются ссылками на существующие ручные экраны; POST forms и apply-кнопок нет.
- На мобильном экране таблицы должны использовать существующий responsive table pattern.

## Acceptance Criteria

- Есть route `/certificates/backfill-readiness/` с именем `certificate_backfill_readiness_report`.
- Страница доступна только администратору.
- GET страницы не создает `BalanceAccount`, `LedgerEntry` и не меняет `Certificate.balance_account`.
- Страница показывает:
  - total/linked/unlinked readiness summary;
  - candidate count;
  - zero-balance count;
  - issue rows по тем же кодам, что preflight;
  - duplicate groups without raw certificate numbers.
- `/work-queue/#queue-certificates` ссылается на подробный отчет.
- Нет миграций.
- Проверки: Ruff, Django check, migration dry-run, focused tests, full pytest, Browser QA desktop/mobile, secret scan, Graphify structural update.

## Implementation Notes

- Добавлен `certificate_balance_preflight_issue_querysets()` как единый источник queryset для preflight и UI.
- Добавлен `certificate_balance_zero_balance_without_account_queryset()`.
- Добавлен view/context `certificate_backfill_readiness_report()`.
- Шаблон `templates/operations/certificate_backfill_readiness_report.html` не содержит backfill POST action.
- Regression tests проверяют read-only, маскирование номеров и отсутствие ФИО получателя в отчете.
