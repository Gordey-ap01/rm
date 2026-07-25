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
BACKUP_PATH="$(require_absolute_directory "$1")"
for required_file in db.dump media.tar.gz SHA256SUMS metadata.env; do
  [[ -f "$BACKUP_PATH/$required_file" ]] || production_fail "Backup is missing $required_file."
done
grep -qx 'FORMAT=rm-backup-v1' "$BACKUP_PATH/metadata.env" || production_fail "Unsupported backup format."

(
  cd -- "$BACKUP_PATH"
  cmp -s <(sha256sum db.dump media.tar.gz) SHA256SUMS || production_fail "Backup checksum verification failed."
)

production_compose exec -T db pg_restore --list < "$BACKUP_PATH/db.dump" > /dev/null
tar -tzf "$BACKUP_PATH/media.tar.gz" > /dev/null

printf 'Backup verified: %s\n' "$BACKUP_PATH"
