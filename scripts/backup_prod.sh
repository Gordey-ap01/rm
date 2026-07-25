#!/usr/bin/env bash

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=_production_common.sh
source "$SCRIPT_DIR/_production_common.sh"

load_production_environment "${ENV_FILE:-$PRODUCTION_ROOT/.env.production}"
for variable in POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD; do
  require_environment_value "$variable"
done

BACKUP_DIR="${BACKUP_DIR:-$(environment_value BACKUP_DIR || printf '/var/backups/rm')}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-$(environment_value BACKUP_RETENTION_DAYS || printf '30')}"
[[ "$BACKUP_DIR" == /* && "$BACKUP_DIR" != "/" ]] || production_fail "BACKUP_DIR must be an absolute non-root path."
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || production_fail "BACKUP_RETENTION_DAYS must be a non-negative integer."

mkdir -p -- "$BACKUP_DIR"
BACKUP_DIR="$(cd -- "$BACKUP_DIR" && pwd -P)"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL_DIR="$BACKUP_DIR/$STAMP"
[[ ! -e "$FINAL_DIR" ]] || production_fail "Backup timestamp collision; run again."

TMP_DIR="$(mktemp -d "$BACKUP_DIR/.partial-${STAMP}.XXXXXX")"
cleanup_partial() {
  rm -rf -- "$TMP_DIR"
}
trap cleanup_partial EXIT

DB_ARCHIVE="$TMP_DIR/db.dump"
MEDIA_ARCHIVE="$TMP_DIR/media.tar.gz"

production_compose exec -T db pg_dump --format=custom --username="$POSTGRES_USER" "$POSTGRES_DB" > "$DB_ARCHIVE"
[[ -s "$DB_ARCHIVE" ]] || production_fail "Database archive is empty."
production_compose exec -T db pg_restore --list < "$DB_ARCHIVE" > /dev/null

production_compose exec -T web tar -C /app -czf - media > "$MEDIA_ARCHIVE"
[[ -s "$MEDIA_ARCHIVE" ]] || production_fail "Media archive is empty."
tar -tzf "$MEDIA_ARCHIVE" > /dev/null

(
  cd -- "$TMP_DIR"
  sha256sum db.dump media.tar.gz > SHA256SUMS
  printf 'FORMAT=rm-backup-v1\n' > metadata.env
  printf 'CREATED_AT_UTC=%s\n' "$STAMP" >> metadata.env
  printf 'GIT_REVISION=%s\n' "$(git -C "$PRODUCTION_ROOT" rev-parse HEAD 2>/dev/null || printf 'unavailable')" >> metadata.env
)

mv -- "$TMP_DIR" "$FINAL_DIR"
trap - EXIT

if (( RETENTION_DAYS > 0 )); then
  find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '20[0-9][0-9]*T[0-9][0-9]*Z' -mtime "+$RETENTION_DAYS" -exec rm -rf -- {} +
fi

printf 'Verified backup created: %s\n' "$FINAL_DIR"
