# External Signed File Upload

Дата: 2026-07-19

Статус: реализовано 2026-07-19

Основание:
- `docs/31-immutable-contract-signed-file-archive-contract.md`
- `docs/35-act-signed-file-archive-contract.md`
- `docs/36-consent-signed-file-archive-contract.md`

## Проблема

Архив подписанных договоров, актов и согласий уже сохраняет неизменяемую копию generated Word. В реальной работе администратор часто получает подписанный скан или PDF: распечатали, подписали, отсканировали. Сейчас такой файл нельзя зафиксировать через UI без подмены текущего `Document.file`.

## Решение

Добавить upload-flow поверх существующих архивных моделей:

- БД-схема не меняется.
- POST `archive-signed` сохраняет generated Word как раньше, если файл не приложен.
- Если в POST приложен `signed_file`, сервис архивирует именно загруженный файл.
- Поддерживаемые расширения: `.pdf`, `.docx`, `.jpg`, `.jpeg`, `.png`.
- Размер файла ограничен 15 МБ.
- `source_document` остается текущий generated `Document`, который задает юридический источник/snapshot и проверку владельца.
- Архивные записи остаются immutable, correction path остается `status=void` с причиной.

## Не входит

- Распознавание сканов, OCR, ЭДО, электронная подпись.
- Создание нового `Document` под загруженный скан.
- Массовая загрузка архивов.
- Изменение моделей `ContractSignedFile`, `ContractActSignedFile`, `ConsentSignedFile`.
- Финансы, расписание, гранты, payroll, платежи, импорт.

## Acceptance Criteria

- Новых миграций нет.
- Существующая кнопка `Зафиксировать` продолжает копировать generated Word.
- Новый upload-control на `/contracts/` и `/consents/` позволяет загрузить внешний подписанный файл.
- Сервис считает SHA-256 и размер по загруженному файлу, сохраняет original filename и content type.
- Архив использует те же frozen snapshots, что и текущая фиксация generated Word.
- Неверное расширение или файл больше 15 МБ не создает архив.
- Download route возвращает именно загруженный файл.
- Проверки: Ruff touched Python, Django check, migration dry-run `No changes detected`, focused tests, full pytest, Browser QA desktop/mobile.

## Implementation 2026-07-19

Выполнен срез `external-signed-file-upload`:

- новых миграций нет;
- добавлена форма `SignedArchiveUploadForm`;
- archive POST для service/donation/B2B договоров, актов и согласий теперь принимает optional `signed_file`;
- если файл не приложен, прежнее копирование generated Word сохраняется;
- если файл приложен, архив сохраняет загруженный payload, original filename, content type, size, SHA-256 и прежние frozen snapshots;
- `/contracts/` и `/consents/` получили компактный upload-control рядом с `Зафиксировать`;
- download routes возвращают загруженный файл без создания нового `Document`.

Проверено:

- Ruff touched Python;
- Django check;
- migration dry-run `No changes detected`;
- focused upload/view tests `13 passed`;
- full pytest `624 passed`;
- Playwright desktop/mobile QA for service contract + consent external signed upload and download;
- Graphify code-index `5186` nodes / `22856` edges.

Не менялось:
- models/migrations;
- archive immutability rules;
- `LedgerEntry`, `BalanceAccount`, `Payment`, billing, payroll, grants, schedule, appointment statuses, import write-path.
