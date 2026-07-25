# Контракт среза: production preflight, восстановимость и наблюдаемость

Дата: 2026-07-26

Статус: implemented

## Основание

`docs/01-prd.md` требует backup/restore, SMTP, monitoring и production
preflight до реального запуска. В репозитории уже есть Docker Compose, Caddy и
`scripts/backup_prod.sh`, но существующий backup не доказывает пригодность
архива к восстановлению: сбой первой команды в shell pipeline может остаться
незамеченным, DB и media не образуют атомарный набор, а документация не даёт
проверяемого restore-пути.

Для реализации используются документированные механизмы Django 5.2 для
view/URLconf и PostgreSQL 17 `pg_dump -Fc`/`pg_restore --list`:

- https://docs.djangoproject.com/en/5.2/topics/http/views/
- https://docs.djangoproject.com/en/5.2/topics/http/urls/
- https://www.postgresql.org/docs/17/app-pgdump.html
- https://www.postgresql.org/docs/17/app-pgrestore.html

## Граница среза

Входит:

1. Public `GET /healthz/`, который не раскрывает данные центра, проверяет
   доступность БД и возвращает `200` либо `503`.
2. Docker healthcheck web-контейнера и запуск Caddy после готовности web.
3. Набор резервной копии одного момента: custom PostgreSQL archive, media
   archive, SHA-256 checksum manifest и metadata в одной timestamp-папке.
4. Проверка архива через `pg_restore --list` и `tar -tzf` до публикации набора.
5. Restore-script, который по умолчанию ничего не меняет и требует явный
   `--confirm`; до DB/media он проверяет checksum и формат архивов.
6. Preflight-script: Docker Compose config, обязательные production variables,
   отсутствие development placeholders, Django `check --deploy`, готовность
   БД, health endpoint и SMTP connection без отправки письма.
7. Обновлённый runbook: расписание backup/financial-integrity, restore drill,
   monitoring/alerting ownership и rollback границы.

Не входит:

- новая модель, миграция, изменение финансовых фактов или пользовательских
  ролей;
- автоматический restore в production, restore без явного подтверждения,
  откат миграций или удаление production данных по таймеру;
- выбор и подключение S3, Uptime Kuma, Telegram, SMTP-провайдера или другого
  внешнего сервиса без одобрения владельца и его секретов;
- обещание, что один серверный backup защищает от потери сервера.

## Инварианты

| Область | Инвариант |
| --- | --- |
| Health | Ответ не требует логина и не содержит версии, секретов, PII, имени БД или stack trace. |
| DB health | Недоступная БД даёт `503`, а не ложный `200`. |
| Backup | В итоговом каталоге либо есть проверяемые DB+media+manifest вместе, либо нет ничего. |
| Archive | `db.dump` имеет custom format и проходит `pg_restore --list`; `media.tar.gz` проходит `tar -tzf`. |
| Integrity | Restore и verify сверяют checksum до любого разрушительного действия. |
| Restore | Без точного `--confirm` и абсолютного validated path скрипт завершается до остановки web/изменения DB/media. |
| Secrets | Скрипты не печатают значения `.env.production`; файлы backup создаются с ограниченными правами. |
| SMTP | Preflight только открывает и закрывает SMTP connection, не отправляя получателям тестовое письмо. |

## Backup формат

Один backup — каталог `<BACKUP_DIR>/<UTC timestamp>/`:

```text
db.dump          PostgreSQL custom archive (`pg_dump -Fc`)
media.tar.gz     каталог /app/media
SHA256SUMS       sha256 для обоих файлов
metadata.env     timestamp, format, application revision; без секретов
```

Набор создаётся в sibling temporary directory с `umask 077`; только после
проверки обоих архивов, checksum и metadata он переименовывается в конечный
timestamp. Retention удаляет только полные старые каталоги внутри
валидированного `BACKUP_DIR`.

## Restore политика

`scripts/restore_prod.sh --confirm /absolute/path/to/backup` — намеренно
разрушительная команда. До неё оператор обязан:

1. остановить обычные операции и сообщить центру окно восстановления;
2. сделать новый проверенный backup текущего состояния, если это возможно;
3. проверить каталог backup отдельной `verify_backup_prod.sh`;
4. зафиксировать причину и ответственного вне Git.

Скрипт проверяет checksum и оба архива, останавливает только web, создаёт
чистую БД, выполняет `pg_restore --no-owner --no-privileges`, заменяет media,
запускает web и требует последующего `production_preflight.sh`. Ошибка не
переходит к следующему шагу: нет «best effort» восстановления и нет rollback
поверх уже восстановленной DB.

Restore проводится сначала только на disposable staging/restore-drill
окружении. Production restore требует отдельного согласования руководителя.

## Внешние границы

Health endpoint делает возможным внешний monitoring, но поставщик alerting
выбирается владельцем центра. До его выбора обязательны cron-log проверки и
ручная проверка `/healthz/`; после выбора подключаются только минимальные
webhook/credentials вне репозитория.

S3/offsite copy также оставлена отдельным контрактом: перенос персональных
данных к провайдеру и retention-политика требуют выбора региона, договора и
ответственного. Локальный backup не считается аварийным планом потери VPS.

## Факт реализации

- Добавлен минимальный публичный `GET /healthz/`: только `{"status": "ok"}` или
  `{"status": "unavailable"}`, без PII, версии и диагностических деталей.
- `web` имеет Docker healthcheck, а Caddy ожидает его готовности. Внутренняя
  проверка передаёт `X-Forwarded-Proto: https`, поэтому не ослабляет
  `DJANGO_SECURE_SSL_REDIRECT` и соответствует реальному reverse proxy.
- `backup_prod.sh`, `verify_backup_prod.sh`, `restore_prod.sh` и
  `production_preflight.sh` работают с `.env.production` без её выполнения
  через shell. Restore намеренно требует `--confirm` и абсолютный путь.
- CI проверяет Bash syntax, Compose config, отсутствие drift между
  `pyproject.toml` и `requirements.txt`, а также отдельный Docker restore-drill.
  Drill создаёт данные и media, сохраняет backup, проверяет отказ повреждённого
  архива, меняет данные и доказывает восстановление исходных DB+media.
- В Docker build context исключены локальные `.env`, данные, media, документы и
  Graphify-артефакты. Это не заменяет offsite backup, но исключает их случайное
  включение в production image.

Локальный disposable drill успешно выполнен 2026-07-26. Реальные SMTP и
внешний monitoring не выполнялись: для них требуются выбранный провайдер,
ответственный и секреты вне Git.

## Проверки приемки

1. `GET /healthz/` возвращает минимальный JSON `200` при доступной БД и `503`
   при ошибке подключения; оба ответа покрыты Django tests.
2. `compose.prod.yaml` имеет web healthcheck; Caddy зависит от health web, не
   от простого процесса Gunicorn.
3. Backup-script имеет bash syntax test и disposable drill: DB archive и media
   archive проверяются средствами PostgreSQL/tar до публикации.
4. Restore-script без `--confirm`, с bad checksum и с relative path отказывает
   до любого compose stop/restore; success drill выполняется только в
   disposable окружении.
5. Preflight определяет DEBUG/default placeholder/неполный SMTP/неверный
   compose config как ошибку; с полностью настроенным ephemeral окружением
   проходит `check --deploy`, DB и health.
6. CI, полный pytest и `docker compose config` проходят. Реальный SMTP и
   внешний monitoring проверяются только после выдачи production access.

## Опасные точки миграции

Изменений схемы и миграций нет. Самая опасная операция — не кодовая миграция,
а restore. Поэтому он не доступен из web UI, не вызывается CI, не имеет
автоматического расписания и требует отдельного explicit command.

## Параллельная работа

После этого контракта можно безопасно разделить работу:

- один агент: health view/URL/tests;
- второй агент: shell scripts и disposable drill;
- третий агент: runbook/read-only security review.

Никто в этом срезе не меняет `operations/models.py` или migration chain.
Один ведущий агент объединяет результат и владеет compose/deploy контрактом.
