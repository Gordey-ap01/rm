# Contract Acts Generation

Дата: 2026-07-19

Статус: реализовано 2026-07-19

Основание:
- `docs/24-document-template-source-inventory.md`
- `docs/26-legal-document-targets-and-center-profile-contract.md`
- `docs/27-legal-template-families-contract.md`
- `docs/33-consent-template-generation-contract.md`

## Проблема

В шаблонном контуре уже есть семейство `ContractTemplate.ACT`, но в доменной модели нет акта оказанных услуг. Администратор может сформировать договор, B2B-договор и согласие, но не может зафиксировать юридический акт за период без ручного файла вне системы.

Акт нельзя смешивать с договором:
- договор описывает основание и план услуг;
- акт фиксирует факт предъявления/подписания документа за период;
- акт не должен сам создавать платежи, списания, начисления зарплаты, грантовые факты или занятия.

## Решение первого среза

Добавить отдельную сущность `ContractAct` для актов оказанных услуг:

- акт относится ровно к одному договорному основанию:
  - `ServiceContract`;
  - или `OrganizationServiceContract`;
- акт хранит номер, дату акта, период, сумму, статус, шаблон, документ и комментарий;
- для Word-генерации используются текущие `ContractTemplate` с типом `ACT` или `OTHER`;
- сгенерированный файл сохраняется как `Document(category=act)`;
- для акта с получателем документ должен быть `target_type=recipient` и принадлежать получателю договора;
- для B2B-акта документ должен быть `target_type=counterparty` и принадлежать организации договора;
- при генерации Word акт сохраняет snapshot-поля в самой записи акта: данные акта, договора, центра, получателя, представителя, контрагента, источника финансирования и шаблона.

## Не входит

- Immutable signed archive для актов.
- Отдельная модель `ActSignedFile`.
- Автоматический расчет суммы акта по фактически списанным занятиям.
- Связь акта с `Appointment`, `AppointmentParticipant`, табелем, payroll или грантовыми отчетами.
- Создание `Payment`, `LedgerEntry`, `BalanceAccount` или изменение остатков сертификатов.
- Excel import write-path.
- ЭДО, статусы отправки контрагенту, внешняя подпись.
- Изменение уже реализованных договорных snapshot/архивов.

## Acceptance criteria

- `Document.Category.ACT` добавлен без изменения существующих документов.
- `ContractAct` валидирует ровно один target-договор и соответствие `act_kind`.
- `ContractAct` валидирует порядок дат периода и положительную сумму, если сумма указана.
- `ContractAct` принимает только шаблоны `ContractTemplate.ACT` или `OTHER`.
- Акт с получателем не может ссылаться на документ другого получателя или документ контрагента.
- B2B-акт не может ссылаться на документ получателя или документ другой организации.
- `/contracts/` показывает отдельный блок актов и кнопку создания акта.
- POST `Word` создает/обновляет `Document(category=act)`, привязывает его к акту и заполняет `act.*`, `center.*`, `contract.*`, `child.*`, `representative.*`, `counterparty.*`, `funding_source.*`, `service_spec.*`.
- Повторная генерация Word переиспользует тот же `Document` акта и обновляет snapshot-поля акта.
- Финансы, расписание, гранты, табели, статусы занятий и import write-path не меняются.
- Проверки: Ruff touched Python, Django check, migration dry-run, focused model/service/view tests, full pytest, Browser QA desktop/mobile для блока актов.

## Доменная модель

`ContractAct`
- `act_kind`: `service` или `organization_service`;
- `service_contract`: nullable FK `ServiceContract`, `PROTECT`;
- `organization_contract`: nullable FK `OrganizationServiceContract`, `PROTECT`;
- `number`: номер акта;
- `act_on`: дата акта;
- `period_from`, `period_until`: период оказания услуг по акту;
- `amount`: сумма по акту, nullable;
- `status`: `draft`, `issued`, `signed`, `cancelled`;
- `template`: nullable FK `ContractTemplate`, `SET_NULL`;
- `document`: nullable FK `Document`, `SET_NULL`;
- snapshot JSON-поля для воспроизводимости последней генерации Word.

Опасные миграции:
- не расширять `ContractLegalSnapshot` актами в этом срезе: текущая модель договорная и защищена constraints;
- не делать общий `LegalDocumentSnapshot` миграцией-заменой;
- не связывать акт с фактом занятия или ledger до отдельного контракта.

## План реализации

1. Additive DB-модель и миграция.
2. Admin/auditlog registration.
3. `ContractActForm` с фильтрами шаблонов и документов.
4. Word generation/save helpers для акта.
5. `/contracts/acts/new/`, `/contracts/acts/<id>/edit/`, `/contracts/acts/<id>/word/`.
6. Блок актов в `/contracts/`.
7. Тесты модели, формы, генерации Word и view.
8. Browser QA и recovery update.

## Агентские правила

- Срез делает один DB-owner, потому что меняются `operations/models.py` и migration chain.
- Параллельным агентам можно отдавать только read-only review или подготовку `.docx`-шаблонов вне git.
- Нельзя одновременно менять финансовые сервисы, расписание, billing, payroll или import write-path.

## Implementation 2026-07-19

Выполнен первый срез `contract-acts-generation`:

- migration `operations.0038_alter_document_category_contractact`;
- `Document.Category.ACT`;
- модель `ContractAct` с target-договором `ServiceContract` или `OrganizationServiceContract`;
- валидация шаблона акта, владельца документа, периода, суммы и exactly-one target contract;
- admin/auditlog registration;
- `ContractActForm` с фильтрами act-шаблонов и документов по выбранному договору;
- Word generation/save helpers в `operations.services.contract_documents`;
- `/contracts/acts/new/`, `/contracts/acts/<id>/edit/`, `/contracts/acts/<id>/word/`;
- отдельный блок актов в `/contracts/`.

Проверено:

- Ruff touched Python/migration;
- Django check;
- migration dry-run `No changes detected`;
- focused contract/view tests `62 passed`;
- full pytest `612 passed`;
- Playwright desktop/mobile QA for acts list and Word-triggered document link;
- Graphify code-index `5086` nodes / `22128` edges.
