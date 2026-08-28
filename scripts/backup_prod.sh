#!/usr/bin/env bash

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=_production_common.sh
source "$SCRIPT_DIR/_production_common.sh"

MODE="backup"
if [[ $# -eq 1 && "$1" == "--recover" ]]; then
  MODE="recover"
elif [[ $# -ne 0 ]]; then
  production_fail "Usage: scripts/backup_prod.sh [--recover]"
fi

load_production_environment "${ENV_FILE:-$PRODUCTION_ROOT/.env.production}"
for variable in POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD POSTGRES_RUNTIME_USER POSTGRES_RUNTIME_PASSWORD; do
  require_environment_value "$variable"
done
acquire_production_maintenance_lock
assert_production_migration_not_running

BACKUP_DIR="${BACKUP_DIR:-$(environment_value BACKUP_DIR || printf '/var/backups/rm')}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-$(environment_value BACKUP_RETENTION_DAYS || printf '30')}"
MAX_DATABASE_BYTES="$(environment_value BACKUP_MAX_DATABASE_BYTES || printf '1099511627776')"
MAX_ARCHIVE_BYTES="$(environment_value BACKUP_MAX_ARCHIVE_BYTES || printf '21474836480')"
[[ "$BACKUP_DIR" == /* && "$BACKUP_DIR" != "/" ]] || production_fail \
  "BACKUP_DIR must be an absolute non-root path."
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || production_fail \
  "BACKUP_RETENTION_DAYS must be a non-negative integer."
[[ "$MAX_DATABASE_BYTES" =~ ^[1-9][0-9]{0,15}$ ]] || production_fail \
  "BACKUP_MAX_DATABASE_BYTES must be a bounded positive integer."
[[ "$MAX_ARCHIVE_BYTES" =~ ^[1-9][0-9]{0,15}$ ]] || production_fail \
  "BACKUP_MAX_ARCHIVE_BYTES must be a bounded positive integer."

mkdir -p -- "$BACKUP_DIR"
BACKUP_DIR="$(cd -- "$BACKUP_DIR" && pwd -P)"
BACKUP_MOUNT_PATH="$(compose_host_path "$BACKUP_DIR")"

recover_backup() {
  assert_no_incomplete_restore
  local state web_was_running
  production_compose run --rm --no-deps --user rehab:rehab volume-init \
    python /app/scripts/restore_files.py adopt-backup-state || \
    production_fail "No valid incomplete backup state exists."
  state="$(production_compose run --rm --no-deps --user rehab:rehab volume-init \
    cat /app/private-artifacts/.backup-in-progress)" || \
    production_fail "No incomplete backup state exists."
  web_was_running="$(printf '%s\n' "$state" | \
    sed -n 's/^WEB_WAS_RUNNING=//p' | tail -n 1)"
  [[ "$web_was_running" == "true" || "$web_was_running" == "false" ]] || \
    production_fail "Backup state contains an invalid web flag."
  if [[ "$web_was_running" == "true" ]]; then
    production_compose up -d --no-deps web
    wait_for_production_web_health || production_fail \
      "Web did not recover after an interrupted backup."
  else
    production_compose stop web > /dev/null
  fi
  find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d \
    -name '.partial-*' -exec rm -rf -- {} +
  production_compose run --rm --no-deps \
    -v "$BACKUP_MOUNT_PATH:/backup-host:ro" archive-maintenance \
    python /app/scripts/fsync_backup.py root
  production_compose run --rm --no-deps --user rehab:rehab volume-init \
    python /app/scripts/restore_files.py remove-backup-state
  printf 'Incomplete backup recovered.\n'
}

if [[ "$MODE" == "recover" ]]; then
  recover_backup
  exit 0
fi

assert_no_incomplete_maintenance
BACKUP_TEST_CRASH_AFTER_PARTIAL="${BACKUP_TEST_CRASH_AFTER_PARTIAL:-0}"
BACKUP_TEST_FAIL_AFTER_STATE_TEMP="${BACKUP_TEST_FAIL_AFTER_STATE_TEMP:-0}"
[[ "$BACKUP_TEST_CRASH_AFTER_PARTIAL" == "0" \
   || "$BACKUP_TEST_CRASH_AFTER_PARTIAL" == "1" ]] || production_fail \
  "BACKUP_TEST_CRASH_AFTER_PARTIAL must be 0 or 1."
if [[ "$BACKUP_TEST_CRASH_AFTER_PARTIAL" == "1" && "${CI:-}" != "true" ]]; then
  production_fail "Backup fault injection is restricted to CI."
fi
[[ "$BACKUP_TEST_FAIL_AFTER_STATE_TEMP" == "0" \
   || "$BACKUP_TEST_FAIL_AFTER_STATE_TEMP" == "1" ]] || production_fail \
  "BACKUP_TEST_FAIL_AFTER_STATE_TEMP must be 0 or 1."
if [[ "$BACKUP_TEST_FAIL_AFTER_STATE_TEMP" == "1" && "${CI:-}" != "true" ]]; then
  production_fail "Backup fault injection is restricted to CI."
fi
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL_DIR="$BACKUP_DIR/$STAMP"
[[ ! -e "$FINAL_DIR" ]] || production_fail "Backup timestamp collision; run again."

web_was_running=false
if production_service_running web; then
  web_was_running=true
fi
backup_state_injection=()
if [[ "$BACKUP_TEST_FAIL_AFTER_STATE_TEMP" == "1" ]]; then
  backup_state_injection=(--inject-before-publish)
fi
production_compose run --rm --no-deps --user rehab:rehab volume-init \
  python /app/scripts/restore_files.py write-backup-state \
    --web-was-running "$web_was_running" "${backup_state_injection[@]}"
backup_state_written=true
web_stopped=false
TMP_DIR=""
cleanup_partial() {
  local recovered=false
  if [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]]; then
    rm -rf -- "$TMP_DIR"
  fi
  if [[ "$backup_state_written" == true ]]; then
    if [[ "$web_was_running" == true ]]; then
      if production_compose up -d --no-deps web > /dev/null 2>&1 \
         && wait_for_production_web_health; then
        recovered=true
      fi
    else
      production_compose stop web > /dev/null 2>&1 || true
      recovered=true
    fi
    if [[ "$recovered" == true ]]; then
      production_compose run --rm --no-deps --user rehab:rehab volume-init \
        python /app/scripts/restore_files.py remove-backup-state \
        > /dev/null 2>&1 || true
    else
      printf 'Backup recovery marker retained; run backup_prod.sh --recover.\n' >&2
    fi
  fi
}
trap cleanup_partial EXIT

if [[ "$web_was_running" == true ]]; then
  production_compose stop web
  web_stopped=true
fi
production_compose run --rm --no-deps web \
  python manage.py audit_donor_report_submissions --strict --quiescent

DATABASE_SIZE_BYTES="$(
  production_compose exec -T db psql \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --tuples-only --no-align \
    --command='SELECT pg_database_size(current_database());' \
    | tr -d '[:space:]'
)"
[[ "$DATABASE_SIZE_BYTES" =~ ^[1-9][0-9]*$ ]] || \
  production_fail "PostgreSQL returned an invalid database size."
(( DATABASE_SIZE_BYTES <= MAX_DATABASE_BYTES )) || production_fail \
  "Database exceeds BACKUP_MAX_DATABASE_BYTES."

mapfile -t SOURCE_BYTES < <(
  production_compose run --rm --no-deps --user rehab:rehab volume-init sh -ec \
    "du -sb /app/media /app/private-artifacts | awk '{print \$1}'"
)
[[ "${#SOURCE_BYTES[@]}" -eq 2 \
   && "${SOURCE_BYTES[0]}" =~ ^[0-9]+$ \
   && "${SOURCE_BYTES[1]}" =~ ^[0-9]+$ ]] || \
  production_fail "Could not measure backup source volumes."
(( SOURCE_BYTES[0] <= MAX_ARCHIVE_BYTES \
   && SOURCE_BYTES[1] <= MAX_ARCHIVE_BYTES )) || production_fail \
  "A backup source exceeds BACKUP_MAX_ARCHIVE_BYTES."
REQUIRED_BYTES=$((DATABASE_SIZE_BYTES + SOURCE_BYTES[0] + SOURCE_BYTES[1]))
RESERVE_BYTES=$((REQUIRED_BYTES / 20))
(( RESERVE_BYTES >= 268435456 )) || RESERVE_BYTES=268435456
BACKUP_CAPACITY="$(
  production_compose run --rm --no-deps \
    -v "$BACKUP_MOUNT_PATH:/backup-host:ro" archive-maintenance \
    python /app/scripts/fsync_backup.py capacity \
    | tail -n 1 | tr -d '\r'
)"
IFS=: read -r AVAILABLE_BYTES AVAILABLE_INODES <<< "$BACKUP_CAPACITY"
[[ "$AVAILABLE_BYTES" =~ ^[0-9]+$ && "$AVAILABLE_INODES" =~ ^[0-9]+$ ]] || \
  production_fail "Could not inspect backup filesystem capacity."
(( AVAILABLE_BYTES >= REQUIRED_BYTES + RESERVE_BYTES )) || production_fail \
  "Insufficient free space for a complete unpublished backup."
(( AVAILABLE_INODES >= 32 )) || production_fail \
  "Insufficient free inodes for a complete backup."

TMP_DIR="$(mktemp -d "$BACKUP_DIR/.partial-${STAMP}.XXXXXX")"
TMP_BASENAME="$(basename -- "$TMP_DIR")"
if [[ "$BACKUP_TEST_CRASH_AFTER_PARTIAL" == "1" ]]; then
  kill -KILL "$$"
fi
DB_ARCHIVE="$TMP_DIR/db.dump"
MEDIA_ARCHIVE="$TMP_DIR/media.tar.gz"
PRIVATE_ARCHIVE="$TMP_DIR/private-artifacts.tar.gz"

production_compose exec -T db pg_dump --format=custom \
  --username="$POSTGRES_USER" "$POSTGRES_DB" > "$DB_ARCHIVE"
[[ -s "$DB_ARCHIVE" ]] || production_fail "Database archive is empty."
production_compose exec -T db pg_restore --list < "$DB_ARCHIVE" > /dev/null

production_compose run --rm --no-deps --user rehab:rehab volume-init \
  tar -C /app -czf - media > "$MEDIA_ARCHIVE"
[[ -s "$MEDIA_ARCHIVE" ]] || production_fail "Media archive is empty."
tar -tzf "$MEDIA_ARCHIVE" > /dev/null
production_compose run --rm --no-deps --user rehab:rehab volume-init \
  tar -C /app \
    --exclude='private-artifacts/.staging' \
    --exclude='private-artifacts/.restore-*' \
    --exclude='private-artifacts/.backup-*' \
    -czf - private-artifacts \
  > "$PRIVATE_ARCHIVE"
[[ -s "$PRIVATE_ARCHIVE" ]] || production_fail "Private artifact archive is empty."
tar -tzf "$PRIVATE_ARCHIVE" > /dev/null

(
  cd -- "$TMP_DIR"
  printf 'FORMAT=rm-backup-v2\n' > metadata.env
  printf 'CREATED_AT_UTC=%s\n' "$STAMP" >> metadata.env
  printf 'DATABASE_SIZE_BYTES=%s\n' "$DATABASE_SIZE_BYTES" >> metadata.env
  printf 'GIT_REVISION=%s\n' \
    "$(git -C "$PRODUCTION_ROOT" rev-parse HEAD 2>/dev/null || printf 'unavailable')" \
    >> metadata.env
  sha256sum db.dump media.tar.gz private-artifacts.tar.gz metadata.env > SHA256SUMS
)

production_compose run --rm --no-deps \
  -v "$BACKUP_MOUNT_PATH:/backup-host:ro" archive-maintenance \
  python /app/scripts/fsync_backup.py tree "$TMP_BASENAME"
mv -- "$TMP_DIR" "$FINAL_DIR"
TMP_DIR=""
production_compose run --rm --no-deps \
  -v "$BACKUP_MOUNT_PATH:/backup-host:ro" archive-maintenance \
  python /app/scripts/fsync_backup.py root

if [[ "$web_was_running" == true ]]; then
  production_compose up -d --no-deps web
  wait_for_production_web_health || production_fail \
    "Web did not become healthy after backup."
fi
production_compose run --rm --no-deps --user rehab:rehab volume-init \
  python /app/scripts/restore_files.py remove-backup-state
backup_state_written=false
web_stopped=false
trap - EXIT

if (( RETENTION_DAYS > 0 )); then
  find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d \
    -name '20[0-9][0-9]*T[0-9][0-9]*Z' -mtime "+$RETENTION_DAYS" \
    -exec rm -rf -- {} +
fi
production_compose run --rm --no-deps \
  -v "$BACKUP_MOUNT_PATH:/backup-host:ro" archive-maintenance \
  python /app/scripts/fsync_backup.py root

printf 'Verified backup created: %s\n' "$FINAL_DIR"
