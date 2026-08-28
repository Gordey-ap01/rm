#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=_production_common.sh
source "$SCRIPT_DIR/_production_common.sh"

if [[ $# -ne 1 ]]; then
  printf 'Usage: %s /absolute/path/to/backup\n' "$(basename -- "$0")" >&2
  exit 2
fi

load_production_environment "${ENV_FILE:-$PRODUCTION_ROOT/.env.production}"
MAX_ARCHIVE_BYTES="$(environment_value BACKUP_MAX_ARCHIVE_BYTES || printf '21474836480')"
MAX_DATABASE_BYTES="$(environment_value BACKUP_MAX_DATABASE_BYTES || printf '1099511627776')"
[[ "$MAX_ARCHIVE_BYTES" =~ ^[1-9][0-9]*$ ]] || production_fail \
  "BACKUP_MAX_ARCHIVE_BYTES must be a positive integer."
[[ "$MAX_DATABASE_BYTES" =~ ^[1-9][0-9]{0,15}$ ]] || production_fail \
  "BACKUP_MAX_DATABASE_BYTES must be a bounded positive integer."
BACKUP_PATH="$(require_absolute_directory "$1")"
for required_file in db.dump media.tar.gz SHA256SUMS metadata.env; do
  [[ -f "$BACKUP_PATH/$required_file" ]] || production_fail "Backup is missing $required_file."
done
FORMAT="$(grep -E '^FORMAT=' "$BACKUP_PATH/metadata.env" | tail -n 1 | cut -d= -f2-)"
case "$FORMAT" in
  rm-backup-v1)
    checksum_files=(db.dump media.tar.gz)
    ;;
  rm-backup-v2)
    [[ -f "$BACKUP_PATH/private-artifacts.tar.gz" ]] || production_fail \
      "Backup is missing private-artifacts.tar.gz."
    DATABASE_SIZE_BYTES="$(grep -E '^DATABASE_SIZE_BYTES=' "$BACKUP_PATH/metadata.env" | tail -n 1 | cut -d= -f2-)"
    [[ "$DATABASE_SIZE_BYTES" =~ ^[1-9][0-9]*$ ]] || production_fail \
      "Backup metadata has an invalid database size."
    (( DATABASE_SIZE_BYTES <= MAX_DATABASE_BYTES )) || production_fail \
      "Backup database size exceeds BACKUP_MAX_DATABASE_BYTES."
    checksum_files=(db.dump media.tar.gz private-artifacts.tar.gz metadata.env)
    ;;
  *)
    production_fail "Unsupported backup format."
    ;;
esac

(
  cd -- "$BACKUP_PATH"
  cmp -s <(sha256sum "${checksum_files[@]}") SHA256SUMS || production_fail "Backup checksum verification failed."
)

production_compose exec -T db pg_restore --list < "$BACKUP_PATH/db.dump" > /dev/null
tar -tzf "$BACKUP_PATH/media.tar.gz" > /dev/null
BACKUP_MOUNT_PATH="$(compose_host_path "$BACKUP_PATH")"
production_compose run --rm --no-deps \
  -v "$BACKUP_MOUNT_PATH:/backup:ro" archive-maintenance \
  python manage.py validate_backup_archive /backup/media.tar.gz --root media \
    --max-uncompressed-bytes "$MAX_ARCHIVE_BYTES"
if [[ "$FORMAT" == "rm-backup-v2" ]]; then
  tar -tzf "$BACKUP_PATH/private-artifacts.tar.gz" > /dev/null
  production_compose run --rm --no-deps \
    -v "$BACKUP_MOUNT_PATH:/backup:ro" archive-maintenance \
    python manage.py validate_backup_archive \
      /backup/private-artifacts.tar.gz --root private-artifacts \
      --max-uncompressed-bytes "$MAX_ARCHIVE_BYTES"
fi

printf 'Backup verified (%s): %s\n' "$FORMAT" "$BACKUP_PATH"
