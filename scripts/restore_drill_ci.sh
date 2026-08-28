#!/usr/bin/env bash

set -euo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=_production_common.sh
source "$SCRIPT_DIR/_production_common.sh"

[[ "${CI:-}" == "true" ]] || production_fail "Restore drill is restricted to CI."
[[ ! -e "$PRODUCTION_ROOT/.env.production" ]] || production_fail \
  "Refusing to replace an existing .env.production."

DRILL_ROOT="$(mktemp -d "$PRODUCTION_ROOT/.restore-drill.XXXXXX")"
ENV_ARGUMENT="$DRILL_ROOT/restore-drill.env"
BACKUP_DIR="$DRILL_ROOT/backups"
COMPOSE_PROJECT_NAME="rmrestore${RANDOM}${RANDOM}"
export COMPOSE_PROJECT_NAME
services_started=false

cleanup() {
  if [[ "$services_started" == true ]]; then
    production_compose down --volumes --remove-orphans || true
  fi
  rm -f -- "$PRODUCTION_ROOT/.env.production"
  rm -rf -- "$DRILL_ROOT"
}
trap cleanup EXIT

mkdir -p -- "$BACKUP_DIR"
cat > "$ENV_ARGUMENT" <<EOF
APP_DOMAIN=restore-drill.test
DJANGO_SECRET_KEY=restore-drill-secret-not-production
DJANGO_DEBUG=0
DJANGO_ALLOWED_HOSTS=restore-drill.test,localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://restore-drill.test
DJANGO_SECURE_SSL_REDIRECT=1
DJANGO_SECURE_HSTS_SECONDS=31536000
DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=1
DJANGO_SECURE_HSTS_PRELOAD=1
POSTGRES_DB=rehab_restore_drill
POSTGRES_USER=rehab_restore_drill
POSTGRES_PASSWORD=restore-drill-postgres-password
POSTGRES_RUNTIME_USER=rehab_restore_runtime
POSTGRES_RUNTIME_PASSWORD=restore-drill-runtime-password
BACKUP_DIR=$BACKUP_DIR
BACKUP_RETENTION_DAYS=1
DONOR_REPORT_SUBMISSIONS_ENABLED=1
DONOR_REPORT_SUBMISSIONS_PRODUCTION_APPROVED=1
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.restore-drill.test
EMAIL_PORT=587
EMAIL_USE_TLS=1
EMAIL_HOST_USER=restore-drill@test
EMAIL_HOST_PASSWORD=restore-drill-smtp-password
DEFAULT_FROM_EMAIL=restore-drill@test
EOF
cp -- "$ENV_ARGUMENT" "$PRODUCTION_ROOT/.env.production"
load_production_environment "$ENV_ARGUMENT"

if command -v flock > /dev/null 2>&1; then
  mkdir -p -- "$PRODUCTION_ROOT/.runtime"
  exec 8> "$PRODUCTION_ROOT/.runtime/production-maintenance.lock"
  flock -n 8
  if ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/backup_prod.sh"; then
    production_fail "Backup bypassed the shared production maintenance lock."
  fi
  flock -u 8
  exec 8>&-
else
  export PRODUCTION_TEST_SKIP_MAINTENANCE_LOCK=1
fi

wait_for_web() {
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
  production_fail "Web did not become healthy during restore drill."
}

services_started=true
production_compose up -d --build db redis web caddy
wait_for_web
production_service_running caddy || production_fail \
  "Caddy did not start during restore drill."
production_compose exec -T web \
  python manage.py assert_runtime_database_role_is_restricted
if production_compose exec -T web python manage.py shell -c \
  "from django.db import connection; connection.cursor().execute('TRUNCATE public.operations_donorreportsubmissionaccess')"; then
  production_fail "Runtime role could truncate immutable donor history."
fi
if production_compose exec -T -e PGOPTIONS='-c search_path=public' web \
  python manage.py assert_runtime_database_role_is_restricted; then
  production_fail "Runtime role guard accepted an unsafe search_path."
fi
if production_compose run --rm --no-deps migration \
  python manage.py assert_runtime_database_role_is_restricted; then
  production_fail "Migration owner was accepted as a restricted runtime role."
fi

production_compose exec -T web python manage.py shell -c \
  "from datetime import timedelta; from django.contrib.auth import get_user_model; from django.core.files.uploadedfile import SimpleUploadedFile; from django.utils import timezone; from operations.models import Counterparty, FundingSource; from operations.services.donor_reports import close_donor_report_snapshot, create_donor_report_submission, review_donor_report_snapshot; User=get_user_model(); user=User.objects.create_superuser(username='restore-drill-before', password='not-production'); funding=FundingSource.objects.create(name='Restore drill grant', source_type=FundingSource.SourceType.GRANT); counterparty=Counterparty.objects.create(name='Restore drill donor', counterparty_type=Counterparty.CounterpartyType.FOUNDATION); today=timezone.localdate(); review=review_donor_report_snapshot(funding_source_id=funding.pk, counterparty=counterparty, date_from=today-timedelta(days=1), date_to=today); snapshot=close_donor_report_snapshot(funding_source_id=funding.pk, counterparty=counterparty, date_from=today-timedelta(days=1), date_to=today, actor=user, reason='Restore drill closed snapshot.', expected_review_token=review.review_token); create_donor_report_submission(report_snapshot_id=snapshot.pk, uploaded_file=SimpleUploadedFile('restore-drill.pdf', b'%PDF-1.4\\nrestore-drill-before\\n%%EOF\\n', content_type='application/pdf'), submitted_on=today, actor=user, reason='Restore drill submitted file.')"
production_compose exec -T web sh -ec "printf before > /app/media/restore-drill.txt"
for injection in --inject-after-old-prepare --inject-after-old-publish; do
  production_compose exec -T web sh -ec \
    'mkdir -p /app/media/.restore-new/media; printf candidate > /app/media/.restore-new/media/restore-drill.txt'
  if production_compose exec -T web \
    python /app/scripts/restore_files.py switch-root \
      --root /app/media --new-relative .restore-new/media "$injection"; then
    production_fail "Restore file-state fault injection did not stop."
  fi
  production_compose exec -T web \
    python /app/scripts/restore_files.py rollback-root --root /app/media
  production_compose exec -T web sh -ec \
    'test "$(cat /app/media/restore-drill.txt)" = before'
done

production_compose exec -T web sh -ec \
  'mkdir -p /app/media/.restore-new/media; touch /app/media/.restore-unknown'
if production_compose exec -T web \
  python /app/scripts/restore_files.py switch-root \
    --root /app/media --new-relative .restore-new/media; then
  production_fail "Restore accepted an unknown control entry."
fi
production_compose exec -T web sh -ec '
  test ! -e /app/media/.restore-old
  test ! -e /app/media/.restore-old-preparing
  rm -f /app/media/.restore-unknown
'
production_compose exec -T web \
  python /app/scripts/restore_files.py cleanup-root --root /app/media

if BACKUP_TEST_FAIL_AFTER_STATE_TEMP=1 \
  ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/backup_prod.sh"; then
  production_fail "Backup temp-state fault injection did not stop the operation."
fi
production_compose run --rm --no-deps web sh -ec '
  test -f /app/private-artifacts/.backup-in-progress.tmp
  test ! -e /app/private-artifacts/.backup-in-progress
'
ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/backup_prod.sh" --recover
wait_for_web
production_compose run --rm --no-deps web sh -ec '
  test ! -e /app/private-artifacts/.backup-in-progress.tmp
  test ! -e /app/private-artifacts/.backup-in-progress
'

ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/backup_prod.sh"

BACKUP_PATH="$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '20[0-9][0-9]*T[0-9][0-9]*Z' -print -quit)"
[[ -n "$BACKUP_PATH" ]] || production_fail "Restore drill backup was not created."
ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/verify_backup_prod.sh" "$BACKUP_PATH"

sleep 1
if BACKUP_TEST_CRASH_AFTER_PARTIAL=1 \
  ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/backup_prod.sh"; then
  production_fail "Backup crash injection did not stop the operation."
fi
production_compose run --rm --no-deps web \
  test -f /app/private-artifacts/.backup-in-progress
production_service_running web && production_fail \
  "Web remained running after abrupt backup interruption."
find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '.partial-*' \
  -print -quit | grep -q . || production_fail \
  "Backup crash injection did not leave an unpublished partial directory."
ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/backup_prod.sh" --recover
wait_for_web
production_compose run --rm --no-deps web \
  test ! -e /app/private-artifacts/.backup-in-progress
if find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d -name '.partial-*' \
  -print -quit | grep -q .; then
  production_fail "Backup recovery left an unpublished partial directory."
fi

if DEPLOY_TEST_FAIL_AFTER_WEB_START=1 \
  ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/deploy_prod.sh" --confirm; then
  production_fail "Deployment web-start fault injection did not stop the operation."
fi
production_service_running caddy && production_fail \
  "Failed deployment reopened Caddy."
production_service_running web && production_fail \
  "Failed deployment left unverified web running."
production_compose up -d --no-deps web
wait_for_web
production_compose up -d --no-deps caddy
production_service_running caddy || production_fail \
  "Drill could not restore Caddy after the deployment fault assertion."

if RESTORE_TEST_FAIL_AFTER_STATE_TEMP=1 \
  ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/restore_prod.sh" --confirm "$BACKUP_PATH"; then
  production_fail "Restore temp-state fault injection did not stop the operation."
fi
production_compose run --rm --no-deps web sh -ec '
  test -f /app/private-artifacts/.restore-in-progress.tmp
  test ! -e /app/private-artifacts/.restore-in-progress
'
ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/restore_prod.sh" --recover --confirm
wait_for_web
production_service_running caddy || production_fail \
  "Caddy was not preserved after temp-only restore recovery."

CORRUPT_PATH="$DRILL_ROOT/corrupt-backup"
cp -a -- "$BACKUP_PATH" "$CORRUPT_PATH"
printf x >> "$CORRUPT_PATH/db.dump"
if ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/restore_prod.sh" --confirm "$CORRUPT_PATH"; then
  production_fail "Restore accepted a corrupt archive."
fi
wait_for_web

production_compose exec -T web python manage.py shell -c \
  "from django.contrib.auth import get_user_model; get_user_model().objects.create_user(username='restore-drill-after')"
production_compose exec -T web sh -ec "printf after > /app/media/restore-drill.txt"
production_compose exec -T web python manage.py shell -c \
  "from django.contrib.auth import get_user_model; from django.core.files.uploadedfile import SimpleUploadedFile; from django.utils import timezone; from operations.models import DonorReportSnapshot, DonorReportSubmission; from operations.services.donor_reports import create_donor_report_submission; user=get_user_model().objects.get(username='restore-drill-before'); snapshot=DonorReportSnapshot.objects.get(); previous=DonorReportSubmission.objects.get(); create_donor_report_submission(report_snapshot_id=snapshot.pk, uploaded_file=SimpleUploadedFile('restore-drill-replacement.pdf', b'%PDF-1.4\\nrestore-drill-after\\n%%EOF\\n', content_type='application/pdf'), submitted_on=timezone.localdate(), actor=user, reason='Restore drill replacement file.', expected_submission_id=previous.pk)"

if RESTORE_TEST_FAIL_AFTER_FILE_SWITCH=1 \
  ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/restore_prod.sh" --confirm "$BACKUP_PATH"; then
  production_fail "Restore fault injection did not stop the operation."
fi
production_compose run --rm --no-deps web \
  test -f /app/private-artifacts/.restore-in-progress
if ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/backup_prod.sh"; then
  production_fail "Backup accepted an incomplete restore state."
fi
if production_compose run --rm --no-deps migration; then
  production_fail "Migration accepted an incomplete restore state."
fi
if ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/deploy_prod.sh" --confirm; then
  production_fail "Deployment accepted an incomplete restore state."
fi
if RESTORE_TEST_FAIL_DURING_FILE_ROLLBACK=1 \
  ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/restore_prod.sh" --recover --confirm; then
  production_fail "Recovery fault injection did not stop the operation."
fi
production_compose run --rm --no-deps web \
  test -f /app/private-artifacts/.restore-in-progress
ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/restore_prod.sh" --recover --confirm
wait_for_web
production_compose exec -T web python manage.py shell -c \
  "from django.contrib.auth import get_user_model; User=get_user_model(); assert User.objects.filter(username='restore-drill-after').exists()"
production_compose exec -T web sh -ec 'test "$(cat /app/media/restore-drill.txt)" = after'
production_compose exec -T web python manage.py shell -c \
  "from operations.models import DonorReportSubmission; from operations.services.private_artifacts import read_verified_artifact; submissions=list(DonorReportSubmission.objects.order_by('submission_number')); assert len(submissions) == 2; latest=submissions[-1]; payload=read_verified_artifact(storage_key=latest.storage_key, expected_size=latest.file_size, expected_sha256=latest.file_sha256, expected_content_type=latest.content_type); assert b'restore-drill-after' in payload"
production_compose exec -T web \
  python manage.py audit_donor_report_submissions --strict --quiescent

if RESTORE_TEST_FAIL_AFTER_CANDIDATE_HEALTH=1 \
  ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/restore_prod.sh" --confirm "$BACKUP_PATH"; then
  production_fail "Candidate health fault injection did not stop the operation."
fi
production_service_running caddy && production_fail \
  "Caddy exposed an unaccepted restore candidate."
production_compose run --rm --no-deps web sh -ec \
  "grep -q '^STATUS=candidate$' /app/private-artifacts/.restore-in-progress"
ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/restore_prod.sh" --recover --confirm
wait_for_web
production_service_running caddy || production_fail \
  "Caddy was not restored after candidate rollback."
production_compose exec -T web sh -ec \
  'test "$RESTORE_CANDIDATE_START" = 0'
production_compose exec -T web python manage.py shell -c \
  "from django.contrib.auth import get_user_model; User=get_user_model(); assert User.objects.filter(username='restore-drill-after').exists()"

ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/restore_prod.sh" --confirm "$BACKUP_PATH"
wait_for_web
production_compose exec -T web python manage.py shell -c \
  "from django.contrib.auth import get_user_model; from operations.models import DonorReportSubmission; from operations.services.private_artifacts import read_verified_artifact; User = get_user_model(); assert User.objects.filter(username='restore-drill-before').exists(); assert not User.objects.filter(username='restore-drill-after').exists(); submission=DonorReportSubmission.objects.get(); payload=read_verified_artifact(storage_key=submission.storage_key, expected_size=submission.file_size, expected_sha256=submission.file_sha256, expected_content_type=submission.content_type); assert b'restore-drill-before' in payload"
production_compose exec -T web sh -ec 'test "$(cat /app/media/restore-drill.txt)" = before'

V1_PATH="$DRILL_ROOT/v1-with-submission"
cp -a -- "$BACKUP_PATH" "$V1_PATH"
rm -f -- "$V1_PATH/private-artifacts.tar.gz"
sed -i 's/^FORMAT=rm-backup-v2$/FORMAT=rm-backup-v1/' "$V1_PATH/metadata.env"
(
  cd -- "$V1_PATH"
  sha256sum db.dump media.tar.gz > SHA256SUMS
)
if ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/restore_prod.sh" --confirm "$V1_PATH"; then
  production_fail "v1 restore accepted submission rows without private files."
fi
production_compose run --rm --no-deps web \
  test -f /app/private-artifacts/.restore-in-progress
ENV_FILE="$ENV_FILE" bash "$SCRIPT_DIR/restore_prod.sh" --recover --confirm
wait_for_web
production_compose exec -T web python manage.py audit_donor_report_submissions \
  --strict --quiescent

printf 'Production backup and restore drill passed.\n'
