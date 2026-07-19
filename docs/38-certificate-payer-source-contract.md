# Certificate Payer Source Contract

Дата: 2026-07-19

Статус: DB-owner срез выполнен

Основание:
- `docs/30-certificate-contract-link-contract.md`
- `docs/24-document-template-source-inventory.md`
- `docs/37-external-signed-file-upload-contract.md`
- интервью: договор подписывает основной представитель; источники оплаты включают личные средства, гранты, фонды, спонсоров и сертификаты.

## Цель

Закрыть оставленный пробел `certificate.payer_name`: договор по сертификату должен брать плательщика и источник из структурной записи сертификата, а не оставлять поле пустым в Word-шаблоне.

## Решение среза

- Добавить к `Certificate` опциональные реквизиты:
  - `funding_source` - источник финансирования, если сертификат привязан к справочнику источников;
  - `payer_representative` - представитель получателя, который выступает плательщиком;
  - `payer_name` - ручное имя плательщика для случаев, где плательщик не заведен как представитель.
- Валидировать, что `payer_representative` относится к тому же получателю, что и сертификат.
- Заполнять Word placeholders `certificate.payer_name`, `certificate.funding_source`, `certificate.payer_relationship`.
- Фиксировать эти реквизиты в `ContractLegalSnapshot.contract_snapshot["certificate"]`.
- Показать плательщика сертификата в реестре договоров с получателями.
- Зарегистрировать `Certificate` в auditlog.

## Не входит

- Списание средств сертификата.
- Автоматическое создание `BalanceAccount`, `Payment` или `LedgerEntry`.
- Перерасчет остатков сертификата.
- Импорт сертификатов из Excel.
- Отдельный пользовательский CRUD сертификатов вне Django admin.
- Автоматическая подстановка подписанта договора как плательщика: если плательщик не задан в сертификате, placeholder остается blank fallback.

## Acceptance criteria

- Existing certificates migrate with empty payer/source fields.
- Certificate validation rejects a payer representative from another child.
- Admin can search certificates by funding source and payer data.
- Service contract list shows certificate payer when it is set.
- Word generation fills `certificate.payer_name` from `payer_name`, then `payer_representative`, then `funding_source`.
- Legal snapshot stores certificate payer/source details.
- No ledger/balance/payment/billing/payroll/grant/schedule/status semantics change.
- Checks: Ruff, Django check, migration dry-run, focused contract/auditlog tests, full pytest, Browser QA for contract list/form.

## Реализация 2026-07-19

- Миграция `operations.0041_certificate_funding_source_certificate_payer_name_and_more` добавляет nullable поля `Certificate.funding_source`, `Certificate.payer_representative`, `Certificate.payer_name` и индексы по получателю/источнику/плательщику.
- `Certificate.clean()` отклоняет представителя-плательщика другого получателя.
- `Certificate.payer_display_name` выбирает явное имя, затем представителя-плательщика, затем источник финансирования; подписант договора автоматически не подставляется.
- Django admin показывает и ищет источник/плательщика сертификата; `Certificate` зарегистрирован в auditlog.
- `/contracts/` показывает плательщика сертификата у договора с получателем, если сертификат связан и плательщик задан.
- Word generation заполняет `certificate.funding_source`, `certificate.payer_name`, `certificate.payer_relationship`; legal snapshot фиксирует источник и плательщика сертификата.
- Проверки: Ruff touched Python и `operations`, Django check, migration dry-run `No changes detected`, focused contract/view/auditlog tests, full pytest `626 passed`, Playwright desktop/mobile QA для `/contracts/`.
- Graphify code-index после среза: `5206` nodes / `22885` edges. Semantic extraction не запускалась; raw `docshablon/` остается ignored/private.
