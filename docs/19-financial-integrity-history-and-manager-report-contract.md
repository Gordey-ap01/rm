# Контракт среза: financial-integrity-history-and-manager-report

Дата: 2026-07-15

Статус: accepted for current epic. Docs-only контракт создан, DB-owner Slice 1 выполнен; detail timeline UI и manager trend report еще не реализованы.

Назначение: подготовить безопасный следующий этап после `financial-integrity-runner-operations`: человекочитаемую историю разбора financial integrity findings и read-only отчет руководителя по динамике финансовых расхождений. Контракт нужен до любых изменений `operations/models.py`, migration chain, runner-а, triage service или UI отчета.

## Контекст

Уже есть:

- `FinancialIntegrityCheckRun` - сохраненные запуски проверки, счетчики и failed-run error;
- `FinancialIntegrityFinding` - deduplicated persisted finding с текущим status/severity/source snapshot;
- django-auditlog registration для check runs и findings;
- triage service для `acknowledge`, `return_to_open`, `ignore`, `reopen`;
- work queue actions, finding detail, scoped appointment recheck, latest-run summary;
- production command `run_financial_integrity_check`.

Остается недостаточно покрыто:

- обычному администратору неудобно читать сырые `LogEntry` из auditlog admin;
- detail page не имеет доменного timeline-а по finding-у;
- руководителю нужен периодный контроль: сколько новых, решенных, игнорируемых, просроченных и повторяющихся расхождений;
- trend report не должен запускать аудит на GET и не должен исправлять финансовые данные.

## Решение

Добавить отдельный DB-owner срез для typed event history, а затем отдельный UI/report срез. Не смешивать миграцию event table, запись событий и отчет руководителя в один большой commit.

Первый event-table срез должен быть OLTP-normalized: хранить события изменения состояния и источник события, а не предрассчитанные управленческие агрегаты. Trend report сначала строится read-only запросами по `FinancialIntegrityCheckRun`, `FinancialIntegrityFinding` и `FinancialIntegrityFindingEvent`; отдельная aggregate table допускается только после измерения производительности на реальных данных.

## Предлагаемая модель

### FinancialIntegrityFindingEvent

Назначение: неизменяемое доменное событие по finding-у, пригодное для timeline-а и периодных отчетов.

Поля:

- `id` - стандартный PK.
- `finding` - nullable FK to `FinancialIntegrityFinding`, `on_delete=SET_NULL`, indexed. Event должен переживать случайное удаление finding-а в admin.
- `event_key` - `CharField(max_length=64, unique=True)`, idempotency key для защиты от повторной записи одного события при retry.
- `event_type` - choices:
  - `created`;
  - `acknowledged`;
  - `returned_to_open`;
  - `ignored`;
  - `reopened`;
  - `resolved`;
  - `scoped_recheck`;
  - `note_added` only if future UI adds standalone notes.
- `event_at` - `DateTimeField(default=timezone.now, db_index=True)`.
- `run` - nullable FK to `FinancialIntegrityCheckRun`, `on_delete=SET_NULL`, indexed.
- `actor` - nullable FK to `settings.AUTH_USER_MODEL`, `on_delete=SET_NULL`, indexed.
- `status_from`, `status_to` - short strings from `FinancialIntegrityFinding.Status`; blank allowed.
- `severity` - snapshot of severity at event time.
- `code` - snapshot of issue code.
- `issue_key` - snapshot of finding issue key.
- `message` - snapshot of finding message at event time.
- `note` - user note or system reason, blank allowed.
- `source_snapshot` - JSON snapshot with appointment/account/funding/participant/ledger ids and display labels needed when FK targets disappear.

Indexes:

- `["finding", "-event_at"]` for detail timeline.
- `["event_type", "-event_at"]` for report filters.
- `["run", "event_type"]` for diagnostics per check run.
- `["code", "-event_at"]` for recurring issue patterns.
- `["status_to", "-event_at"]` for resolved/ignored/reopened trends.

Do not add a separate aggregate table in the first migration.

## Выполнение 2026-07-15: event schema/service slice

Выполнен DB-owner Slice 1 без UI-отчета и без timeline rendering.

- Добавлена модель `FinancialIntegrityFindingEvent` и миграция `operations.0023_financialintegrityfindingevent`.
- Event table append-only на уровне сервиса: `event_key`, `event_type`, `event_at`, nullable links to finding/run/actor, status transition fields, code/severity/issue snapshots, note and `source_snapshot`.
- Добавлен service `operations.services.financial_integrity_events.record_finding_event()` с idempotency через `event_key`.
- `FinancialIntegrityFindingEvent` зарегистрирован в Django admin and auditlog.
- Runner пишет events внутри transaction:
  - `created` для нового finding;
  - `resolved` при исчезновении active finding из проверки;
  - `reopened` при повторном появлении previously resolved finding.
- Triage service пишет events внутри transaction для `acknowledged`, `returned_to_open`, `ignored`, `reopened`.
- Scoped appointment recheck на detail page пишет `scoped_recheck` event after successful POST run.
- No noisy `seen_again` event добавлен не был; repeated check of same active finding обновляет `last_seen_at/last_seen_run`, но не раздувает event table.
- Модели финансовых фактов, billing/ledger/payroll/grants/reports/status semantics, auto-fix/backfill и manager report UI не менялись.

Проверки:

- `ruff check` по затронутым Python files and migration прошел;
- `pytest operations/tests/test_services.py -q --tb=short` прошел (`145 passed`);
- `pytest operations/tests/test_auditlog.py -q` прошел (`6 passed`);
- `pytest operations/tests/test_views.py::WorkQueueViewTests -q` прошел (`30 passed`);
- `manage.py check --settings=rehab_center.settings_test` прошел;
- `manage.py makemigrations --check --dry-run --settings=rehab_center.settings_test` показал `No changes detected`;
- полный `pytest -q --tb=short` прошел (`492 passed`, 1 прежнее предупреждение django-tasks).
- `graphify update . --no-cluster` обновил code-index до `4222` nodes / `15254` edges; semantic extraction не запускалась.

Следующий срез по этому контракту: detail timeline UI на existing finding detail page, либо read-only manager trend report. Не начинать оба одновременно; UI worker может стартовать только после этого DB-owner commit.

## Выполнение 2026-07-15: detail timeline UI slice

Выполнен UI-срез без новой миграции и без изменений финансовой семантики.

- Existing finding detail page `/financial-integrity/findings/<id>/` теперь получает `event_rows` из `FinancialIntegrityFindingEvent`.
- Timeline показывает последние 20 событий по finding: тип события, время, actor/system label, status transition, note и run id.
- GET detail page не создает новых событий; запись событий остается только в runner/triage/scoped recheck paths.
- Добавлены CSS-стили `financial-integrity-history` / `financial-integrity-event` поверх существующей `compact-timeline`; mobile layout не должен давать горизонтальный overflow.
- Тесты detail page проверяют наличие timeline, отсутствие side-effect event creation на GET и отображение note из triage event.
- Manager trend report остается следующим отдельным read-only срезом; он не был начат в этом UI-срезе.

Проверки:

- `ruff check operations/views/dashboard.py operations/tests/test_views.py` прошел;
- `pytest operations/tests/test_views.py::WorkQueueViewTests -q --tb=short` прошел (`30 passed`);
- `manage.py check --settings=rehab_center.settings_test` прошел;
- `manage.py makemigrations --check --dry-run --settings=rehab_center.settings_test` показал `No changes detected`;
- полный `pytest -q --tb=short` прошел (`492 passed`, 1 прежнее предупреждение django-tasks);
- Playwright Browser QA fallback прошел на `/financial-integrity/findings/8/` desktop 1365x900 и mobile 390x844: history block `1`, events `2`, note visible, payload visible, overflow `0`, console/page errors and 4xx/5xx responses отсутствуют. Артефакты: `%TEMP%\rmcodex-browser-qa-financial-timeline`; QA data очищены, runserver на 8068 остановлен.
- `graphify update . --no-cluster` обновил code-index до `4229` nodes / `15275` edges; semantic extraction не запускалась.

## Event Recording Rules

Events are append-only. UI must not edit or delete them.

Runner records, inside the same transaction as finding changes:

- `created` when a new `FinancialIntegrityFinding` is created;
- `resolved` when an open/acknowledged finding disappears from a check and runner marks it resolved;
- `reopened` when a previously resolved finding appears again and runner returns it to open;
- `scoped_recheck` only when user explicitly runs scoped appointment recheck from finding detail.

Triage service records, inside the same transaction as the status change:

- `acknowledged`;
- `returned_to_open`;
- `ignored`;
- `reopened` from ignored/resolved.

Do not record a noisy `seen_again` event on every run in the first implementation. `last_seen_at` and `last_seen_run` already cover that operational fact. If руководителю later needs a full observation history, add `seen_again` in a separate performance-reviewed slice.

## Migration Plan

Slice 1 must be a single DB-owner migration:

- add `FinancialIntegrityFindingEvent`;
- register in Django admin and auditlog;
- add service helper `record_financial_integrity_event(...)`;
- write tests for constraints, idempotency, indexes through `makemigrations --check --dry-run`, and event creation in service-level paths.

No data backfill in the schema migration. If existing findings need baseline events, add a separate management command:

- dry-run by default;
- creates one `created`/`baseline_imported`-style event per existing finding only after explicit flag;
- never changes finding status, ledger, accounts, payroll, grants or billing decisions.

Potentially dangerous operations:

- adding non-null FK or non-null status fields with existing data;
- backfilling inside the migration;
- deriving events from auditlog text payloads;
- deleting or rewriting existing auditlog entries;
- adding report aggregates before access patterns are measured.

## Manager Report Scope

First report should be read-only and period-filtered:

- latest run status and failure message;
- runs count, failed runs, checked appointments;
- current active findings by severity/status/code;
- new findings in period;
- resolved findings in period;
- ignored findings in period;
- reopened findings in period;
- oldest active finding age buckets;
- top recurring codes and funding/account/source context, without unnecessary personal data.

Report must link to existing finding detail pages. It must not run a check on GET and must not offer auto-fix.

## UX/UI Map

Finding detail:

- add timeline below current source/payload/actions;
- show event type, time, actor, status transition, note, run link/id;
- keep source snapshot visible even when FK is gone;
- collapse long snapshots/payloads.

Manager report:

- period filter: 7/30/90 days and custom dates;
- summary strip for active/new/resolved/ignored/reopened/failed-run counts;
- table grouped by issue code and severity;
- action links to work queue filtered anchor or finding detail;
- no charts in first slice unless table readability is insufficient.

Before concrete report UI implementation, use Lazyweb/design research if available because this is product UI. If Lazyweb is unavailable, follow existing Bootstrap/dashboard patterns and verify with Playwright desktop/mobile.

## Acceptance Criteria

Schema/event slice:

- migration adds only event table and indexes;
- event service is idempotent through `event_key`;
- runner and triage service create expected events in focused tests;
- no GET request writes events;
- no financial data is auto-fixed;
- full pytest passes;
- auditlog/admin registration exists.

Detail timeline slice:

- finding detail shows ordered events newest/oldest according to product choice;
- missing FK does not break event rendering because snapshots exist;
- actions still use existing triage service;
- Browser QA passes desktop/mobile.

Manager report slice:

- report is read-only and period-filtered;
- no audit runs on GET;
- counts match event/run/finding fixtures;
- links lead to detail/work queue;
- Browser QA passes desktop/mobile;
- no PII-heavy export is introduced.

## Parallel Agents

Do not parallelize migrations.

- Lead DB owner: owns `operations/models.py`, migration file, event service and runner/triage event emission.
- UI worker: starts only after DB-owner commit; may work on detail timeline and manager report templates/CSS/tests.
- QA/docs worker: may update docs, run checks and Browser QA; no model/migration edits.

No two agents may edit `operations/models.py`, `operations/migrations/*`, `operations/services/financial_integrity_checks.py` or `operations/services/financial_integrity_triage.py` concurrently during this epic.

## Open Questions

1. Нужен ли отдельный manager permission для просмотра ignored/resolved history, или пока достаточно staff/admin?
2. Нужна ли baseline-import command для existing findings, или история начинается с момента внедрения event table?
3. Должны ли ignored findings попадать в отдельный архив руководителя на первом report-срезе?
4. Нужен ли `seen_again` event на каждом запуске после реальных замеров, или достаточно `last_seen_at/last_seen_run`?
5. Какой период отчета руководителя считать default: 7 дней, 30 дней или текущий месяц?
