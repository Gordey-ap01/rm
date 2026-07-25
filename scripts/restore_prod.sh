#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=_production_common.sh
source "$SCRIPT_DIR/_production_common.sh"

if [[ $# -ne 2 || "$1" != "--confirm" ]]; then
  printf 'Usage: %s --confirm /absolute/path/to/backup\n' "$(basename -- "$0")" >&2
  printf 'This command replaces the production database and media.\n' >&2
  exit 2
fi

load_production_environment "${ENV_FILE:-$PRODUCTION_ROOT/.env.production}"
for variable in POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD; do
  require_environment_value "$variable"
done
BACKUP_PATH="$(require_absolute_directory "$2")"
ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/verify_backup_prod.sh" "$BACKUP_PATH"

production_compose stop web
restore_complete=false
restore_cleanup() {
  if [[ "$restore_complete" != true ]]; then
    printf 'Restore failed. Web remains stopped; inspect the error before starting it.\n' >&2
  fi
}
trap restore_cleanup EXIT

production_compose exec -T db dropdb --if-exists --force --maintenance-db=postgres --username "$POSTGRES_USER" "$POSTGRES_DB"
production_compose exec -T db createdb --maintenance-db=postgres --username "$POSTGRES_USER" "$POSTGRES_DB"
production_compose exec -T db pg_restore --exit-on-error --no-owner --no-privileges --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" < "$BACKUP_PATH/db.dump"

BACKUP_MOUNT_PATH="$(compose_host_path "$BACKUP_PATH")"
production_compose run --rm --no-deps -v "$BACKUP_MOUNT_PATH:/backup:ro" web sh -ec 'find /app/media -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; tar -xzf /backup/media.tar.gz -C /app'
production_compose up -d web

restore_complete=true
trap - EXIT
printf 'Restore completed. Run scripts/production_preflight.sh before reopening normal work.\n'
