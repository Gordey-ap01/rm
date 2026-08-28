# ADR-008: приватное content-addressed хранилище артефактов

## Статус

Принято

## Дата

2026-08-01

## Контекст

Закрытый `DonorReportSnapshot` хранит проверяемый структурный payload, но
контракт 59B-2 требует сохранять и фактически отправленный донору файл. Текущий
`MEDIA_ROOT` отдается Caddy по `/media/*`, поэтому даже случайный непредсказуемый
путь не является достаточной защитой чувствительного документа.

Продукт остается local-first. Выбор внешнего S3-поставщика и его владельца еще
не принят, но это не должно блокировать корректную доменную модель и локальный
production-контур.

## Решение

- Ввести отдельный `PRIVATE_ARTIFACT_ROOT` и production volume, который не
  монтируется в Caddy.
- Не использовать `FileField.url` и публичный media backend для чувствительных
  submission-файлов.
- Хранить в БД generated content-addressed/write-once `storage_key`, SHA-256,
  detected MIME и размер.
- Записывать файл через узкий storage service с staging, no-overwrite publish,
  rollback cleanup и integrity scan.
- Разрешать чтение только server-mediated Django view после проверки
  конкретного объекта, роли и глобального download-permission администратора,
  а также повторной проверки SHA-256.
- Аудитировать серверную выдачу уже проверенных bytes отдельным типизированным
  append-only event, а не generic update log. Это не утверждает, что клиент
  полностью сохранил HTTP response.
- Включить private root в единый backup/restore format v2; старые v1 backup
  остаются читаемыми.
- До отдельного решения хранить артефакты бессрочно и не реализовывать delete
  path.

### Дополнение 2026-08-11

- Production разделяет migration/restore owner и runtime DB-role; web не
  владеет schema/functions, не имеет DDL, database `TEMPORARY`, role
  memberships или записи в `django_migrations`. На append-only таблицах ему
  доступны только `SELECT/INSERT`; сериализация идет через изменяемый корень
  `DonorReport`. `EXECUTE` на application functions отозван у `PUBLIC` и runtime,
  кроме фиксированного allowlist необходимых trigger validation helpers.
- Backup/restore/deploy/migration делят host-level lock. Backup публикуется
  только после `fsync`, а restore удерживает rollback до health-check и
  durable-состояния `validated`; только затем открывается Caddy.
- Runtime не имеет `UPDATE/DELETE/TRUNCATE` на неизменяемой истории. Такая
  история защищает от переписывания и удаления, но не является
  криптографической non-repudiation: скомпрометированный runtime SQL может
  дописать событие с существующим `actor_id`. До появления подписанного
  отдельного writer-контура runtime credentials и web-процесс остаются
  доверительной границей.
- Прерванный backup и restore имеют раздельные durable markers и явные
  повторяемые `--recover`-пути. Restore-кандидат запускается только через
  отдельный Compose override; обычный production Compose фиксирует bypass в
  выключенном состоянии. Recovery принимает валидный fsynced temp-only marker,
  но неизвестный `.restore-*` и невалидное состояние сохраняет без cleanup.
- Ошибка deploy после начала изменения release не пытается автоматически
  переоткрыть непроверенную версию: web и Caddy остаются остановленными.

## Альтернативы

### Сохранить файл в обычном `MEDIA_ROOT`

Отклонено: Caddy обслуживает этот каталог без Django authorization и аудита.

### Хранить бинарный файл в PostgreSQL

Отклонено: увеличивает размер transactional backup и стоимость чтения, не
дает преимущества для локального неизменяемого файла и усложняет будущий
переход на object storage.

### Сразу выбрать внешний S3

Отложено: поставщик, ключи, offsite backup и эксплуатационный владелец не
согласованы. Модель с opaque storage key и отдельным service boundary оставляет
такую миграцию возможной без изменения submission-контракта.

### Использовать только generic auditlog

Отклонено: download является read-событием и не создает model update.
Юридически значимый доступ требует типизированного факта с actor, ролью,
основанием permission, временем и проверенным hash.

## Последствия

- Production получает третий сохраняемый набор данных: БД, public media и
  private artifacts должны резервироваться и восстанавливаться вместе.
- Web-процесс получает доступ к private volume, Caddy его не получает.
- Авария между publish файла и commit БД может оставить orphan, но не
  перезаписывает существующий объект; strict scan обнаруживает расхождение.
- Локальное хранение не заменяет offsite backup, мониторинг или внешний
  malware scanner. Оно также требует шифрования host/backup at rest,
  утвержденной retention policy. Разделение DB-ролей реализовано в
  коде, но до реального запуска оператор обязан задать разные credentials вне
  Git и получить успешный preflight.
