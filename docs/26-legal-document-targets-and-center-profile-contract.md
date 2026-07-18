# Legal Document Targets and Center Profile

Дата: 2026-07-18

Статус: контракт на DB-owner срез перед миграциями

Основание:
- `docs/24-document-template-source-inventory.md`
- `docs/25-template-placeholder-expansion-v2-contract.md`
- локальные примеры `docshablon/` используются только как приватные источники структуры шаблонов

## Проблема

Текущая модель `Document` обязательна к `Child` и удаляется каскадом вместе с получателем. Это подходит для медицинских заключений, ИПР и согласий получателя, но плохо подходит для:
- договоров пожертвования и спонсорских договоров без получателя;
- документов контрагентов;
- документов расходов центра;
- юридических документов самого центра;
- будущих подписанных snapshot-файлов, которые должны жить независимо от изменения карточек.

Из-за этого Word-файл `ServiceContract` уже может сохраняться как `Document`, а Word-файл `DonationContract` пока только скачивается и не сохраняется в реестр документов.

## Решения

- Этот блок делает один ведущий DB-owner агент. Несколько агентов не должны одновременно менять `operations/models.py` и цепочку миграций.
- В первом срезе не использовать `GenericForeignKey`: для юридически важных документов нужны явные nullable FK и проверяемая модель.
- Расширять `Document` обратно-совместимо: существующие документы получателей остаются валидными.
- Не переносить физические файлы в storage при миграции. Меняется путь только для новых загрузок без получателя.
- Не менять `LedgerEntry`, `BalanceAccount`, `Payment`, billing, payroll, grant, schedule или appointment-status semantics.
- Не коммитить и не отправлять в Graphify semantic extraction raw-файлы из `docshablon/`.

## Целевая модель первого среза

`Document`:
- `target_type`: `recipient`, `center`, `counterparty`, `contract`, `other`.
- `child`: nullable FK на `Child`; для `recipient` обязателен.
- `counterparty`: nullable FK на `Counterparty`; для `counterparty` обязателен.
- `category`, `title`, `file`, `issued_on`, `expires_on`, `uploaded_by`, `note` сохраняются.
- индексы по `target_type/category`, `child/category`, `counterparty/category`.
- model/form validation запрещает пустую обязательную цель для `recipient`, `counterparty`.
- документы расходов центра в первом срезе не получают новый обратный FK: связь владельца уже есть через `CenterExpense.document`.

`document_upload_path`:
- при `child_id` оставляет совместимый путь `documents/<child_id>/<filename>`;
- при `counterparty_id` использует `documents/counterparties/<id>/<filename>`;
- иначе использует `documents/<target_type>/<filename>`.

`DonationContract`:
- Word generation может создать `Document(target_type=counterparty, counterparty=<contract.counterparty>, category=contract)` и связать его через существующее поле `DonationContract.document`;
- PDF download остается read-only и не создает `Document`.

`ServiceContract`:
- Word generation продолжает создавать `Document(target_type=recipient, child=<contract.child>, category=contract)`;
- валидация продолжает запрещать документ другого получателя для договора с получателем.

## Следующие модели после первого среза

`CenterLegalProfile`:
- активная карточка центра с полным/кратким названием, директором, основанием полномочий, лицензией, ОГРН, ИНН, КПП, адресами, контактами и банковскими реквизитами;
- не подставляется в подписанные старые документы задним числом.

`ContractLegalSnapshot` или snapshot-поля договора:
- фиксируют данные центра, контрагента, представителя, получателя и спецификации услуг на момент генерации/подписания;
- нужны до полноценного юридического документооборота, актов и подписанных версий.

### Semantics первого `contract-signed-snapshot` среза

- Snapshot хранится отдельной моделью `ContractLegalSnapshot`, а не JSON-полем на `ServiceContract`/`DonationContract`, чтобы один `Document` имел один проверяемый юридический снимок.
- Связи: `document` один-к-одному, один из FK `service_contract` или `donation_contract` обязателен, второй пустой. Тип договора фиксируется явным `contract_kind`.
- `document`, `service_contract`, `donation_contract` защищены `PROTECT`: после создания snapshot нельзя случайно удалить юридическую связку через админку или shell без явного решения.
- Snapshot создается или обновляется при Word generation. Повторная генерация того же связанного `Document` обновляет snapshot под новый файл.
- Изменение `CenterLegalProfile`, карточки получателя, представителя, контрагента или источника финансирования после генерации не меняет уже сохраненный snapshot и старый файл, пока администратор явно не нажмет Word заново.
- Это еще не полноценная неизменяемая подписанная версия: “каждая подпись = отдельный файл + статус подписи + архив неизменяемых файлов” остается будущим срезом после стабилизации договорных типов и актов.

## Опасные миграции

- `Document.child` из обязательного `CASCADE` становится nullable. Перед изменением поведения удаления надо отдельно решить, какие документы должны сохраняться при архивировании/удалении получателя.
- `Document` уже используется `Consent`, `ServiceContract`, `DonationContract`, `CenterExpense`; изменение constraints может сломать существующие формы, если не обновить validation и queryset.
- Нельзя добавлять двусторонние FK `Document -> ServiceContract/DonationContract` в первом срезе: уже есть `contract.document`, лишняя циклическая связь усложнит миграции и integrity.
- Нельзя добавлять двусторонний FK `Document -> CenterExpense` в первом срезе: уже есть `CenterExpense.document`.

## Вертикальные срезы

1. `document-target-foundation`
   - DB: расширить `Document` целями и constraints.
   - Validation/UI: форма загрузки умеет документ получателя, центра, контрагента и договора.
   - Contracts: donation Word сохраняет `Document`; service Word сохраняет recipient target.
   - Проверки: migration dry-run после миграции, model/form/view/contract tests, full pytest, browser QA для реестра/формы документов.

2. `center-legal-profile-foundation`
   - DB: добавить активный юридический профиль центра.
   - UI: простая карточка администратора/руководителя.
   - Templates: подстановка `center.*` из профиля вместо blank fallback.
   - Проверки: model/form/view tests, Word placeholder tests.

3. `contract-signed-snapshot`
   - DB/service: фиксировать юридические данные в момент генерации/подписания.
   - UI: показывать, что файл создан из snapshot, а не из текущих карточек.
   - Проверки: изменение профиля после генерации не меняет старый документ.

4. `legal-template-families`
   - Расширить типы шаблонов: B2B, безвозмездные услуги, маткапитал, присмотр/уход, фото/видео согласие, акты.
   - Не начинать до стабилизации target/snapshot модели.

## Acceptance criteria первого среза

- Старые документы получателей остаются в списке и карточках.
- Получатель обязателен только для `target_type=recipient`.
- Можно сохранить contract document без `Child` и привязать его к `DonationContract`.
- Нельзя выбрать не-contract документ в `ServiceContract`/`DonationContract`.
- Нельзя привязать к `ServiceContract` contract document другого получателя.
- Donation Word создает или обновляет один связанный `Document` и не создает financial facts.
- Service Word продолжает создавать или обновлять один связанный `Document` и не создает financial facts.
- PDF downloads остаются read-only.
- Полный тестовый прогон проходит.

## Implementation 2026-07-18

Completed `document-target-foundation`:
- migration `operations.0028_document_counterparty_document_target_type_and_more`;
- generalized `Document.target_type`, nullable `child`, optional `counterparty`, indexes and constraints;
- document list/form target UI;
- donation Word now saves/updates counterparty `Document`; service Word remains recipient-scoped;
- verification: Ruff, Django check, migration dry-run, focused tests `31 passed`, full pytest `566 passed`, Browser QA documents list/form.

Completed `center-legal-profile-foundation`:
- migration `operations.0029_centerlegalprofile`;
- `CenterLegalProfile` model with one active profile constraint, admin/auditlog registration and product UI `/center/legal-profile/`;
- active profile values fill `center.*` placeholders in service/donation Word generation;
- no snapshot layer yet: changing the profile affects future generation only by current lookup, while already saved files are not rewritten;
- verification: Ruff, Django check, migration dry-run, focused related tests `43 passed`, full pytest `571 passed`, Browser QA center legal profile desktop/mobile.

Completed `contract-signed-snapshot`:
- migration `operations.0030_contractlegalsnapshot`;
- `ContractLegalSnapshot` stores one legal snapshot per generated contract `Document` with explicit service/donation FK, `PROTECT` links and JSON snapshots for contract, center, recipient, representative, counterparty, funding source and template;
- service and donation Word generation now creates/updates the snapshot together with the saved `Document`; PDF downloads stay read-only;
- generation rejects a linked `Document` that already has a legal snapshot for another contract, before rewriting the file;
- `/contracts/` shows compact snapshot status for generated files;
- this is still not a full immutable signed-version archive: repeated Word generation updates the same document snapshot, while later signed-version/versioning flows need a separate contract;
- verification: Ruff, Django check, migration dry-run, focused tests `37 passed`, full pytest `574 passed`, Python Playwright desktop/mobile QA for `/contracts/`.
- Graphify code-index after this slice: `4790` nodes / `19559` edges; semantic extraction was not rerun.

Next:
- `legal-template-families` or another explicit legal-document contract should follow before B2B contracts, consents, acts or immutable signed versions.
