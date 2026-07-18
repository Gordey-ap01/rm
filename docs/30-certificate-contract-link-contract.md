# Certificate Contract Link

Дата: 2026-07-18

Статус: контракт на DB-owner срез

Основание:
- `docs/24-document-template-source-inventory.md`
- `docs/25-template-placeholder-expansion-v2-contract.md`
- `docs/27-legal-template-families-contract.md`
- `docs/29-service-contract-spec-and-funding-contract.md`

## Цель

Шаблоны договоров по сертификату/материнскому капиталу требуют реквизиты сертификата: тип, номер, сумма, остаток и срок действия. В системе уже есть `Certificate`, но `ServiceContract` не может ссылаться на него, поэтому плейсхолдеры `certificate.*` остаются пустыми.

## Решение первого среза

- Добавить к `ServiceContract` опциональный `certificate`.
- Фильтровать сертификаты в форме договора по выбранному получателю.
- Валидировать, что выбранный сертификат относится к тому же получателю, что и договор.
- Показывать сертификат в реестре договоров с получателями.
- Заполнять Word placeholders `certificate.type`, `certificate.number`, `certificate.total_amount`, `certificate.remaining_amount`, `certificate.valid_from`, `certificate.valid_until`.
- Фиксировать snapshot сертификата внутри `ContractLegalSnapshot.contract_snapshot`.

## Не входит

- Изменение остатка сертификата при подписании договора или списании занятия.
- Автоматическое создание `BalanceAccount`, `Payment` или `LedgerEntry`.
- Связь сертификата с конкретными строками спецификации.
- Отдельная сущность плательщика/владельца сертификата; `certificate.payer_name` пока остается пустым.
- Импорт сертификатов или договоров с записью в БД.

## Acceptance criteria

- Existing service contracts migrate with empty certificate link.
- Admin can select a certificate for a service contract only from certificates of the selected child.
- Model validation rejects certificate from another child.
- Contract list shows certificate number/type when linked.
- Word generation fills existing `certificate.*` placeholders from the linked certificate.
- `ContractLegalSnapshot.contract_snapshot["certificate"]` stores certificate id/type/number/amounts/dates.
- No ledger/balance/payment/billing/payroll/grant/schedule/status semantics change.
- Checks: Ruff, Django check, migration dry-run, focused contract/model/view tests, full pytest, Browser QA for service contract form/list.

