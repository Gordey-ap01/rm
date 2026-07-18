# Legal Template Families

Дата: 2026-07-18

Статус: контракт на небольшой DB-owner срез после snapshot-модели

Основание:
- `docs/24-document-template-source-inventory.md`
- `docs/25-template-placeholder-expansion-v2-contract.md`
- `docs/26-legal-document-targets-and-center-profile-contract.md`

## Проблема

После инвентаризации `docshablon/` стало ясно, что одного типа `recipient_service` недостаточно для реальных договоров с получателями. В исходниках есть платные услуги, безвозмездные услуги за счет пожертвований, присмотр/уход, материнский капитал/сертификат, пожертвования и будущие B2B/согласия/акты.

При этом текущая модель `ContractTemplate` уже используется договорами и UI. Переименовывать ее в общий `DocumentTemplate` или смешивать согласия/акты с текущими договорами одним шагом опасно.

## Решение первого среза

- Расширить только choices `ContractTemplate.TemplateType`.
- Оставить существующие значения БД совместимыми.
- Разрешить текущему `ServiceContract` выбирать только шаблоны, которые юридически соответствуют договору с получателем: платные услуги, безвозмездные услуги, присмотр/уход, материнский капитал/сертификат и `other`.
- Разрешить текущему `DonationContract` выбирать только donation/sponsor/project шаблоны и `other`.
- B2B, согласия и акты можно хранить как типы шаблонов для будущей подготовки, но нельзя привязывать к текущим `ServiceContract`/`DonationContract`, пока нет правильной доменной модели.

## Не входит

- Переименование `ContractTemplate` в `DocumentTemplate`.
- Генерация согласий, актов и B2B-договоров.
- `OrganizationServiceContract`, стороны B2B, плательщик/получатель/представитель-согласующий.
- Immutable signed versions and signature workflow.
- Связи договоров с балансами, платежами, ledger, payroll, grant facts или расписанием.
- Автоматическая конвертация legacy `.doc`.

## Acceptance criteria

- Старые шаблоны `recipient_service`, `donation_one_time`, `donation_monthly`, `sponsor`, `vendor`, `other` остаются валидными.
- Форма шаблона показывает новые семейства.
- `ServiceContract` принимает новые service-family templates и отклоняет B2B/consent/act/vendor templates.
- `DonationContract` принимает project donation template и отклоняет recipient service templates.
- Word generation продолжает работать без изменений placeholder semantics.
- Миграция не меняет финансовые, расписательные, payroll, grant или status semantics.
- Проверки: Ruff, Django check, migration dry-run, focused contract tests, full pytest, Browser QA только если меняется видимая форма/реестр.

## Следующие срезы

1. `template-family-choice-foundation`
   - choices + form/queryset/model validation + UI tests.
2. `representative-child-legal-fields`
   - паспорт/адрес представителя и адрес получателя, один DB-owner.
3. `service-contract-spec-and-funding`
   - строки услуг, количество занятий/часов, цена, источник финансирования договора.
4. `organization-service-contract`
   - отдельная B2B-модель, не перегружать текущий `ServiceContract`.
5. `consent-template-generation`
   - общий шаблон для `Consent`, возможно через будущий `DocumentTemplate`.
6. `legal-acts-and-signed-versions`
   - акты, статусы подписи и immutable file versions.

## Агентские правила

- Этот срез делает один DB-owner, потому что меняются `operations/models.py` и migration chain.
- Параллельным агентам можно отдавать только read-only review или подготовку `.docx`-шаблонов вне git.
- Нельзя одновременно менять `ContractTemplate`, `ServiceContract`, `DonationContract` validation разными агентами.

## Implementation 2026-07-18

Completed `template-family-choice-foundation`:
- migration `operations.0031_alter_contracttemplate_template_type`;
- added template families for recipient free service, care, certificate/maternity-capital, project donation, B2B organization service, photo/video consent and acts while preserving old values;
- `ContractTemplate.service_contract_template_types()` and `donation_contract_template_types()` are the shared allowlists for model validation and forms;
- current `ServiceContract` can use recipient-service families only; current `DonationContract` can use donation/project/sponsor families only;
- B2B, consent and act templates can be cataloged for future preparation but are not selectable by current contract forms;
- no ledger/balance/payment/billing/payroll/grant/schedule/status semantics changed.

Verified:
- Ruff check on touched Python files.
- Django check.
- Migration dry-run `No changes detected`.
- Focused contract tests `33 passed`.
- Full pytest `579 passed`.
- Python Playwright desktop/mobile QA for `/contracts/templates/new/` with artifacts `%TEMP%\rmcodex-browser-qa-template-families`.
- Graphify code-index after this slice: `4810` nodes / `19584` edges; semantic extraction was not rerun.
