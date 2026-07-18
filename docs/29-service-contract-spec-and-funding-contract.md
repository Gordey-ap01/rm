# Service Contract Specification and Funding

Дата: 2026-07-18

Статус: DB-owner срез выполнен

Основание:
- `docs/24-document-template-source-inventory.md`
- `docs/25-template-placeholder-expansion-v2-contract.md`
- `docs/27-legal-template-families-contract.md`
- `docs/28-representative-child-legal-fields-contract.md`

## Цель

Договоры с получателем в реальных шаблонах требуют структурную спецификацию услуг: наименование услуги, количество, единица, цена, сумма, период оказания и источник финансирования договора. Сейчас `ServiceContract` хранит только шапку договора, подписанта, шаблон и файл, а плейсхолдеры `service_spec.*` и `funding_source.*` для сервисного договора остаются пустыми.

## Решение первого среза

- Добавить к `ServiceContract` опциональный `funding_source` как договорную метку источника финансирования.
- Добавить строки спецификации `ServiceContractLine` с услугой, наименованием для договора, количеством, единицей, ценой, периодом, сортировкой и примечанием.
- Считать сумму строки как `quantity * unit_price`; в первом срезе не делать автоматические проводки и не связывать строку напрямую с `BalanceAccount`.
- Дать администратору редактировать строки на странице договора с получателем через inline formset.
- Заполнять Word placeholders `service_spec.*` и `funding_source.*` из договора и строк спецификации.
- Фиксировать строки спецификации и источник в `ContractLegalSnapshot` при Word generation.

## Не входит

- Создание/пополнение/списание `BalanceAccount`, `LedgerEntry` или `Payment` из договора.
- Импорт договоров с записью в БД.
- Автоматическое создание программ занятий или расписания из строк договора.
- Сертификат/материнский капитал как отдельная сущность с остатком.
- B2B-договоры, согласия, акты и immutable signed-file archive.
- Изменение payroll, грантовых фактов, статусов занятий или правил списания.

## Доменный контракт

- `ServiceContract.funding_source` nullable, `PROTECT`, related name `service_contracts`, допускает договоры без источника на этапе черновика.
- `ServiceContractLine.service_contract` `CASCADE`: строка является частью договора и удаляется вместе с черновой записью договора.
- `ServiceContractLine.service` `PROTECT`: строка ссылается на справочник услуг, но хранит `service_name` как юридическое наименование для документа.
- `quantity > 0`, `unit_price >= 0`, даты строки упорядочены.
- `sort_order` задает порядок вывода в документе; при равенстве порядок по `pk`.
- Сумма договора в первом срезе вычисляется read-only как сумма строк, а не хранится отдельным полем.

## Acceptance criteria

- Existing service contracts migrate with empty funding source and zero specification lines.
- Admin can create/edit a service contract with one or more specification lines.
- Contract list shows short summary of specification and computed amount for service contracts.
- Word generation fills `funding_source.*`, `contract.amount`, `service_spec.rows`, and first-line `service_spec.service_name`, `quantity`, `unit`, `hours`, `price`, `amount`, `period`.
- `ContractLegalSnapshot.contract_snapshot` or a dedicated spec snapshot contains service lines and computed totals.
- Blank/missing spec fields still render as `_______________`.
- No ledger/balance/payment/billing/payroll/grant/schedule/status semantics change.
- Checks: Ruff, Django check, migration dry-run, focused contract/model/view tests, full pytest, Browser QA for service contract form/list and Word generation.

## Риски

- Не смешивать договорную сумму с фактическими списаниями: договор может быть планом/юридическим основанием, а ledger остается источником финансового факта.
- Не пытаться в этом срезе привязать строки к каскадам или расписанию: это отдельный контракт после стабилизации договорной модели.
- При генерации Word из нескольких строк нужен простой текстовый вывод, совместимый с текущим placeholder engine; настоящие Word-таблицы требуют отдельного шаблонного механизма.

## Реализация 2026-07-18

- Миграция `operations.0033_servicecontractline_servicecontract_funding_source_and_more` добавляет `ServiceContract.funding_source` и новую таблицу `ServiceContractLine`.
- `ServiceContractLine` хранит услугу, юридическое наименование услуги, количество, единицу, цену, период, порядок и примечание; сумма строки вычисляется как `quantity * unit_price`.
- `ServiceContractForm` фильтрует источники финансирования и сохраняет строки через `ServiceContractLineFormSet` атомарно вместе с договором.
- Пустые extra-строки formset с default unit не считаются измененными, чтобы реальный браузерный submit не ломался из-за пустой второй строки.
- `/contracts/` показывает источник финансирования, краткую спецификацию и вычисленную сумму договора.
- Word generation заполняет `funding_source.*`, `contract.amount`, `service_spec.rows` и поля первой строки `service_spec.*`; `ContractLegalSnapshot` фиксирует источник, строки спецификации и сумму.
- Проверки: Ruff, Django check, migration dry-run `No changes detected`, focused contract tests `40 passed`, full pytest `588 passed`, Python Playwright desktop/mobile QA для формы и реестра договоров; артефакты `%TEMP%\rmcodex-browser-qa-service-contract-spec`.
