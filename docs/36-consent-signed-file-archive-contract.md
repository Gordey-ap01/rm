# Consent Signed File Archive

Дата: 2026-07-19

Статус: реализовано 2026-07-19

Основание:
- `docs/31-immutable-contract-signed-file-archive-contract.md`
- `docs/33-contract-documents-and-templates-contract.md`
- `docs/35-act-signed-file-archive-contract.md`

## Проблема

Согласие представителя уже можно завести в системе и сформировать в Word, но подписанная версия пока остается текущим редактируемым `Document(category=consent)`. Повторная генерация Word может перезаписать связанный файл, поэтому для юридического контура нужен отдельный неизменяемый архив подписанного согласия.

Нельзя смешивать согласия с `ContractSignedFile` или `ContractActSignedFile`: у договоров и актов разные владельцы, constraints, snapshot-наборы и маршруты. Для согласий нужна отдельная узкая модель, которая не меняет финансовый, расписательный, грантовый или договорный контур.

## Решение

Добавить отдельную модель `ConsentSignedFile`:

- архив относится ровно к одному `Consent`;
- исходным файлом является текущий `Document(category=consent, target_type=recipient)` согласия;
- файл копируется в архивное хранилище, а не переиспользует путь `Document.file`;
- сохраняются имя исходного файла, content type, размер, SHA-256, дата подписания, загрузивший пользователь и статус;
- frozen snapshots фиксируются на момент архивации: согласие, центр, получатель, подписант-представитель и шаблон;
- после создания нельзя менять согласие, исходный документ, файл, checksum, дату подписания, загрузившего и snapshot-поля;
- корректировка выполняется только переводом архива в `void` с причиной.

## Не входит

- Загрузка внешнего скана/фото вместо копирования текущего generated Word.
- ЭДО, маршруты отправки на подпись, email/public consent flows.
- Автоматическое влияние согласий на расписание, допуск к занятиям, финансы, гранты, payroll, табель, платежи или импорт.
- Новая сущность `DocumentTemplate`.
- Переработка модели `Consent` в юридический snapshot-документ.
- Изменение существующих архивов договоров и актов.

## Acceptance Criteria

- Есть additive migration с новой таблицей `ConsentSignedFile`; существующие таблицы договоров, актов, расписания, финансов и импорта не меняются.
- Модель валидирует, что `source_document.category == consent`, `target_type == recipient`, а документ относится к тому же получателю.
- `file_size > 0`, `file_sha256` имеет 64 символа, `void` требует причину.
- Иммутабельные поля нельзя изменить после создания; разрешено только менять `status` и `void_reason`.
- Сервис архива требует, чтобы Word-файл согласия уже был сформирован.
- POST-действие фиксации копирует текущий файл согласия, считает SHA-256 и создает новую архивную запись без изменения финансов, расписания, грантов, табелей, платежей или статусов занятий.
- `/consents/` показывает последнюю активную архивную версию согласия и кнопку фиксации архива только для согласий с generated `Document`.
- Есть download route для архивного файла согласия.
- Проверки: Ruff touched Python/migration, Django check, migration dry-run, focused model/view tests, full pytest, Browser QA desktop/mobile для списка согласий и download.

## Доменная Модель

`ConsentSignedFile`
- `consent`: FK `Consent`, `PROTECT`;
- `source_document`: FK `Document`, `PROTECT`, nullable для admin/form compatibility, но сервис всегда заполняет;
- `file`: архивная копия файла;
- `original_filename`, `content_type`, `file_size`, `file_sha256`;
- `signed_on`: дата подписания, по умолчанию `Consent.signed_on` или текущая дата;
- `uploaded_by`: пользователь, который зафиксировал архив;
- `status`: `active` или `void`;
- `void_reason`;
- snapshot JSON-поля: `consent_snapshot`, `center_snapshot`, `recipient_snapshot`, `representative_snapshot`, `template_snapshot`;
- `note`.

## План Реализации

1. Добавить `consent_signed_file_upload_path` и модель `ConsentSignedFile`.
2. Сгенерировать migration `0040` и применить локально.
3. Зарегистрировать модель в admin и auditlog.
4. Добавить сервис `archive_consent_signed_file()`.
5. Добавить routes/views для POST-фиксации и download.
6. Расширить `/consents/` ссылкой на архив и кнопкой `Зафиксировать`.
7. Добавить focused tests.
8. Выполнить проверки, Browser QA, Graphify update и recovery updates.

## Агентские Правила

- Срез делает один DB-owner, потому что меняются `operations/models.py` и migration chain.
- Не параллелить изменения моделей/миграций с другими агентами.
- Параллельным агентам можно отдавать только read-only review или подготовку приватных `.docx` шаблонов вне git.

## Implementation 2026-07-19

Выполнен срез `consent-signed-file-archive`:

- migration `operations.0040_consentsignedfile`;
- новая модель `ConsentSignedFile`;
- upload path `consent_signed_files/<consent_id>/...`;
- admin/auditlog registration;
- сервис `archive_consent_signed_file()`;
- routes:
  - `/consents/<id>/archive-signed/`;
  - `/consents/signed-files/<id>/download/`;
- `/consents/` показывает последнюю активную архивную копию согласия и кнопку `Зафиксировать` после Word-генерации.

Проверено:

- Ruff touched Python/migration;
- Django check;
- migration dry-run `No changes detected`;
- focused model/view tests `34 passed`;
- full pytest `620 passed`;
- Playwright desktop/mobile QA for consent archive UI and download.
- Graphify code-index `5157` nodes / `22691` edges.

Не менялось:
- `ContractSignedFile` для договоров;
- `ContractActSignedFile` для актов;
- `LedgerEntry`, `BalanceAccount`, `Payment`, billing, payroll, grants, schedule, appointment statuses, import write-path.
