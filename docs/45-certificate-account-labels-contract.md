# Certificate Account Labels Contract

Дата: 2026-07-19

Статус: implemented

Основание:
- `docs/42-certificate-balance-ledger-contract.md`
- `docs/44-certificate-balance-backfill-command-contract.md`

## Цель

Сделать счета баланса, созданные из сертификатов, узнаваемыми в операционных местах:
общий список балансов, карточка получателя и выбор счета при списании занятия.

После backfill у администратора не должно быть ситуации, когда денежный счет выглядит
как обычная личная оплата и непонятно, что это остаток сертификата.

## Границы

Разрешено:
- добавить read-only label/marker для `BalanceAccount`, если он связан с `Certificate`;
- показать тип и номер сертификата рядом со счетом;
- добавить колонку/строку "Основание" в таблицы балансов;
- улучшить labels в `ModelChoiceField` для выбора счета списания.

Запрещено:
- менять финансовые расчеты;
- менять `Certificate.remaining_amount`;
- создавать или удалять `BalanceAccount`;
- создавать `LedgerEntry`, `Payment`, payroll, grant, schedule или status facts;
- менять backfill-command behavior;
- добавлять миграции.

## UX Rules

- В таблицах показывать компактную метку `Сертификат` и строку с типом/номером.
- Если номер пустой, показывать `б/н`.
- В селекторе счета использовать тот же смысловой хвост, чтобы при списании было видно
  происхождение счета.
- Не перегружать карточку: это provenance, а не отдельная финансовая книга.

## Acceptance Criteria

- `BalanceAccount` имеет read-only label для связанного сертификата.
- `/balances/` показывает сертификатное основание для linked accounts.
- Карточка получателя показывает сертификатное основание в блоке счетов.
- Поле выбора счета при списании занятия показывает сертификатный marker.
- Нет миграций.
- Тесты покрывают таблицу балансов, карточку получателя и choice label.
- Проверки: Ruff, Django check, migration dry-run, focused tests, full pytest, Browser QA desktop/mobile.

## Implementation Notes

- `BalanceAccount.certificate_link_label` returns the linked certificate type and number, or an empty string for ordinary accounts.
- `BalanceAccount.is_certificate_linked` is a read-only convenience flag for future UI/report filters.
- `BalanceAccount.__str__()` is intentionally unchanged so legacy reports, audit labels and exports keep their previous account text.
- Account selectors in appointment billing/payment forms append `сертификат: ...` only for linked certificate accounts.
- `/balances/` and recipient detail show the same compact `Сертификат` provenance marker.
- Querysets use `select_related("certificate")` where the new marker is rendered to avoid per-row certificate lookups.
