# Representative and Child Legal Fields

Дата: 2026-07-18

Статус: DB-owner срез выполнен

Основание:
- `docs/24-document-template-source-inventory.md`
- `docs/25-template-placeholder-expansion-v2-contract.md`
- `docs/26-legal-document-targets-and-center-profile-contract.md`
- `docs/27-legal-template-families-contract.md`

## Цель

Реальные договоры с получателем требуют паспортные данные и адрес представителя, а также адрес получателя. Сейчас плейсхолдеры `representative.passport_*`, `representative.registration_address` и `child.address` зарезервированы, но остаются пустыми.

## Решение первого среза

- Additive поля в `ParentGuardian`: серия/номер паспорта, кем выдан, дата выдачи, адрес регистрации.
- Additive поля в `Child`: адрес регистрации и адрес проживания.
- Подключить поля в формы представителя и получателя.
- Подставлять эти поля в Word placeholders и `ContractLegalSnapshot`.
- Отображать адреса в карточке получателя без изменения расписания, балансов и программ.

## Не входит

- Хранение сканов паспортов или файлов документов.
- Проверка формата паспорта, ФИАС/КЛАДР, маскирование, шифрование на уровне поля.
- История изменений паспортных данных отдельной таблицей.
- Изменение ролей представителя, рассылок, платежей, балансов, ledger, payroll, грантов, расписания или статусов занятий.
- Генерация B2B, согласий и актов.

## Безопасность данных

- Не коммитить реальные паспортные данные, адреса и персональные данные в тесты, docs или фикстуры.
- Тестовые значения должны быть синтетическими.
- Snapshot фиксирует значения на момент Word generation; изменение карточки представителя/получателя после генерации не меняет уже сохраненный snapshot.

## Acceptance criteria

- Existing recipients/representatives migrate with blank legal fields.
- Admin can edit passport/address fields in existing representative/recipient forms.
- `service_contract_placeholders()` fills `child.address`, `representative.passport_series`, `representative.passport_number`, `representative.passport_issued_by`, `representative.passport_issued_on`, `representative.registration_address`.
- `ContractLegalSnapshot` for service contracts stores the same legal fields.
- Blank fields still render as `_______________`.
- No financial/schedule/payroll/grant/status semantics change.
- Checks: Ruff, Django check, migration dry-run, focused model/view/contract tests, full pytest, Browser QA for recipient/representative forms and contract Word generation if UI changed.

## Реализация 2026-07-18

- Миграция `operations.0032_child_registration_address_child_residential_address_and_more` добавляет только nullable/blank-safe юридические поля без backfill и без изменения существующих правил расписания, финансов, payroll, грантов или статусов занятий.
- `ParentGuardian` хранит `passport_series`, `passport_number`, `passport_issued_by`, `passport_issued_on`, `registration_address`.
- `Child` хранит `registration_address` и `residential_address`; `child.address` в Word placeholder берется из регистрации, затем из проживания.
- `RepresentativeForm` и `RecipientForm` позволяют администратору редактировать новые поля; карточка получателя показывает адрес регистрации и проживания в контактном блоке.
- `service_contract_placeholders()` подставляет паспорт/адрес представителя и адрес получателя. `ContractLegalSnapshot` фиксирует эти значения при Word generation, чтобы будущие изменения карточек не меняли уже сохраненный snapshot.
- Добавлены тесты на Word-подстановки, snapshot payload, сохранение форм получателя/представителя.
- Проверки: Ruff, Django check, migration dry-run `No changes detected`, focused view/contract tests `44 passed`, full pytest `582 passed`, Python Playwright desktop/mobile QA для форм представителя/получателя и карточки получателя; артефакты `%TEMP%\rmcodex-browser-qa-legal-fields`.

## Следующие срезы

- `service-contract-spec-and-funding`: строки услуг, количество, цена, источник финансирования договора.
- `certificate-contract-link`: связь договора с сертификатом/маткапиталом.
- `organization-service-contract`: отдельная B2B-модель.
- `consent-template-generation`: общий шаблон для согласий.
