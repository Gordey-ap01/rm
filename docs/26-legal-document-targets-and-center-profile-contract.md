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

## Следующие модели, но не в первом срезе

`CenterLegalProfile`:
- активная карточка центра с полным/кратким названием, директором, основанием полномочий, лицензией, ОГРН, ИНН, КПП, адресами, контактами и банковскими реквизитами;
- не подставляется в подписанные старые документы задним числом.

`ContractLegalSnapshot` или snapshot-поля договора:
- фиксируют данные центра, контрагента, представителя, получателя и спецификации услуг на момент генерации/подписания;
- нужны до полноценного юридического документооборота, актов и подписанных версий.

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
