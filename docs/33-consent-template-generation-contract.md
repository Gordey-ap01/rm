# Consent Template Generation

Дата: 2026-07-19

Статус: реализовано 2026-07-19

Основание:
- `docs/24-document-template-source-inventory.md`
- `docs/25-template-placeholder-expansion-v2-contract.md`
- `docs/26-legal-document-targets-and-center-profile-contract.md`
- `docs/27-legal-template-families-contract.md`
- `docs/28-representative-child-legal-fields-contract.md`

## Проблема

В системе уже есть `Consent` и `Document(category=consent)`, а в исходных шаблонах есть согласие на фото/видео. Но текущая карточка согласия только фиксирует факт вручную: тип, получатель, даты и optional документ. Она не хранит подписанта-представителя, не выбирает шаблон и не может сформировать `.docx` из структурных данных.

Нужен первый безопасный срез генерации согласий: администратор выбирает получателя, подписанта, тип согласия и шаблон; система формирует Word-файл, сохраняет его как документ получателя категории `consent` и привязывает к `Consent.document`.

## Решение первого среза

- Добавить в `Consent` nullable `signatory_representative` на `RecipientRepresentative`.
- Добавить в `Consent` nullable `template` на `ContractTemplate`.
- Добавить `ContractTemplate.consent_template_types()`: доступны `consent_photo_video` и `other`.
- Валидация `Consent`:
  - дата окончания не раньше даты подписи;
  - подписант должен относиться к тому же получателю;
  - шаблон должен быть из consent allowlist;
  - связанный документ должен быть `Document(category=consent, target_type=recipient, child=<consent.child>)`.
- `ConsentForm` фильтрует подписантов по выбранному получателю и показывает только допустимые шаблоны/документы.
- `operations.services.contract_documents` получает генерацию `.docx` для согласия с placeholders `center.*`, `child.*`, `representative.*`, `consent.*`.
- `/consents/` показывает подписанта, шаблон, документ и POST-действие `Word`.

## Не входит

- Электронная подпись и ЭДО.
- Immutable signed archive для согласий.
- Новый общий `DocumentTemplate`.
- Акты выполненных услуг.
- Связь согласий с расписанием, рассылками, публичными ссылками или автоматическим запретом публикации фото/видео.
- Перенос старых документов или попытка распознать уже загруженные файлы.
- Любые изменения `LedgerEntry`, `BalanceAccount`, `Payment`, payroll, grants, appointments или статусов занятий.

## Acceptance criteria

- Администратор может создать согласие с получателем, подписантом, шаблоном, датами и примечанием.
- Подписант другого получателя отклоняется на уровне модели/формы.
- Шаблон другого семейства отклоняется.
- Документ другого получателя, не-recipient target или не категории `consent` отклоняется.
- Word generation для согласия создает/обновляет `Document(target_type=recipient, category=consent, child=<consent.child>)` и привязывает его к `Consent.document`.
- Повторная Word generation обновляет тот же документ согласия, а не создает дубликат.
- Реестр `/consents/` показывает подписанта/шаблон/документ и кнопку `Word`.
- Полный pytest проходит; Browser QA проверяет desktop/mobile реестр согласий и появление документа после Word.

## Реализация 2026-07-19

- Добавлена migration `operations.0037_consent_signatory_representative_consent_template_and_more`.
- `Consent` получил optional `signatory_representative` (`PROTECT`) и optional `template` (`SET_NULL`).
- `ContractTemplate.consent_template_types()` допускает `consent_photo_video` и `other`.
- `Consent.clean()` проверяет подписанта того же получателя, consent template allowlist, recipient `Document(category=consent)` и порядок дат.
- `ConsentForm` фильтрует подписантов и документы по выбранному получателю, шаблоны - по consent allowlist.
- `operations.services.contract_documents` генерирует `.docx` согласия, поддерживает placeholders `consent.*` и сохраняет файл как `Document(target_type=recipient, category=consent)`.
- Добавлен POST route `/consents/<id>/word/`; `/consents/` показывает подписанта, шаблон, документ и действие `Word`.
- Не добавлялись signed archive, legal snapshot для согласий, `DocumentTemplate`, акты, расписательные запреты, рассылки, финансы или import write-path.
- Проверки: Ruff, Django check, migration dry-run `No changes detected`, focused legal/consent tests `26 passed`, full pytest `607 passed`, in-app Browser desktop/mobile QA для реестра согласий и Word-triggered document link. In-app Browser не поддерживает download events; Word download route покрыт Django tests.
- Graphify code-index after this slice: `5028` nodes / `21578` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private.

## Риски

- `ContractTemplate` по имени договорный, но уже содержит `consent_photo_video`. В первом срезе используем его как общий шаблонный каталог, чтобы не делать преждевременное переименование в `DocumentTemplate`.
- У согласий пока нет frozen legal snapshot. Это приемлемо для генерации первичного файла, но юридически подписанный архив согласий нужно делать отдельным контрактом.
- `docshablon/` содержит реальные данные; raw образцы не коммитить и не отправлять в Graphify semantic extraction.

## Агентские правила

- Срез делает один DB-owner агент: меняются `operations/models.py` и migration chain.
- Параллельные агенты допустимы только для read-only review или подготовки `.docx` template вне git.
- Не подключать согласия к расписанию/финансам без отдельного утвержденного контракта.
