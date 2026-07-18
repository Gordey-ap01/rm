# Organization Service Contract

Дата: 2026-07-18

Статус: реализовано 2026-07-18

Основание:
- `docs/24-document-template-source-inventory.md`
- `docs/26-legal-document-targets-and-center-profile-contract.md`
- `docs/27-legal-template-families-contract.md`
- `docs/29-service-contract-spec-and-funding-contract.md`
- `docs/31-immutable-contract-signed-file-archive-contract.md`

## Проблема

В `ContractTemplate.TemplateType` уже есть семейство `organization_service`, но текущая доменная модель не имеет договора оказания услуг организации. Использовать `ServiceContract` для B2B нельзя: в нем обязательны получатель, представитель и recipient-scoped `Document`. Использовать `DonationContract` тоже нельзя: пожертвование не является договором оказания услуг и не имеет спецификации услуг.

Нужна отдельная структурная запись B2B-договора, чтобы администратор мог вести договор с организацией, сформировать Word, зафиксировать юридический snapshot и архивировать подписанную версию без создания платежей, списаний или занятий.

## Решение первого среза

- Добавить отдельную модель `OrganizationServiceContract`.
- Добавить строки спецификации `OrganizationServiceContractLine`, повторяющие юридическую структуру строк service-договора: услуга, договорное наименование, количество, единица, цена, период, порядок, примечание.
- Расширить `ContractTemplate` allowlist методом `organization_service_contract_template_types()`: доступны `organization_service` и `other`.
- Расширить `ContractLegalSnapshot` третьим типом `organization_service` и nullable FK на `OrganizationServiceContract`.
- Расширить `ContractSignedFile` третьим типом `organization_service` и nullable FK на `OrganizationServiceContract`.
- Word generation B2B-договора создает/обновляет `Document(target_type=counterparty, counterparty=<contract.counterparty>, category=contract)`.
- B2B placeholders используют уже существующие группы `center.*`, `counterparty.*`, `funding_source.*`, `contract.*`, `service_spec.*`.
- `/contracts/` получает отдельный блок B2B-договоров организации с действиями "Открыть", "Word", "PDF", "Зафиксировать".

## Не входит

- Автоматическое создание `Payment`, `LedgerEntry`, `BalanceAccount` или начислений из B2B-договора.
- Привязка B2B-договора к расписанию, получателям, групповым занятиям, грантовым квотам или табелям.
- Акты выполненных услуг.
- Импорт B2B-договоров из Excel.
- Электронная подпись и ЭДО.
- Переписывание текущего `ServiceContract`/`DonationContract` в общую полиморфную модель.

## Доменный контракт

`OrganizationServiceContract`:
- `counterparty` `PROTECT`, обязательный: организация-заказчик/партнер;
- `funding_source` nullable `PROTECT`: договорная метка источника финансирования без финансового факта;
- `contract_type`: `standard`, `project`, `other`;
- `number`, `signed_on`, `valid_from`, `valid_until`, `status`, `template`, `document`, `notes`;
- дата окончания не раньше даты начала;
- уникальность заполненных `contract_type + number + signed_on`;
- `template` только из organization-service allowlist;
- `document` только категории `contract`, не recipient-scoped; если `target_type=counterparty`, он должен относиться к выбранному `counterparty`.

`OrganizationServiceContractLine`:
- `organization_contract` `CASCADE`;
- `service` `PROTECT`;
- `service_name` хранит договорное наименование, чтобы изменение справочника услуг не меняло старый договор;
- `quantity > 0`, `unit_price >= 0`, даты строки упорядочены;
- сумма строки вычисляется как `quantity * unit_price`, сумма договора - сумма строк.

`ContractLegalSnapshot` и `ContractSignedFile`:
- ровно одна contract-ссылка по `contract_kind`: service, donation или organization_service;
- для organization-service snapshot/source document не может быть document получателя;
- если document контрагента, контрагент должен совпадать с договором;
- signed archive остается неизменяемым и не создает финансовых фактов.

## Acceptance criteria

- Администратор может создать/отредактировать B2B-договор с организацией и строками спецификации.
- B2B-договор не требует получателя и представителя.
- Форма B2B показывает только active counterparty, optional funding source, organization-service templates и допустимые contract documents.
- Реестр `/contracts/` показывает B2B-договоры отдельным блоком, фильтр `kind=organization` работает, поиск ищет номер, контрагента, источник, шаблон, документ и строки услуг.
- Word generation для B2B создает/обновляет counterparty `Document`, сохраняет `ContractLegalSnapshot` и не создает financial facts.
- PDF download для B2B остается read-only.
- Архивация подписанной B2B-версии создает `ContractSignedFile(contract_kind=organization_service)` и сохраняет immutable snapshot-copy.
- Нельзя привязать recipient document или template другого семейства.
- Повторная Word generation не меняет старые `ContractSignedFile`.
- Полный pytest проходит; browser QA проверяет desktop/mobile реестр и B2B-форму.

## Реализация 2026-07-18

- Добавлены `OrganizationServiceContract` и `OrganizationServiceContractLine`, migration `operations.0036_organizationservicecontract_and_more`.
- `ContractLegalSnapshot` и `ContractSignedFile` расширены третьим видом `organization_service` с nullable FK на B2B-договор и DB/model constraints "ровно один договор".
- `ContractTemplate.organization_service_contract_template_types()` ограничивает шаблоны для B2B семействами `organization_service` и `other`.
- Добавлены формы, admin registration, auditlog tracking через `operations/apps.py`, routes create/edit/PDF/Word/archive для B2B-договоров.
- Word generation B2B создает/обновляет counterparty `Document(category=contract)`, сохраняет юридический snapshot и поддерживает `center.*`, `counterparty.*`, `funding_source.*`, `contract.*`, `service_spec.*`.
- PDF B2B остается read-only; архив signed file копирует уже generated Word-файл и immutable snapshot-copy.
- `/contracts/` получил отдельный блок B2B-договоров с фильтром `kind=organization`, действиями "Открыть", "Word", "PDF", "Зафиксировать" и ссылкой на подписанный архив.
- Финансы, ledger, платежи, балансы, зарплата, гранты, расписание, сертификатные остатки, акты и import write-path не менялись.
- Проверки: Ruff, Django check, migration dry-run `No changes detected`, focused contract/view tests `55 passed`, full pytest `603 passed`, in-app Browser desktop/mobile QA для B2B-реестра и архива подписанной версии. In-app Browser не поддерживает download events; Word/download routes покрыты Django tests.
- Graphify code-index after this slice: `4996` nodes / `21474` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private.

## Риски

- Расширение `ContractLegalSnapshot` и `ContractSignedFile` третьим типом меняет DB constraints; делать только одним DB-owner агентом.
- Дублирование строк спецификации с `ServiceContractLine` приемлемо в первом срезе, потому что юридические owner-FK разные. Обобщать строки стоит только после стабилизации актов и B2B-сценариев.
- B2B-договор похож на будущие акты, но акты не должны появляться в этом срезе: акт является фактом оказания/приемки, договор - юридическим основанием.

## Агентские правила

- Срез делает один DB-owner агент: меняются `operations/models.py` и migration chain.
- Параллельные агенты допустимы только read-only review или подготовка `.docx` templates вне git.
- `docshablon/` не коммитить и не отправлять в Graphify semantic extraction.
