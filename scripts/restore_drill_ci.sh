#!/usr/bin/env bash

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=_production_common.sh
source "$SCRIPT_DIR/_production_common.sh"

[[ "${CI:-}" == "true" ]] || production_fail "Restore drill is restricted to CI."
[[ ! -e "$PRODUCTION_ROOT/.env.production" ]] || production_fail \
  "Refusing to replace an existing .env.production."

DRILL_ROOT="$(mktemp -d "$PRODUCTION_ROOT/.restore-drill.XXXXXX")"
ENV_ARGUMENT="$DRILL_ROOT/restore-drill.env"
BACKUP_DIR="$DRILL_ROOT/backups"
COMPOSE_PROJECT_NAME="rmrestore${RANDOM}${RANDOM}"
export COMPOSE_PROJECT_NAME
services_started=false

cleanup() {
  if [[ "$services_started" == true ]]; then
    production_compose down --volumes --remove-orphans || true
  fi
  rm -f -- "$PRODUCTION_ROOT/.env.production"
  rm -rf -- "$DRILL_ROOT"
}
trap cleanup EXIT

mkdir -p -- "$BACKUP_DIR"
cat > "$ENV_ARGUMENT" <<EOF
APP_DOMAIN=restore-drill.test
DJANGO_SECRET_KEY=restore-drill-secret-not-production
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=restore-drill.test,localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://restore-drill.test
DJANGO_SECURE_SSL_REDIRECT=1
DJANGO_SECURE_HSTS_SECONDS=0
DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=0
DJANGO_SECURE_HSTS_PRELOAD=0
POSTGRES_DB=rehab_restore_drill
POSTGRES_USER=rehab_restore_drill
POSTGRES_PASSWORD=restore-drill-postgres-password
BACKUP_DIR=$BACKUP_DIR
BACKUP_RETENTION_DAYS=1
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.restore-drill.test
EMAIL_PORT=587
EMAIL_USE_TLS=1
EMAIL_HOST_USER=restore-drill@test
EMAIL_HOST_PASSWORD=restore-drill-smtp-password
DEFAULT_FROM_EMAIL=restore-drill@test
EOF
cp -- "$ENV_ARGUMENT" "$PRODUCTION_ROOT/.env.production"
load_production_environment "$ENV_ARGUMENT"

wait_for_web() {
  local attempt
  for attempt in $(seq 1 60); do
    if production_compose exec -T web python -c \
      "import urllib.request; request = urllib.request.Request('http://127.0.0.1:8000/healthz/', headers={'X-Forwarded-Proto': 'https'}); urllib.request.urlopen(request, timeout=5)" \
      > /dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  production_compose logs web >&2 || true
  production_fail "Web did not become healthy during restore drill."
}

production_compose up -d --build db redis web
services_started=true
wait_for_web

production_compose exec -T web python manage.py shell -c \
  "from django.contrib.auth import get_user_model; get_user_model().objects.create_user(username='restore-drill-before')"
production_compose exec -T web sh -ec "printf before > /app/media/restore-drill.txt"
ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/backup_prod.sh"

BACKUP_PATH="$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '20[0-9][0-9]*T[0-9][0-9]*Z' -print -quit)"
[[ -n "$BACKUP_PATH" ]] || production_fail "Restore drill backup was not created."
ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/verify_backup_prod.sh" "$BACKUP_PATH"

CORRUPT_PATH="$DRILL_ROOT/corrupt-backup"
cp -a -- "$BACKUP_PATH" "$CORRUPT_PATH"
printf x >> "$CORRUPT_PATH/db.dump"
if ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/restore_prod.sh" --confirm "$CORRUPT_PATH"; then
  production_fail "Restore accepted a corrupt archive."
fi
wait_for_web

production_compose exec -T web python manage.py shell -c \
  "from django.contrib.auth import get_user_model; get_user_model().objects.create_user(username='restore-drill-after')"
production_compose exec -T web sh -ec "printf after > /app/media/restore-drill.txt"

ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/restore_prod.sh" --confirm "$BACKUP_PATH"
wait_for_web
production_compose exec -T web python manage.py shell -c \
  "from django.contrib.auth import get_user_model; User = get_user_model(); assert User.objects.filter(username='restore-drill-before').exists(); assert not User.objects.filter(username='restore-drill-after').exists()"
production_compose exec -T web sh -ec 'test "$(cat /app/media/restore-drill.txt)" = before'

printf 'Production backup and restore drill passed.\n'
