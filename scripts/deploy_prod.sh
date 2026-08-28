#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=_production_common.sh
source "$SCRIPT_DIR/_production_common.sh"

[[ $# -eq 1 && "$1" == "--confirm" ]] || production_fail \
  "Usage: scripts/deploy_prod.sh --confirm"
load_production_environment "${ENV_FILE:-$PRODUCTION_ROOT/.env.production}"
for variable in POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD POSTGRES_RUNTIME_USER POSTGRES_RUNTIME_PASSWORD; do
  require_environment_value "$variable"
done

ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/production_preflight.sh" --config-only
acquire_production_maintenance_lock
assert_production_migration_not_running
assert_no_incomplete_maintenance

web_existed=false
web_was_running=false
caddy_existed=false
caddy_was_running=false
production_service_exists web && web_existed=true
production_service_running web && web_was_running=true
production_service_exists caddy && caddy_existed=true
production_service_running caddy && caddy_was_running=true
web_should_run="$web_was_running"
caddy_should_run="$caddy_was_running"
[[ "$web_existed" == true ]] || web_should_run=true
[[ "$caddy_existed" == true ]] || caddy_should_run=true

deployment_changed=false
deployment_complete=false
close_failed_deployment() {
  if [[ "$deployment_changed" != true || "$deployment_complete" == true ]]; then
    return
  fi
  production_compose stop caddy web > /dev/null 2>&1 || true
  printf 'Deployment failed after changing the release; web and Caddy remain stopped.\n' >&2
  printf 'Inspect the failure and deploy a verified compatible release before reopening traffic.\n' >&2
}
trap close_failed_deployment EXIT

DEPLOY_TEST_FAIL_AFTER_WEB_START="${DEPLOY_TEST_FAIL_AFTER_WEB_START:-0}"
[[ "$DEPLOY_TEST_FAIL_AFTER_WEB_START" == "0" \
   || "$DEPLOY_TEST_FAIL_AFTER_WEB_START" == "1" ]] || production_fail \
  "DEPLOY_TEST_FAIL_AFTER_WEB_START must be 0 or 1."
if [[ "$DEPLOY_TEST_FAIL_AFTER_WEB_START" == "1" && "${CI:-}" != "true" ]]; then
  production_fail "Deployment fault injection is restricted to CI."
fi

deployment_changed=true
production_compose stop caddy web
production_compose build
production_compose up -d --wait db redis
production_compose run --rm --no-deps volume-init
production_compose run --rm --no-deps --user rehab:rehab migration sh -ec \
  'python manage.py migrate --noinput && python manage.py configure_runtime_database_role'
production_compose up -d --no-deps --wait web
if [[ "$DEPLOY_TEST_FAIL_AFTER_WEB_START" == "1" ]]; then
  production_fail "Injected failure after deployment web start."
fi
ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/production_preflight.sh" --internal-only

if [[ "$caddy_should_run" == true && "$web_should_run" == true ]]; then
  production_compose up -d --no-deps caddy
  ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/production_preflight.sh"
else
  production_compose stop caddy > /dev/null
fi
if [[ "$web_should_run" != true ]]; then
  production_compose stop web > /dev/null
fi

deployment_complete=true
trap - EXIT
printf 'Production deployment completed and passed its applicable preflight.\n'
