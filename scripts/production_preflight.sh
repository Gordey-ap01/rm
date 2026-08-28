#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=_production_common.sh
source "$SCRIPT_DIR/_production_common.sh"

CONFIG_ONLY=false
INTERNAL_ONLY=false
ENV_ARGUMENT="${ENV_FILE:-$PRODUCTION_ROOT/.env.production}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config-only)
      CONFIG_ONLY=true
      ;;
    --internal-only)
      INTERNAL_ONLY=true
      ;;
    --env-file)
      shift
      [[ $# -gt 0 ]] || production_fail "--env-file requires a path."
      ENV_ARGUMENT="$1"
      ;;
    *)
      production_fail "Unknown preflight argument: $1"
      ;;
  esac
  shift
done
[[ "$CONFIG_ONLY" != true || "$INTERNAL_ONLY" != true ]] || production_fail \
  "Use only one preflight mode."

load_production_environment "$ENV_ARGUMENT"
for variable in APP_DOMAIN DJANGO_SECRET_KEY POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD POSTGRES_RUNTIME_USER POSTGRES_RUNTIME_PASSWORD EMAIL_HOST EMAIL_HOST_USER EMAIL_HOST_PASSWORD DEFAULT_FROM_EMAIL; do
  ensure_not_placeholder "$variable"
done
[[ "$POSTGRES_RUNTIME_USER" != "$POSTGRES_USER" ]] || production_fail \
  "POSTGRES_RUNTIME_USER must differ from the migration/restore POSTGRES_USER."

[[ "$(environment_value DJANGO_DEBUG)" == "0" ]] || production_fail "DJANGO_DEBUG must be 0 in production."
[[ "$(environment_value DJANGO_SECURE_SSL_REDIRECT)" == "1" ]] || production_fail "DJANGO_SECURE_SSL_REDIRECT must be 1."
HSTS_SECONDS="$(environment_value DJANGO_SECURE_HSTS_SECONDS || printf '0')"
[[ "$HSTS_SECONDS" =~ ^[1-9][0-9]*$ ]] || production_fail \
  "DJANGO_SECURE_HSTS_SECONDS must be a positive integer in production."
[[ "$(environment_value DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS)" == "1" ]] || \
  production_fail "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS must be 1."
[[ "$(environment_value DJANGO_SECURE_HSTS_PRELOAD)" == "1" ]] || \
  production_fail "DJANGO_SECURE_HSTS_PRELOAD must be 1."
[[ "$(environment_value EMAIL_BACKEND)" == "django.core.mail.backends.smtp.EmailBackend" ]] || production_fail "EMAIL_BACKEND must use SMTP in production."
SUBMISSIONS_ENABLED="$(environment_value DONOR_REPORT_SUBMISSIONS_ENABLED || printf '0')"
SUBMISSIONS_APPROVED="$(environment_value DONOR_REPORT_SUBMISSIONS_PRODUCTION_APPROVED || printf '0')"
[[ "$SUBMISSIONS_ENABLED" =~ ^[01]$ ]] || production_fail "DONOR_REPORT_SUBMISSIONS_ENABLED must be 0 or 1."
[[ "$SUBMISSIONS_APPROVED" =~ ^[01]$ ]] || production_fail "DONOR_REPORT_SUBMISSIONS_PRODUCTION_APPROVED must be 0 or 1."
if [[ "$SUBMISSIONS_ENABLED" == "1" && "$SUBMISSIONS_APPROVED" != "1" ]]; then
  production_fail "Donor report uploads require explicit production approval."
fi
production_compose config --quiet

if [[ "$CONFIG_ONLY" == true ]]; then
  printf 'Production configuration preflight passed.\n'
  exit 0
fi

production_compose exec -T web \
  python manage.py assert_runtime_database_role_is_restricted
production_compose exec -T web python manage.py check --deploy --fail-level WARNING
production_compose exec -T web python manage.py audit_donor_report_submissions --strict
production_compose exec -T web python -c "import urllib.request; request = urllib.request.Request('http://127.0.0.1:8000/healthz/', headers={'X-Forwarded-Proto': 'https'}); urllib.request.urlopen(request, timeout=5)"
production_compose exec -T web python -c "from django.core.mail import get_connection; connection = get_connection(); connection.open(); connection.close()"
if [[ "$INTERNAL_ONLY" == true ]]; then
  printf 'Production internal preflight passed.\n'
  exit 0
fi
curl --fail --silent --show-error --max-time 10 "https://${APP_DOMAIN}/healthz/" > /dev/null

printf 'Production preflight passed.\n'
