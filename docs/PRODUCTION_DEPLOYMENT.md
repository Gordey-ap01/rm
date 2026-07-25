# Продакшен-размещение

Цель: постоянный доступ к системе по домену, без Cloudflare Quick Tunnel, ngrok, локальной сети и открытых баз данных.

## Рекомендуемая схема

- VPS или облачный сервер в российском дата-центре.
- Домен вида `rm.example.ru`.
- Docker Compose на сервере.
- Caddy как reverse proxy с автоматическим HTTPS.
- Django + Gunicorn.
- PostgreSQL внутри закрытой Docker-сети.
- Ежедневные бэкапы PostgreSQL и media-файлов.

Смартфон, ноутбук администратора и кабинет специалиста открывают один и тот же адрес: `https://ваш-домен`.

## Почему не туннели

Cloudflare Quick Tunnel и ngrok хороши для короткой демонстрации, но не для эксплуатации:

- ссылка временная;
- нет гарантии доступности;
- при сетевых ограничениях возможны ошибки 1033 и разрывы;
- сложно объяснить администратору, почему доступ зависит от открытой консоли.

Для постоянной работы нужна обычная серверная схема: домен → HTTPS → приложение.

## Хостинг

Для этого проекта нужен VPS с Docker. Минимум:

- 2 vCPU;
- 4 GB RAM;
- 40 GB NVMe;
- Ubuntu 24.04 LTS;
- публичный IPv4;
- автоматические снапшоты или отдельное резервное хранилище.

Рабочие варианты:

- Timeweb Cloud: проще для старта, есть серверы, домены, S3 и заявленное соответствие 152-ФЗ УЗ 1.
- Selectel: серьёзнее для бизнеса, сильная инфраструктура, удобен, если нужен рост и регуляторика.
- Beget VPS: проще и дешевле, есть VPS с предустановленным Docker.

Мой выбор для первого настоящего запуска: Timeweb Cloud или Selectel. Для проекта с персональными данными получателей не надо начинать с самого дешёвого случайного VPS.

## Домен

Лучше купить отдельный домен:

- `radost-moya.ru`, если свободен;
- или поддомен на уже существующем домене организации, например `crm.radostmoya.ru`.

DNS-записи:

- `A crm.radostmoya.ru → IP_СЕРВЕРА`
- при необходимости `A www.crm.radostmoya.ru → IP_СЕРВЕРА`

## Подготовка сервера

На сервере:

```bash
apt update
apt install -y ca-certificates curl git ufw
curl -fsSL https://get.docker.com | sh
apt install -y docker-compose-plugin
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
```

## Развёртывание

```bash
git clone https://github.com/Gordey-ap01/rm.git /opt/rm
cd /opt/rm
cp .env.production.example .env.production
nano .env.production
```

В `.env.production` обязательно поменять:

- `APP_DOMAIN`;
- `DJANGO_SECRET_KEY`;
- `DJANGO_ALLOWED_HOSTS`;
- `DJANGO_CSRF_TRUSTED_ORIGINS`;
- `POSTGRES_PASSWORD`;
- `BACKUP_DIR` на существующий абсолютный каталог вне рабочего дерева;
- `BACKUP_RETENTION_DAYS`;
- SMTP-настройки для писем.

Файл содержит только однострочные значения переменных окружения, без shell-кода.
Не использовать `CHANGE_ME`, домены `example`/`invalid` и development passwords:
`production_preflight.sh` намеренно их отклоняет.

Запуск:

```bash
./scripts/production_preflight.sh --config-only
docker compose --env-file .env.production -f compose.prod.yaml up -d --build
docker compose --env-file .env.production -f compose.prod.yaml exec web python manage.py createsuperuser
./scripts/production_preflight.sh
```

Проверка:

```bash
docker compose --env-file .env.production -f compose.prod.yaml ps
docker compose --env-file .env.production -f compose.prod.yaml logs -f web
curl --fail https://ваш-домен/healthz/
```

После этого открыть:

```text
https://ваш-домен
```

## Бэкапы

Ручной запуск:

```bash
./scripts/backup_prod.sh
./scripts/verify_backup_prod.sh /absolute/path/to/backup
```

Один backup - это atomic timestamp-каталог с `db.dump` в custom PostgreSQL
format, `media.tar.gz`, `SHA256SUMS` и metadata. Скрипт проверяет оба архива до
публикации набора. Не удалять `.partial-*` вручную во время работающего backup.

Cron для ежедневного бэкапа в 03:20:

```bash
crontab -e
```

Добавить:

```cron
20 3 * * * cd /opt/rm && /usr/bin/env bash scripts/backup_prod.sh >> /var/log/rm-backup.log 2>&1
```

Важно: локальные бэкапы на том же сервере защищают от ошибки в базе, но не защищают от потери сервера. Для серьёзной эксплуатации нужен второй слой: S3/объектное хранилище или выгрузка на другой сервер.

## Восстановление

Команда ниже разрушительна: она останавливает только `web`, пересоздаёт БД и
заменяет media. До неё руководитель должен подтвердить окно работ, а оператор -
создать свежий backup текущего состояния, если это возможно.

```bash
./scripts/verify_backup_prod.sh /absolute/path/to/backup
./scripts/restore_prod.sh --confirm /absolute/path/to/backup
./scripts/production_preflight.sh
```

Не использовать restore на production как первый тест. В CI уже выполняется
изолированный restore-drill; staging drill перед первым реальным запуском всё
равно обязателен. После выбора владельца monitoring настроить внешнюю проверку
`https://ваш-домен/healthz/`; до этого сохранить cron-логи backup и финансовой
проверки и назначить ответственного за их просмотр.

## Финансовая проверка

Ручной эксплуатационный запуск сохраненной проверки финансовой целостности:

```bash
docker compose --env-file .env.production -f compose.prod.yaml exec web python manage.py run_financial_integrity_check --run-type management_command
```

Ночной cron-вариант, например в 04:10:

```cron
10 4 * * * cd /opt/rm && docker compose --env-file .env.production -f compose.prod.yaml exec -T web python manage.py run_financial_integrity_check --run-type scheduled >> /var/log/rm-financial-integrity.log 2>&1
```

Команда не исправляет финансовые данные автоматически. Она обновляет `FinancialIntegrityCheckRun` и `FinancialIntegrityFinding`; последняя проверка и failed-run message видны администратору в очереди работ в блоке финансового контроля.

## Обновление системы

```bash
cd /opt/rm
git pull
docker compose --env-file .env.production -f compose.prod.yaml up -d --build
docker compose --env-file .env.production -f compose.prod.yaml exec web python manage.py migrate
```

## Что нельзя делать

- Нельзя открывать PostgreSQL наружу.
- Нельзя работать с `DJANGO_DEBUG=1`.
- Нельзя использовать тестовый пароль `admin12345` в реальной эксплуатации.
- Нельзя хранить единственный бэкап на том же сервере.
- Нельзя давать всем одну учётную запись администратора.

## Следующий уровень

После первого продакшен-запуска нужно добавить:

- нормальную почту организации;
- автоматическую выгрузку бэкапов в S3;
- журнал доступа и регламент ролей;
- отдельные аккаунты администратора, руководителя и специалистов;
- мониторинг доступности;
- документ по персональным данным и доступам.
