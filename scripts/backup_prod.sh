#!/usr/bin/env sh
set -eu

if [ -f .env.production ]; then
  set -a
  . ./.env.production
  set +a
fi

BACKUP_DIR="${BACKUP_DIR:-./backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"

docker compose -f compose.prod.yaml exec -T db pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" | gzip > "$BACKUP_DIR/postgres-$STAMP.sql.gz"
docker compose -f compose.prod.yaml exec -T web tar -czf - media > "$BACKUP_DIR/media-$STAMP.tar.gz"

find "$BACKUP_DIR" -name "postgres-*.sql.gz" -mtime +30 -delete
find "$BACKUP_DIR" -name "media-*.tar.gz" -mtime +30 -delete

echo "Backup created in $BACKUP_DIR"
