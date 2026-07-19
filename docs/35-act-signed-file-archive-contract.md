# Act Signed File Archive

Дата: 2026-07-19

Статус: реализовано 2026-07-19

Основание:
- `docs/31-immutable-contract-signed-file-archive-contract.md`
- `docs/34-contract-acts-generation-contract.md`

## Проблема

Акты оказанных услуг уже можно создать в системе и сформировать в Word, но подписанная версия акта пока остается только текущим редактируемым `Document`. Для юридического контура нужен неизменяемый архив подписанного файла: повторная генерация Word не должна переписывать уже зафиксированную подписанную копию.

Акт нельзя просто добавить в `ContractSignedFile`: эта модель уже описывает архив договоров и имеет договорные constraints, названия и `contract_kind`. Расширение ее четвертым типом смешает договоры и акты, увеличит риск регрессии в уже проверенном архиве договоров.

## Решение

Добавить отдельную модель `ContractActSignedFile`:

- архив относится ровно к одному `ContractAct`;
- исходным файлом является текущий `Document(category=act)` акта;
- файл копируется в архивное хранилище, а не переиспользует путь `Document.file`;
- сохраняются имя исходного файла, content type, размер, SHA-256, дата подписания, загрузивший пользователь и статус;
- frozen snapshots копируются из `ContractAct` на момент фиксации;
- после создания нельзя менять акт, исходный документ, файл, checksum, дату подписания, загрузившего и snapshot-поля;
- корректировка выполняется только переводом архива в `void` с причиной.

## Не входит

- Загрузка внешнего скана подписанного акта вместо копирования текущего generated Word.
- ЭДО, внешняя подпись, маршруты отправки контрагенту.
- Автоматическое изменение `ContractAct.status`.
- Связь акта с `Appointment`, списаниями, табелем, payroll, грантами или платежами.
- `Payment`, `LedgerEntry`, `BalanceAccount`, certificate balance mutation.
- Excel import write-path.
- Изменение существующего `ContractSignedFile` для договоров.

## Acceptance Criteria

- Есть additive migration с новой таблицей `ContractActSignedFile`; существующие таблицы договоров, финансов, расписания и импорт не меняются.
- Модель валидирует, что `source_document.category == act`.
- Для акта к договору с получателем архивный документ должен быть документом того же получателя.
- Для B2B-акта архивный документ не может быть документом получателя и, если это документ контрагента, должен принадлежать организации договора.
- `file_size > 0`, `file_sha256` имеет 64 символа, `void` требует причину.
- Иммутабельные поля нельзя изменить после создания; разрешено только поменять `status` и `void_reason`.
- Сервис архива требует, чтобы Word-файл акта уже был сформирован и snapshot-поля акта уже заполнены.
- `/contracts/` показывает последнюю активную архивную версию акта и кнопку фиксации архива только для актов с generated `Document` и snapshot.
- POST-действие фиксации копирует текущий файл акта, считает SHA-256 и создает новую архивную запись без изменения финансов, расписания, грантов, табелей, платежей или статусов занятий.
- Есть download route для архивного файла акта.
- Проверки: Ruff touched Python/migration, Django check, migration dry-run, focused model/service/view tests, full pytest, Browser QA desktop/mobile для блока актов.

## Доменная Модель

`ContractActSignedFile`
- `act`: FK `ContractAct`, `PROTECT`;
- `source_document`: FK `Document`, `PROTECT`, nullable для совместимости формы/admin, но сервис всегда заполняет;
- `file`: архивная копия файла;
- `original_filename`, `content_type`, `file_size`, `file_sha256`;
- `signed_on`: дата подписания, по умолчанию дата акта или текущая дата;
- `uploaded_by`: пользователь, который зафиксировал архив;
- `status`: `active` или `void`;
- `void_reason`;
- snapshot JSON-поля: `act_snapshot`, `contract_snapshot`, `center_snapshot`, `recipient_snapshot`, `representative_snapshot`, `counterparty_snapshot`, `funding_source_snapshot`, `template_snapshot`;
- `note`.

## План Реализации

1. Добавить `contract_act_signed_file_upload_path` и модель `ContractActSignedFile`.
2. Сгенерировать migration `0039` и применить локально.
3. Зарегистрировать модель в admin и auditlog.
4. Добавить сервис `archive_contract_act_signed_file()`.
5. Добавить routes/views для POST-фиксации и download.
6. Расширить блок актов в `/contracts/`.
7. Добавить focused tests.
8. Выполнить проверки, Browser QA, Graphify update и recovery updates.

## Агентские Правила

- Срез делает один DB-owner, потому что меняются `operations/models.py` и migration chain.
- Не параллелить изменения моделей/миграций с другими агентами.
- Параллельным агентам можно отдавать только read-only review или подготовку приватных `.docx` шаблонов вне git.

## Implementation 2026-07-19

Выполнен срез `act-signed-file-archive`:

- migration `operations.0039_contractactsignedfile`;
- новая модель `ContractActSignedFile`;
- upload path `contract_act_signed_files/<act_id>/...`;
- admin/auditlog registration;
- сервис `archive_contract_act_signed_file()`;
- routes:
  - `/contracts/acts/<id>/archive-signed/`;
  - `/contracts/acts/signed-files/<id>/download/`;
- блок актов в `/contracts/` показывает последнюю активную архивную копию и кнопку `Зафиксировать` после Word-генерации акта.

Проверено:

- Ruff touched Python/migration;
- Django check;
- migration dry-run `No changes detected`;
- focused contract/view tests `66 passed`;
- full pytest `616 passed`;
- Playwright desktop/mobile QA for act signed archive UI and download.
- Graphify code-index `5122` nodes / `22406` edges.

Не менялось:
- `ContractSignedFile` для договоров;
- `LedgerEntry`, `BalanceAccount`, `Payment`, billing, payroll, grants, schedule, appointment statuses, import write-path.
