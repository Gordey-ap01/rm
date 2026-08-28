# Контракт 60: приватные артефакты и факт сдачи отчета

Дата: 2026-08-28
Статус: реализован и локально принят; production-допуск только после критериев раздела 10
Связан с: контрактом 59B-2 и ADR-008

Документ не заменяет контракт 59 и не повторяет модель закрытого payload. Он
фиксирует недостающий storage/access/retention контракт, после которого можно
реализовать `DonorReportSubmission`.

## 1. Границы среза

Входит:

- локальное приватное хранилище вне `MEDIA_ROOT` и публичных маршрутов Caddy;
- append-only факт первой сдачи и замены файла одного закрытого snapshot;
- отдельное право администратора на скачивание;
- неизменяемый журнал серверных выдач проверенных байтов;
- проверка размера, формата, MIME, SHA-256, наличия и осиротевших файлов;
- backup/restore БД, public media и private artifacts одним набором.

Не входит:

- генератор универсальной донорской формы;
- отправка файла во внешнюю систему или email;
- публичная ссылка;
- удаление либо сокращение срока хранения;
- облачный storage, DLP и внешний антивирусный сервис.

## 2. Хранение и retention

`PRIVATE_ARTIFACT_ROOT` является отдельным корнем. В production это отдельный
Docker volume, доступный web-процессу, но не Caddy. У объекта нет публичного URL:
чтение идет только через авторизованный Django view.

Storage key генерируется сервером из snapshot, номера сдачи, SHA-256 и
разрешенного расширения. Клиентское имя в пути не используется. Final key
создается один раз без overwrite; повторное содержимое не заменяет байты.
Файлы и каталоги создаются с минимальными локальными правами.

До отдельного решения владельца центра действует консервативное бессрочное
хранение: application и
обычные management commands не удаляют submission, access events и final
objects. Срок очистки backup-копий не является разрешением удалить основной
артефакт. Это предотвращает преждевременную потерю юридической истории, но не
заменяет утвержденную retention/legal-hold policy.

## 3. Разрешенные файлы

Первый срез принимает:

- PDF;
- DOCX без макросов;
- XLSX без макросов;
- ODT;
- ODS.

Максимальный размер: 25 MiB, пустой файл запрещен. Расширение и заявленный
браузером MIME не являются доказательством: формат определяется по сигнатуре и
структуре ZIP-контейнера. Архивы с path traversal, шифрованием, макросами,
чрезмерным числом записей или чрезмерным распакованным размером отклоняются.
Оригинальное имя хранится только как метаданные после удаления пути и
управляющих символов.

Файл всегда отдается как attachment с `nosniff`, `private, no-store` и
внутренней классификацией данных. Встроенный preview отсутствует.

## 4. Доменная модель

### `DonorReportSubmission`

- `report_snapshot`, `PROTECT`;
- `submission_number`, начиная с 1;
- `event_type= submitted | replaced`;
- nullable `supersedes`, `RESTRICT`, того же snapshot;
- уникальный generated `storage_key`;
- `original_filename`, detected `content_type`, `file_size`, `file_sha256`;
- `submitted_on`, `recorded_at`;
- `external_reference`, `actor`, `actor_role_snapshot=director`;
- обязательное `reason`, необязательное `note`.

Первая запись имеет номер 1, событие `submitted` и не имеет `supersedes`.
Следующая имеет последовательный номер, событие `replaced` и ссылается на
текущую терминальную запись. Один predecessor имеет не более одного successor.
Замена тем же SHA-256 запрещена. Изменение payload требует нового
`DonorReportSnapshot`, а не submission.

### `DonorReportSubmissionAccess`

Типизированный append-only факт серверной выдачи проверенного файла:

- `submission`, `PROTECT`;
- `actor`, `PROTECT`;
- snapshot роли `director | administrator`;
- основание права `director_role | explicit_permission`;
- `verified_sha256` реально проверенных перед ответом байтов;
- `accessed_at`.

Событие означает, что сервер авторизовал запрос, проверил exact bytes и начал
ответ; HTTP не позволяет доказать, что клиент полностью сохранил поток. Отказ
в доступе не создает ложное событие выдачи. Операционный security-log отказов
остается задачей инфраструктурного logging/monitoring.

## 5. Полномочия

- Создать первую сдачу или замену может только руководитель с основанием.
- Руководитель всегда может скачать.
- Администратор может скачать все submission-файлы только при отдельном
  глобальном Django permission
  `operations.download_donorreportsubmission`.
- Permission не расширяет доступ специалиста или обычного пользователя.
- Администратор без permission видит метаданные и историю, но не ссылку
  скачивания и получает `403` по прямому URL.

## 6. Write и read path

Write:

1. Поток сохраняется во временный файл внутри private root с подсчетом размера
   и SHA-256.
2. Формат определяется по содержимому.
3. В транзакции блокируется snapshot и читается терминальная submission.
4. Проверяется optimistic `expected_submission_id`.
5. Final object создается без overwrite, затем создается строка БД.
6. При ошибке транзакции созданный final object и temp удаляются. При аварии
   процесса возможный orphan обнаруживается integrity scan.

Read:

1. Проверяются роль и отдельное permission.
2. Storage key повторно валидируется внутри private root.
3. Перед ответом пересчитываются размер, MIME и SHA-256.
4. При расхождении скачивание блокируется `409`.
5. При первом чтении тела streaming response создается access event, после
   чего отдаются те же уже проверенные bytes без повторного открытия пути.
   Ответ, который клиент не начал читать, не создает ложный факт выдачи.

## 7. Миграция и DB-инварианты

Migration `0054` только добавляет две таблицы, permission, constraints, indexes
и PostgreSQL triggers. Legacy backfill отсутствует.

Immutable manager, model guards и PostgreSQL triggers запрещают
`UPDATE/DELETE`, проверяют
последовательную цепочку, роль, основание, SHA-256, MIME/extension/storage-key
matrix и согласованность access event. QuerySet/raw SQL не должны обходить
неизменяемость.

Production runtime-роль имеет на неизменяемых таблицах только `SELECT/INSERT`:
`UPDATE/DELETE/TRUNCATE` отозваны явно, как и запись в `django_migrations`, database
`TEMPORARY`, DDL, ownership и role membership. Append-цепочка сериализуется
через изменяемый корень `DonorReport`; service и DB-trigger не требуют
`SELECT FOR UPDATE` на самой неизменяемой истории. `PUBLIC` и runtime не имеют
общего `EXECUTE` на application functions: runtime получает только фиксированный
allowlist из трех чистых validation helpers, вызываемых защитными triggers.

Это приложение-уровневая атрибуция, а не криптографическое доказательство
личности. Компрометация runtime-подключения не позволяет изменить или удалить
историю, но позволяет дописать правдоподобное ложное событие с существующим
`actor_id`. Подписанный внешний writer/HSM-контур не входит в этот срез; до его
введения доступ к runtime credentials и процессу web является доверительной
границей и должен контролироваться как production secret.

После появления первой submission migration `0054` не откатывается:
production rollback оставляет additive schema, private volume и историю,
запуская совместимую версию приложения.

## 8. Backup, restore и integrity

Новый backup format v2 содержит:

- `db.dump`;
- `media.tar.gz`;
- `private-artifacts.tar.gz`;
- `metadata.env` с измеренным размером исходной БД;
- checksum всех трех архивов и metadata.

Backup выполняется в коротком окне остановки web-записей: quiescent integrity
scan, затем DB dump и оба файловых архива. Staging и следы незавершенного
restore не входят в архив. До остановки web записывается durable
`.backup-in-progress` с исходным состоянием сервиса. После аварии штатная
`backup_prod.sh --recover` удаляет только неопубликованные `.partial-*`,
возвращает прежнее состояние web, дожидается health-check и лишь затем удаляет
marker. Валидный fsynced `.backup-in-progress.tmp`, оставшийся при обрыве до
atomic rename, принимается recovery; некорректный marker сохраняется для разбора.
Новый backup, restore, deploy и migration до recovery блокируются.

Проверка и restore сохраняют совместимость с v1 только после доказательства,
что staged-БД не содержит submission-строк. V1 с такими строками
останавливается без изменения live-БД/private root и не запускает web. V2
сначала проверяется от path traversal, duplicate entries, symlink/special
members, checksum и чрезмерного распакованного размера. До распаковки
проверяются bytes/inodes media/private и PostgreSQL с запасом под
staged-БД и WAL. Backup, restore и production deploy/migration делят один
host-level `flock`; backup отказывается работать при любых следах
незавершенного restore. Готовый backup публикуется только после `fsync` файлов,
временного каталога, rename и `fsync` родительского каталога.

Restore восстанавливает dump в отдельную временную БД, применяет к ней
миграции и сверяет staged private root. Только после этого PostgreSQL одной
транзакцией переименовывает live-БД в rollback-БД, а staged-БД — в live.
Старые DB/media/private сохраняются до итогового strict scan, запуска
candidate web и health-check. Candidate получает разрешение старта только из
однооперационного `compose.restore.yaml`; базовый production Compose всегда
фиксирует `RESTORE_CANDIDATE_START=0`. Caddy остается остановлен до
durable-состояния `validated`. Durable marker/file transitions делают `fsync`;
подготовка rollback-каталога публикуется через `.restore-old-preparing`, а
порядок синхронизации и повторный recovery допускают безопасный дубликат имени
после сбоя между `rename` и `fsync`;
неизвестное состояние со старыми файлами блокирует recovery без удаления.
Любой неизвестный `.restore-*` также блокирует переключение до ручного разбора.
Валидный temp-only maintenance marker принимается перед recovery. Maintenance
marker блокирует запуск web при обрыве; явная команда
`restore_prod.sh --recover --confirm` либо возвращает старую согласованную
генерацию, либо завершает cleanup уже проверенной новой. CI fault injection
доказывает rollback после сбоя после переключения БД и файловых корней,
повторяемость recovery при его собственном обрыве, exact private bytes и отказ
опасного v1. Отдельная fault injection подтверждает, что неудачный deploy после
старта новой web-версии не открывает ни web, ни Caddy.

Read-only management command проверяет:

- missing object;
- size/hash/MIME mismatch;
- object с небезопасным key;
- orphan final object;
- оставшийся staging object.

`--strict` возвращает ненулевой exit code при нарушении и включается в
production preflight. Live scan применяет короткий grace period к возможным
текущим staging/orphan objects; backup/restore запускают `--quiescent` после
остановки writers и не допускают исключений.

## 9. Acceptance criteria

- Руководитель через UI создает сдачу №1 и замену №2 одного snapshot.
- Администратор без permission получает `403`; с permission скачивает exact
  bytes, и каждое скачивание создает отдельный access event.
- Неверный формат, макрос, oversized upload, stale token и повтор того же
  SHA-256 не создают строку или final object.
- Update/delete через model, queryset и PostgreSQL raw path запрещены.
- Поврежденный или отсутствующий файл дает `409`, а strict scan завершается
  ошибкой.
- Backup/restore drill восстанавливает БД и exact private file с тем же
  SHA-256.
- Focused SQLite/PostgreSQL tests, Ruff, Django check, migration dry-run,
  полный pytest и desktop/mobile browser smoke двух ролей проходят.

## 10. Production-допуск и остаточные риски

До реального пилота обязательны:

- задать вне Git разные production credentials для уже реализованных
  runtime и migration/restore DB-ролей; preflight отклоняет совпадение,
  owner/DDL/role-membership и право заменять защитные функции;
- подтвердить владельцем центра retention/legal-hold policy;
- подтвердить шифрование диска VPS и offsite backup at rest;
- выбрать malware scanning policy. Первый срез принимает файлы только от
  руководителя, блокирует макросы и активные архивные элементы, но сигнатурная
  проверка формата не является антивирусом;
- выполнить production backup v2/restore drill до открытия upload-команды.

Совместимый rollback-релиз после `0054` обязан сохранять private volume,
использовать backup v2 и запретить новые submission, если сам не поддерживает
их write/read path.

## 11. Факт локальной приемки

- Migration `0054`, модели, PostgreSQL triggers, storage service, role UI,
  integrity commands, разделенные runtime/migration DB-роли и backup/restore v2
  реализованы.
- Focused SQLite: `20 passed, 8 skipped`; focused PostgreSQL 17: `28 passed`
  без пропусков. Полный SQLite regression после hardening:
  `844 passed, 43 skipped`; пропуски относятся к PostgreSQL-only контрактам.
- Restore drill v2 доказал exact private bytes, отказ поврежденного backup,
  abrupt backup recovery, повторяемый возврат старой DB/media/private после
  двух последовательных fault injection, durable file recovery, изоляцию
  candidate за остановленным Caddy, candidate health-check и fail-closed для v1
  с submission-строками.
- Browser smoke на обезличенной PostgreSQL-БД подтвердил замену
  файла, download/access event, desktop и `390px`; console errors и page
  overflow отсутствуют. Матрица ролей дополнительно покрыта view-тестами.
