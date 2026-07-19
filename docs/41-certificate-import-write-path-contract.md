# Certificate Import Write-Path Contract

Дата: 2026-07-19

Статус: частично выполнен: import-batch-foundation; apply/write-path не начат

Основание:
- `docs/38-certificate-payer-source-contract.md`
- `docs/39-recipient-certificate-crud-contract.md`
- `docs/40-certificate-import-preview-contract.md`
- `docs/decisions/ADR-002-balance-accounts-ledger.md`

## Цель

Добавить реальный импорт сертификатов из Excel/CSV после read-only preview так, чтобы администратор мог массово создать карточки сертификатов без ручного ввода и без риска скрыто изменить финансы, расписание или остатки по счетам.

## Ключевое решение

Первый write-path не должен быть прямым POST "загрузил файл = сразу создал записи". Нужен двухшаговый контур:

1. `Preview`: файл разбирается, строки валидируются, создается persisted import batch с результатами проверки.
2. `Apply`: администратор явно подтверждает применение batch; система атомарно создает только валидные и еще не примененные строки.

Такой контур нужен для идемпотентности, аудита, восстановления после падения и безопасной работы с персональными данными.

## Предлагаемая доменная модель

### ImportBatch

Общая модель для будущих импортов, но первый применяемый тип - только `certificates`.

Поля:
- `import_kind`: enum, в первом срезе только `certificates`.
- `status`: `previewed`, `applying`, `applied`, `partially_applied`, `failed`, `cancelled`.
- `original_filename`.
- `source_sha256`.
- `uploaded_by`.
- `applied_by`.
- `applied_at`.
- `total_rows`, `valid_rows`, `invalid_rows`, `warning_rows`, `applied_rows`, `skipped_rows`.
- `header_snapshot`: JSON со строкой заголовков и mapping в системные поля.
- `error_summary`: JSON для ошибки batch-level.
- timestamps.

Индексы:
- `(import_kind, status, created_at)`.
- `(uploaded_by, created_at)`.
- `source_sha256`.

Ограничения:
- `source_sha256` не должен быть уникальным глобально: один и тот же файл можно проверить повторно после изменения справочников.
- Apply повторно к одному batch запрещен статусом и row-level флагами, а не уникальностью файла.

### ImportBatchRow

Поля:
- `batch`.
- `row_number`.
- `status`: `valid`, `invalid`, `applied`, `skipped`, `failed`.
- `raw_values`: JSON с распознанными значениями строки.
- `normalized_values`: JSON с системными значениями, готовыми к созданию `Certificate`.
- `errors`: JSON list.
- `warnings`: JSON list.
- `target_model`: строка, в первом срезе `operations.Certificate`.
- `target_pk`: nullable bigint.
- `applied_at`.

Индексы и ограничения:
- unique `(batch, row_number)`.
- index `(batch, status)`.
- index `(target_model, target_pk)` для аудита результата.

## Правила применения certificate batch

- Apply разрешен только admin/staff пользователю с тем же уровнем доступа, что текущий `/contracts/import-preview/`.
- Apply запускается через явный POST с CSRF и hold-to-confirm UI.
- Apply выполняется в `transaction.atomic()`.
- Batch берется под lock; если статус уже terminal, повторный POST ничего не создает и показывает итог.
- Создаются только строки без errors.
- Строки с warnings создаются только если warning не является блокирующим. В первом срезе duplicate certificate number должен быть `skipped`, а не создан.
- Получатель, источник и представитель-плательщик должны уже существовать. Автосоздание получателей, представителей, источников финансирования и договоров запрещено.
- Если `payer_name` указан вручную, сертификат может быть создан без `payer_representative`.
- Если указан представитель-плательщик, он должен относиться к найденному получателю.
- Если `funding_source` указан, он сохраняется в `Certificate.funding_source`.
- `Certificate.total_amount` и `Certificate.remaining_amount` берутся из файла; ledger не создается.

## Дубликаты и уникальность

Текущая модель `Certificate` не имеет DB unique constraint на номер. Первый write-path должен быть консервативным:

- Если у получателя уже есть сертификат с тем же непустым `number`, строка не создает новую запись и получает статус `skipped`.
- В первом срезе запрещены update/upsert существующего сертификата из файла.
- DB unique constraint `unique_child_certificate_number_when_not_empty` можно рассмотреть отдельной миграцией только после preflight-а существующих данных. Это потенциально опасная миграция, если в production уже есть дубли.

## Финансовые границы

Write-path создает только `Certificate`.

Не создаются и не изменяются:
- `BalanceAccount`;
- `Payment`;
- `LedgerEntry`;
- appointment billing decisions;
- payroll/accruals;
- grant allocations/facts;
- service contracts;
- certificate остатки через занятия.

Связка сертификата со счетом баланса и автоматическое списание сертификата занятиями требуют отдельного контракта.

## UX/UI

Новый flow должен остаться в контуре `/contracts/import-preview/` или рядом с ним:

1. Администратор загружает файл и получает preview.
2. Если есть ошибки, кнопка apply скрыта.
3. Если есть валидные строки, показывается блок "Можно применить".
4. Перед apply система показывает:
   - сколько сертификатов будет создано;
   - сколько строк будет пропущено как дубликаты;
   - что финансы и остатки счетов не изменятся.
5. После apply страница показывает итог batch и ссылки на созданные сертификаты/получателей.

## Acceptance criteria для первого DB-owner среза

- Добавлены additive модели/migration `ImportBatch` и `ImportBatchRow`.
- Preview certificates сохраняет batch + rows, но не создает `Certificate`.
- Batch rows хранят errors/warnings и normalized values.
- Apply endpoint атомарно создает сертификаты только из valid rows.
- Повторный apply не создает дубликаты.
- Existing certificate with same child + non-empty number is skipped, not duplicated.
- Ошибочные строки не создают сертификаты.
- Apply не создает `BalanceAccount`, `Payment`, `LedgerEntry`, payroll, grants, appointments или contracts.
- Admin/auditlog регистрируют новые import batch models.
- Проверки: migration dry-run after migration, Ruff, Django check, focused service/view tests, full pytest, Browser QA preview+apply.

## Риски миграций

- Добавление `ImportBatch`/`ImportBatchRow` additive и низкорисковое.
- Добавление DB unique constraint на существующий `Certificate.number` потенциально опасно и не входит в первый write-path.
- Если позже понадобится хранить исходные файлы импорта, это отдельное решение по private media, срокам хранения и персональным данным.

## Параллельная работа

- Один DB-owner владеет `operations/models.py` и migration chain.
- UI/tests агент может работать только после merged DB contract and named interfaces.
- Нельзя одновременно менять parser, apply service и модели разными агентами без утвержденного контракта функций.

## Реализация 2026-07-19: import-batch-foundation

Выполнен первый foundation-срез без создания сертификатов:

- Добавлены additive модели `ImportBatch` и `ImportBatchRow`.
- Добавлена migration `operations.0042_importbatch_importbatchrow_and_more`.
- Модели зарегистрированы в Django admin и auditlog.
- `ImportPreview` сохраняет `source_sha256`.
- Для `certificates` preview сохраняется persisted `ImportBatch` и `ImportBatchRow` с row-level `errors`, `warnings`, `raw_values`, `normalized_values`.
- UI `/contracts/import-preview/` показывает блок "Сохраненный preview" с номером batch, файлом и счетчиками строк.
- Preview остается read-only: `Certificate` не создается, `BalanceAccount`, `Payment`, `LedgerEntry`, payroll, grants, schedules and statuses не меняются.
- Проверки: Ruff touched Python и полный `operations`, Django check, migration dry-run `No changes detected`, focused import/audit tests `9 passed`, full pytest `632 passed`, Playwright desktop/mobile QA persisted preview batch.
- Browser QA synthetic `BQA-IMPORTBATCH*` data cleaned; local runserver `8109` stopped.

Не выполнено и остается следующим срезом:

- Apply endpoint.
- Создание `Certificate` из batch rows.
- Row-level idempotent apply statuses `applied/skipped/failed`.
- UI hold-to-confirm для применения batch.
