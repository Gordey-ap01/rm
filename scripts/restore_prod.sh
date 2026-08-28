#!/usr/bin/env bash

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=_production_common.sh
source "$SCRIPT_DIR/_production_common.sh"

usage() {
  printf 'Usage:\n' >&2
  printf '  %s --confirm /absolute/path/to/backup\n' "$(basename -- "$0")" >&2
  printf '  %s --recover --confirm\n' "$(basename -- "$0")" >&2
}

MODE=""
BACKUP_ARGUMENT=""
if [[ $# -eq 2 && "$1" == "--confirm" ]]; then
  MODE="restore"
  BACKUP_ARGUMENT="$2"
elif [[ $# -eq 2 && "$1" == "--recover" && "$2" == "--confirm" ]]; then
  MODE="recover"
else
  usage
  printf 'Restore replaces the production database, media and private artifacts.\n' >&2
  exit 2
fi

load_production_environment "${ENV_FILE:-$PRODUCTION_ROOT/.env.production}"
for variable in POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD POSTGRES_RUNTIME_USER POSTGRES_RUNTIME_PASSWORD; do
  require_environment_value "$variable"
done
acquire_production_maintenance_lock
assert_production_migration_not_running
if [[ "$MODE" == "restore" ]]; then
  assert_no_incomplete_maintenance
else
  assert_no_incomplete_backup
fi
for fault_variable in \
  RESTORE_TEST_FAIL_AFTER_DATABASE_SWITCH \
  RESTORE_TEST_FAIL_AFTER_FILE_SWITCH \
  RESTORE_TEST_FAIL_AFTER_CANDIDATE_HEALTH \
  RESTORE_TEST_FAIL_AFTER_ARCHIVE_EXTRACT \
  RESTORE_TEST_FAIL_AFTER_STATE_TEMP \
  RESTORE_TEST_FAIL_DURING_FILE_ROLLBACK; do
  fault_value="${!fault_variable:-0}"
  [[ "$fault_value" == "0" || "$fault_value" == "1" ]] || \
    production_fail "$fault_variable must be 0 or 1."
  if [[ "$fault_value" == "1" && "${CI:-}" != "true" ]]; then
    production_fail "Restore fault injection is restricted to CI."
  fi
done

RESTORE_STATE_PATH="/app/private-artifacts/.restore-in-progress"
STAGED_DB=""
ROLLBACK_DB=""
CADDY_WAS_RUNNING="false"
WEB_WAS_RUNNING="false"
RESTORE_STATUS="preparing"

restore_state_exists() {
  production_compose run --rm --no-deps --user rehab:rehab volume-init \
    test -f "$RESTORE_STATE_PATH" \
    > /dev/null 2>&1
}

write_restore_state() {
  local status="$1"
  local injection_argument=()
  if [[ "$status" == "preparing" \
     && "${RESTORE_TEST_FAIL_AFTER_STATE_TEMP:-0}" == "1" ]]; then
    injection_argument=(--inject-before-publish)
  fi
  production_compose run --rm --no-deps --user rehab:rehab \
    volume-init python /app/scripts/restore_files.py write-state \
      --staged-db "$STAGED_DB" \
      --rollback-db "$ROLLBACK_DB" \
      --caddy-was-running "$CADDY_WAS_RUNNING" \
      --web-was-running "$WEB_WAS_RUNNING" \
      --status "$status" "${injection_argument[@]}" > /dev/null
}

state_value() {
  local state="$1"
  local name="$2"
  printf '%s\n' "$state" | sed -n "s/^${name}=//p" | tail -n 1
}

load_restore_state() {
  local state
  state="$(production_compose run --rm --no-deps --user rehab:rehab \
    volume-init cat "$RESTORE_STATE_PATH")"
  STAGED_DB="$(state_value "$state" STAGED_DB)"
  ROLLBACK_DB="$(state_value "$state" ROLLBACK_DB)"
  CADDY_WAS_RUNNING="$(state_value "$state" CADDY_WAS_RUNNING)"
  WEB_WAS_RUNNING="$(state_value "$state" WEB_WAS_RUNNING)"
  RESTORE_STATUS="$(state_value "$state" STATUS)"
  [[ "$STAGED_DB" =~ ^rm_restore_stage_[0-9]{14}_[0-9]+$ ]] || \
    production_fail "Restore state contains an invalid staged database name."
  [[ "$ROLLBACK_DB" =~ ^rm_restore_rollback_[0-9]{14}_[0-9]+$ ]] || \
    production_fail "Restore state contains an invalid rollback database name."
  [[ "$CADDY_WAS_RUNNING" == "true" || "$CADDY_WAS_RUNNING" == "false" ]] || \
    production_fail "Restore state contains an invalid Caddy flag."
  [[ "$WEB_WAS_RUNNING" == "true" || "$WEB_WAS_RUNNING" == "false" ]] || \
    production_fail "Restore state contains an invalid web flag."
  [[ "$RESTORE_STATUS" == "preparing" \
     || "$RESTORE_STATUS" == "candidate" \
     || "$RESTORE_STATUS" == "validated" ]] || \
    production_fail "Restore state contains an invalid status."
}

database_exists() {
  local database_name="$1"
  local result
  [[ "$database_name" =~ ^rm_restore_(stage|rollback)_[0-9]{14}_[0-9]+$ ]] || \
    production_fail "Refusing to inspect an unsafe restore database name."
  if ! result="$(
    production_compose exec -T db psql \
      --username "$POSTGRES_USER" \
      --dbname postgres \
      --set=ON_ERROR_STOP=1 \
      --tuples-only --no-align \
      --command="SELECT 1 FROM pg_database WHERE datname = '$database_name';"
  )"; then
    production_fail "Could not inspect restore database state."
  fi
  [[ "$(printf '%s' "$result" | tr -d '[:space:]')" == "1" ]]
}

drop_database_if_exists() {
  local database_name="$1"
  production_compose exec -T db dropdb \
    --if-exists --force \
    --maintenance-db=postgres \
    --username "$POSTGRES_USER" \
    "$database_name"
}

rollback_databases() {
  if database_exists "$ROLLBACK_DB"; then
    production_compose exec -T db psql \
      --username "$POSTGRES_USER" \
      --dbname postgres \
      --set=ON_ERROR_STOP=1 \
      --set=live_db="$POSTGRES_DB" \
      --set=staged_db="$STAGED_DB" \
      --set=rollback_db="$ROLLBACK_DB" <<'SQL'
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname IN (:'live_db', :'staged_db', :'rollback_db')
  AND pid <> pg_backend_pid();
BEGIN;
ALTER DATABASE :"live_db" RENAME TO :"staged_db";
ALTER DATABASE :"rollback_db" RENAME TO :"live_db";
COMMIT;
SQL
  fi
  drop_database_if_exists "$STAGED_DB"
}

rollback_file_roots() {
  local inject_argument=()
  if [[ "${RESTORE_TEST_FAIL_DURING_FILE_ROLLBACK:-0}" == "1" ]]; then
    inject_argument=(--inject-after-copy)
  fi
  production_compose run --rm --no-deps volume-init \
    python /app/scripts/restore_files.py rollback-root \
      --root /app/media "${inject_argument[@]}"
  production_compose run --rm --no-deps volume-init \
    python /app/scripts/restore_files.py rollback-root \
      --root /app/private-artifacts
  production_compose run --rm --no-deps volume-init sh -ec '
    set -eu
    chown -R rehab:rehab /app/media /app/private-artifacts
    find /app/private-artifacts -type d -exec chmod 0700 {} +
    find /app/private-artifacts -type f -exec chmod 0600 {} +
  '
}

cleanup_restore_roots() {
  production_compose run --rm --no-deps volume-init \
    python /app/scripts/restore_files.py cleanup-root --root /app/media
  production_compose run --rm --no-deps volume-init \
    python /app/scripts/restore_files.py cleanup-root --root /app/private-artifacts
}

remove_restore_state() {
  production_compose run --rm --no-deps --user rehab:rehab volume-init \
    python /app/scripts/restore_files.py remove-state
}

start_restored_services() {
  if [[ "$WEB_WAS_RUNNING" == "true" ]]; then
    production_compose up -d --no-deps --force-recreate web
    wait_for_production_web_health || production_fail \
      "Recovered web service did not pass its health check."
  else
    production_compose up -d --no-deps --force-recreate web
    wait_for_production_web_health || production_fail \
      "Validated web service did not pass its health check."
    production_compose stop web > /dev/null
  fi
  if [[ "$CADDY_WAS_RUNNING" == "true" && "$WEB_WAS_RUNNING" == "true" ]]; then
    production_compose up -d --no-deps caddy
    production_service_running caddy || production_fail \
      "Caddy did not restart after restore validation."
  else
    production_compose stop caddy > /dev/null
  fi
}

start_and_validate_candidate() {
  local healthy=false
  production_restore_compose up -d --no-deps web
  for _attempt in $(seq 1 30); do
    if production_compose exec -T web python -c \
      "import urllib.request; request = urllib.request.Request('http://127.0.0.1:8000/healthz/', headers={'X-Forwarded-Proto': 'https'}); urllib.request.urlopen(request, timeout=5)" \
      > /dev/null 2>&1; then
      healthy=true
      break
    fi
    sleep 2
  done
  [[ "$healthy" == true ]] || production_fail \
    "Restored web service did not pass its health check; recovery is required."
}

recover_incomplete_restore() {
  production_compose run --rm --no-deps --user rehab:rehab volume-init \
    python /app/scripts/restore_files.py adopt-state || \
    production_fail "No valid incomplete restore state exists."
  restore_state_exists || production_fail "No incomplete restore state exists."
  load_restore_state
  production_compose stop caddy web
  if [[ "$RESTORE_STATUS" == "validated" ]]; then
    drop_database_if_exists "$ROLLBACK_DB"
    cleanup_restore_roots
  else
    rollback_file_roots
    rollback_databases
    production_compose run --rm --no-deps web \
      python manage.py audit_donor_report_submissions --strict --quiescent
    write_restore_state validated
    RESTORE_STATUS="validated"
  fi
  start_restored_services
  remove_restore_state
  printf 'Incomplete restore recovered (%s state).\n' "$RESTORE_STATUS"
}

if [[ "$MODE" == "recover" ]]; then
  recover_incomplete_restore
  exit 0
fi

restore_state_exists && production_fail \
  "An incomplete restore exists. Run scripts/restore_prod.sh --recover --confirm first."

BACKUP_PATH="$(require_absolute_directory "$BACKUP_ARGUMENT")"
ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/verify_backup_prod.sh" "$BACKUP_PATH"
FORMAT="$(grep -E '^FORMAT=' "$BACKUP_PATH/metadata.env" | tail -n 1 | cut -d= -f2-)"
MAX_ARCHIVE_BYTES="$(environment_value BACKUP_MAX_ARCHIVE_BYTES || printf '21474836480')"
MAX_DATABASE_BYTES="$(environment_value BACKUP_MAX_DATABASE_BYTES || printf '1099511627776')"
BACKUP_MOUNT_PATH="$(compose_host_path "$BACKUP_PATH")"

if production_service_running caddy; then
  CADDY_WAS_RUNNING="true"
fi
if production_service_running web; then
  WEB_WAS_RUNNING="true"
fi
RESTORE_TOKEN="$(date -u +%Y%m%d%H%M%S)_$$"
STAGED_DB="rm_restore_stage_${RESTORE_TOKEN}"
ROLLBACK_DB="rm_restore_rollback_${RESTORE_TOKEN}"
write_restore_state preparing
production_compose stop caddy web

restore_complete=false
restore_cleanup() {
  if [[ "$restore_complete" != true ]]; then
    production_compose stop caddy web > /dev/null 2>&1 || true
    printf 'Restore stopped safely. Web remains blocked by the maintenance marker.\n' >&2
    printf 'After inspection, run: scripts/restore_prod.sh --recover --confirm\n' >&2
  fi
}
trap restore_cleanup EXIT

MEDIA_CAPACITY="$(
  production_compose run --rm --no-deps \
    -v "$BACKUP_MOUNT_PATH:/backup:ro" archive-maintenance \
    python manage.py validate_backup_archive /backup/media.tar.gz --root media \
      --max-uncompressed-bytes "$MAX_ARCHIVE_BYTES" --print-capacity \
    | tail -n 1 | tr -d '\r'
)"
IFS=: read -r MEDIA_BYTES MEDIA_INODES <<< "$MEDIA_CAPACITY"
PRIVATE_BYTES=0
PRIVATE_INODES=0
if [[ "$FORMAT" == "rm-backup-v2" ]]; then
  PRIVATE_CAPACITY="$(
    production_compose run --rm --no-deps \
      -v "$BACKUP_MOUNT_PATH:/backup:ro" archive-maintenance \
      python manage.py validate_backup_archive \
        /backup/private-artifacts.tar.gz --root private-artifacts \
        --max-uncompressed-bytes "$MAX_ARCHIVE_BYTES" --print-capacity \
      | tail -n 1 | tr -d '\r'
  )"
  IFS=: read -r PRIVATE_BYTES PRIVATE_INODES <<< "$PRIVATE_CAPACITY"
fi
[[ "$MEDIA_BYTES" =~ ^[0-9]+$ && "$PRIVATE_BYTES" =~ ^[0-9]+$ \
   && "$MEDIA_INODES" =~ ^[1-9][0-9]*$ \
   && "$PRIVATE_INODES" =~ ^[0-9]+$ ]] || \
  production_fail "Archive capacity calculation returned an invalid value."
if [[ "$FORMAT" == "rm-backup-v2" ]]; then
  DATABASE_SIZE_BYTES="$(grep -E '^DATABASE_SIZE_BYTES=' "$BACKUP_PATH/metadata.env" | tail -n 1 | cut -d= -f2-)"
  [[ "$DATABASE_SIZE_BYTES" =~ ^[1-9][0-9]*$ ]] || \
    production_fail "Backup metadata has an invalid database size."
else
  LIVE_DATABASE_SIZE_BYTES="$(
    production_compose exec -T db psql \
      --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
      --tuples-only --no-align \
      --command='SELECT pg_database_size(current_database());' \
      | tr -d '[:space:]'
  )"
  DUMP_SIZE_BYTES="$(wc -c < "$BACKUP_PATH/db.dump" | tr -d '[:space:]')"
  [[ "$LIVE_DATABASE_SIZE_BYTES" =~ ^[1-9][0-9]*$ \
     && "$DUMP_SIZE_BYTES" =~ ^[1-9][0-9]*$ ]] || \
    production_fail "Could not estimate legacy database restore size."
  DATABASE_SIZE_BYTES=$((DUMP_SIZE_BYTES * 10))
  if (( LIVE_DATABASE_SIZE_BYTES > DATABASE_SIZE_BYTES )); then
    DATABASE_SIZE_BYTES="$LIVE_DATABASE_SIZE_BYTES"
  fi
fi
(( DATABASE_SIZE_BYTES <= MAX_DATABASE_BYTES )) || production_fail \
  "Restore database size exceeds BACKUP_MAX_DATABASE_BYTES."
DATABASE_MIN_FREE_INODES="$(environment_value RESTORE_DATABASE_MIN_FREE_INODES || printf '10000')"
[[ "$DATABASE_MIN_FREE_INODES" =~ ^[1-9][0-9]*$ ]] || \
  production_fail "RESTORE_DATABASE_MIN_FREE_INODES must be a positive integer."

production_compose run --rm --no-deps --user rehab:rehab \
  -e RESTORE_MEDIA_BYTES="$MEDIA_BYTES" \
  -e RESTORE_PRIVATE_BYTES="$PRIVATE_BYTES" \
  -e RESTORE_MEDIA_INODES="$MEDIA_INODES" \
  -e RESTORE_PRIVATE_INODES="$PRIVATE_INODES" \
  volume-init python -c '
import os

requirements = {}
inode_requirements = {}
filesystems = {}
for path, byte_variable, inode_variable in (
    ("/app/media", "RESTORE_MEDIA_BYTES", "RESTORE_MEDIA_INODES"),
    (
        "/app/private-artifacts",
        "RESTORE_PRIVATE_BYTES",
        "RESTORE_PRIVATE_INODES",
    ),
):
    device = os.stat(path).st_dev
    requirements[device] = requirements.get(device, 0) + int(os.environ[byte_variable])
    inode_requirements[device] = inode_requirements.get(device, 0) + int(
        os.environ[inode_variable]
    )
    filesystems.setdefault(device, (path, os.statvfs(path)))
for device, required in requirements.items():
    path, stats = filesystems[device]
    available = stats.f_bavail * stats.f_frsize
    reserve = max(64 * 1024 * 1024, required // 20)
    if available < required + reserve:
        raise SystemExit(
            f"Insufficient restore space on {path}: "
            f"required={required + reserve}, available={available}."
        )
    required_inodes = inode_requirements[device]
    inode_reserve = max(1000, required_inodes // 20)
    if stats.f_favail < required_inodes + inode_reserve:
        raise SystemExit(
            f"Insufficient restore inodes on {path}: "
            f"required={required_inodes + inode_reserve}, "
            f"available={stats.f_favail}."
        )
print("Restore capacity check passed.")
'

production_compose exec -T \
  -e RESTORE_DATABASE_SIZE_BYTES="$DATABASE_SIZE_BYTES" \
  -e RESTORE_DATABASE_MIN_FREE_INODES="$DATABASE_MIN_FREE_INODES" \
  db sh -ec '
    set -eu
    available_kib=$(df -Pk /var/lib/postgresql/data | awk "NR == 2 {print \$4}")
    available_inodes=$(df -Pi /var/lib/postgresql/data | awk "NR == 2 {print \$4}")
    case "$available_kib:$available_inodes" in
      *[!0-9:]*|:*) echo "Invalid PostgreSQL capacity result." >&2; exit 1 ;;
    esac
    available_bytes=$((available_kib * 1024))
    database_bytes=$RESTORE_DATABASE_SIZE_BYTES
    required_bytes=$((database_bytes * 2 + 536870912))
    if [ "$available_bytes" -lt "$required_bytes" ]; then
      echo "Insufficient PostgreSQL restore space: required=$required_bytes available=$available_bytes." >&2
      exit 1
    fi
    if [ "$available_inodes" -lt "$RESTORE_DATABASE_MIN_FREE_INODES" ]; then
      echo "Insufficient PostgreSQL restore inodes: required=$RESTORE_DATABASE_MIN_FREE_INODES available=$available_inodes." >&2
      exit 1
    fi
    echo "PostgreSQL restore capacity check passed."
  '

production_compose exec -T db createdb \
  --maintenance-db=postgres --username "$POSTGRES_USER" "$STAGED_DB"
production_compose exec -T db pg_restore \
  --exit-on-error --no-owner --no-privileges \
  --username "$POSTGRES_USER" --dbname "$STAGED_DB" \
  < "$BACKUP_PATH/db.dump"

production_compose run --rm --no-deps \
  -e RESTORE_TEST_FAIL_AFTER_ARCHIVE_EXTRACT="${RESTORE_TEST_FAIL_AFTER_ARCHIVE_EXTRACT:-0}" \
  -v "$BACKUP_MOUNT_PATH:/backup:ro" volume-init sh -ec '
  set -eu
  rm -rf -- /app/media/.restore-new /app/private-artifacts/.restore-new
  install -d -m 0755 /app/media/.restore-new
  tar --no-same-owner --no-same-permissions \
    -xzf /backup/media.tar.gz -C /app/media/.restore-new
  test -d /app/media/.restore-new/media
  if [ "$RESTORE_TEST_FAIL_AFTER_ARCHIVE_EXTRACT" = 1 ]; then
    echo "Injected failure after archive extraction." >&2
    exit 91
  fi
  find /app/media/.restore-new -type d -exec chmod 0755 {} +
  find /app/media/.restore-new -type f -exec chmod 0644 {} +
  chown -R rehab:rehab /app/media/.restore-new
'
if [[ "$FORMAT" == "rm-backup-v2" ]]; then
  production_compose run --rm --no-deps \
    -v "$BACKUP_MOUNT_PATH:/backup:ro" volume-init sh -ec '
    set -eu
    umask 077
    install -d -m 0700 /app/private-artifacts/.restore-new
    tar --no-same-owner --no-same-permissions \
      -xzf /backup/private-artifacts.tar.gz \
      -C /app/private-artifacts/.restore-new
    test -d /app/private-artifacts/.restore-new/private-artifacts
    find /app/private-artifacts/.restore-new -type d -exec chmod 0700 {} +
    find /app/private-artifacts/.restore-new -type f -exec chmod 0600 {} +
    chown -R rehab:rehab /app/private-artifacts/.restore-new
    if find /app/private-artifacts/.restore-new -type d ! -perm 0700 \
         -print -quit | grep -q .; then
      echo "Private restore directory permissions are unsafe." >&2
      exit 1
    fi
    if find /app/private-artifacts/.restore-new -type f ! -perm 0600 \
         -print -quit | grep -q .; then
      echo "Private restore file permissions are unsafe." >&2
      exit 1
    fi
  '
else
  production_compose run --rm --no-deps volume-init \
    install -d -o rehab -g rehab -m 0700 \
      /app/private-artifacts/.restore-new/private-artifacts
fi

production_compose run --rm --no-deps \
  --user rehab:rehab \
  -e RESTORE_DATABASE_NAME_OVERRIDE="$STAGED_DB" \
  migration python manage.py migrate --noinput
production_compose run --rm --no-deps \
  --user rehab:rehab \
  -e RESTORE_DATABASE_NAME_OVERRIDE="$STAGED_DB" \
  migration python manage.py configure_runtime_database_role
if [[ "$FORMAT" == "rm-backup-v2" ]]; then
  production_compose run --rm --no-deps \
    --user rehab:rehab \
    -e RESTORE_DATABASE_NAME_OVERRIDE="$STAGED_DB" \
    -e PRIVATE_ARTIFACT_ROOT=/app/private-artifacts/.restore-new/private-artifacts \
    migration python manage.py audit_donor_report_submissions --strict --quiescent
else
  production_compose run --rm --no-deps \
    --user rehab:rehab \
    -e RESTORE_DATABASE_NAME_OVERRIDE="$STAGED_DB" \
    migration python manage.py assert_legacy_backup_has_no_donor_submissions
fi

production_compose exec -T db psql \
  --username "$POSTGRES_USER" \
  --dbname postgres \
  --set=ON_ERROR_STOP=1 \
  --set=live_db="$POSTGRES_DB" \
  --set=staged_db="$STAGED_DB" \
  --set=rollback_db="$ROLLBACK_DB" <<'SQL'
SELECT pg_terminate_backend(pid)
FROM pg_stat_activity
WHERE datname IN (:'live_db', :'staged_db')
  AND pid <> pg_backend_pid();
BEGIN;
ALTER DATABASE :"live_db" RENAME TO :"rollback_db";
ALTER DATABASE :"staged_db" RENAME TO :"live_db";
COMMIT;
SQL

if [[ "${RESTORE_TEST_FAIL_AFTER_DATABASE_SWITCH:-0}" == "1" ]]; then
  production_fail "Injected failure after database switch."
fi

production_compose run --rm --no-deps --user rehab:rehab volume-init \
  python /app/scripts/restore_files.py switch-root \
    --root /app/media --new-relative .restore-new/media
production_compose run --rm --no-deps --user rehab:rehab volume-init \
  python /app/scripts/restore_files.py switch-root \
    --root /app/private-artifacts \
    --new-relative .restore-new/private-artifacts

if [[ "${RESTORE_TEST_FAIL_AFTER_FILE_SWITCH:-0}" == "1" ]]; then
  production_fail "Injected failure after file switch."
fi

production_compose run --rm --no-deps web \
  python manage.py audit_donor_report_submissions --strict --quiescent
write_restore_state candidate
start_and_validate_candidate
if [[ "${RESTORE_TEST_FAIL_AFTER_CANDIDATE_HEALTH:-0}" == "1" ]]; then
  production_fail "Injected failure after candidate health check."
fi
write_restore_state validated
start_restored_services

drop_database_if_exists "$ROLLBACK_DB"
cleanup_restore_roots
remove_restore_state

restore_complete=true
trap - EXIT
printf 'Restore completed. Run scripts/production_preflight.sh before reopening normal work.\n'
