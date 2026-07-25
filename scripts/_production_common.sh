#!/usr/bin/env bash

set -euo pipefail

PRODUCTION_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
ENV_FILE=""
COMPOSE_FILE="${COMPOSE_FILE:-$PRODUCTION_ROOT/compose.prod.yaml}"

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

production_compose() {
  local compose_env_file compose_file
  compose_env_file="$(compose_host_path "$ENV_FILE")"
  compose_file="$(compose_host_path "$COMPOSE_FILE")"
  local command=(docker compose --env-file "$compose_env_file" -f "$compose_file")
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

require_absolute_directory() {
  local candidate="$1"
  [[ "$candidate" == /* && "$candidate" != "/" ]] || production_fail "Use an absolute non-root directory path."
  [[ -d "$candidate" ]] || production_fail "Directory does not exist: $candidate"
  cd -- "$candidate"
  pwd -P
}
