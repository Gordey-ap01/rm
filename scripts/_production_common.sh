#!/usr/bin/env bash

set -euo pipefail

PRODUCTION_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
ENV_FILE=""
COMPOSE_FILE="${COMPOSE_FILE:-$PRODUCTION_ROOT/compose.prod.yaml}"
RESTORE_COMPOSE_FILE="$PRODUCTION_ROOT/compose.restore.yaml"

production_fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

load_production_environment() {
  local candidate="${1:-$PRODUCTION_ROOT/.env.production}"
  if [[ "$candidate" != /* ]]; then
    candidate="$PRODUCTION_ROOT/$candidate"
  fi
  [[ -f "$candidate" ]] || production_fail "Production env file is missing."
  [[ -f "$COMPOSE_FILE" ]] || production_fail "Production compose file is missing."
  ENV_FILE="$(cd -- "$(dirname -- "$candidate")" && pwd -P)/$(basename -- "$candidate")"
}

environment_value() {
  local name="$1"
  local line
  line="$(grep -E "^${name}=" "$ENV_FILE" | tail -n 1 || true)"
  [[ -n "$line" ]] || return 1
  printf '%s' "${line#*=}"
}

require_environment_value() {
  local name="$1"
  local value
  value="$(environment_value "$name" || true)"
  [[ -n "$value" ]] || production_fail "Required production variable $name is empty or missing."
  printf -v "$name" '%s' "$value"
}

ensure_not_placeholder() {
  local name="$1"
  local value
  value="$(environment_value "$name" || true)"
  [[ -n "$value" ]] || production_fail "Required production variable $name is empty or missing."
  case "$value" in
    *CHANGE_ME*|*change-me*|dev-insecure-change-me|rehab_dev_password|*.example.*|example.*|*.invalid)
      production_fail "Production variable $name still has a development placeholder."
      ;;
  esac
  printf -v "$name" '%s' "$value"
}

compose_host_path() {
  local candidate="$1"
  case "$(uname -s)" in
    MINGW*|MSYS*)
      cygpath -w "$candidate"
      ;;
    *)
      printf '%s' "$candidate"
      ;;
  esac
}

_production_compose() {
  local include_restore_override="$1"
  shift
  local compose_env_file compose_file
  compose_env_file="$(compose_host_path "$ENV_FILE")"
  compose_file="$(compose_host_path "$COMPOSE_FILE")"
  local command=(docker compose --env-file "$compose_env_file" -f "$compose_file")
  if [[ "$include_restore_override" == "true" ]]; then
    [[ -f "$RESTORE_COMPOSE_FILE" ]] || production_fail \
      "Production restore Compose override is missing."
    command+=(-f "$(compose_host_path "$RESTORE_COMPOSE_FILE")")
  fi
  if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
    command+=(--project-name "$COMPOSE_PROJECT_NAME")
  fi
  case "$(uname -s)" in
    MINGW*|MSYS*)
      MSYS_NO_PATHCONV=1 "${command[@]}" "$@"
      ;;
    *)
      "${command[@]}" "$@"
      ;;
  esac
}

production_compose() {
  _production_compose false "$@"
}

production_restore_compose() {
  _production_compose true "$@"
}

acquire_production_maintenance_lock() {
  local skip_lock="${PRODUCTION_TEST_SKIP_MAINTENANCE_LOCK:-0}"
  [[ "$skip_lock" == "0" || "$skip_lock" == "1" ]] || production_fail \
    "PRODUCTION_TEST_SKIP_MAINTENANCE_LOCK must be 0 or 1."
  if [[ "$skip_lock" == "1" ]]; then
    [[ "${CI:-}" == "true" ]] || production_fail \
      "Production maintenance lock bypass is restricted to CI."
    return
  fi
  command -v flock > /dev/null 2>&1 || production_fail \
    "The host flock utility is required for production maintenance."
  mkdir -p -- "$PRODUCTION_ROOT/.runtime"
  exec 9> "$PRODUCTION_ROOT/.runtime/production-maintenance.lock"
  flock -n 9 || production_fail \
    "Another production backup or restore operation is already running."
}

assert_production_migration_not_running() {
  if production_compose ps --status running --services | grep -qx migration; then
    production_fail "Production migration is running; maintenance cannot start."
  fi
}

production_service_running() {
  local service="$1"
  production_compose ps --status running --services | grep -qx "$service"
}

production_service_exists() {
  local service="$1"
  production_compose ps --all --services | grep -qx "$service"
}

wait_for_production_web_health() {
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
  return 1
}

assert_no_incomplete_maintenance() {
  if ! production_compose run --rm --no-deps web sh -ec '
    for path in \
      /app/media/.restore-new \
      /app/media/.restore-old \
      /app/media/.restore-old-preparing \
      /app/media/.restore-discard \
      /app/private-artifacts/.restore-in-progress \
      /app/private-artifacts/.restore-in-progress.tmp \
      /app/private-artifacts/.restore-new \
      /app/private-artifacts/.restore-old \
      /app/private-artifacts/.restore-old-preparing \
      /app/private-artifacts/.restore-discard \
      /app/private-artifacts/.backup-in-progress \
      /app/private-artifacts/.backup-in-progress.tmp; do
      test ! -e "$path" || exit 1
    done
  '; then
    production_fail \
      "Incomplete backup/restore state exists; recover it before maintenance."
  fi
}

assert_no_incomplete_backup() {
  if ! production_compose run --rm --no-deps web sh -ec '
    test ! -e /app/private-artifacts/.backup-in-progress
    test ! -e /app/private-artifacts/.backup-in-progress.tmp
  '; then
    production_fail "Incomplete backup exists; run backup recovery first."
  fi
}

assert_no_incomplete_restore() {
  if ! production_compose run --rm --no-deps web sh -ec '
    for path in \
      /app/media/.restore-new \
      /app/media/.restore-old \
      /app/media/.restore-old-preparing \
      /app/media/.restore-discard \
      /app/private-artifacts/.restore-in-progress \
      /app/private-artifacts/.restore-in-progress.tmp \
      /app/private-artifacts/.restore-new \
      /app/private-artifacts/.restore-old \
      /app/private-artifacts/.restore-old-preparing \
      /app/private-artifacts/.restore-discard; do
      test ! -e "$path" || exit 1
    done
  '; then
    production_fail "Incomplete restore exists; run restore recovery first."
  fi
}

require_absolute_directory() {
  local candidate="$1"
  [[ "$candidate" == /* && "$candidate" != "/" ]] || production_fail "Use an absolute non-root directory path."
  [[ -d "$candidate" ]] || production_fail "Directory does not exist: $candidate"
  cd -- "$candidate"
  pwd -P
}
