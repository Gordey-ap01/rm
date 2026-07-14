# Контракт среза: financial-integrity-triage-and-runner

Дата: 2026-07-15

Статус: draft, готов к review перед любым кодом, который меняет triage-статусы или запускает проверки из UI/расписания.

Назначение: превратить persisted financial integrity findings из пассивного сигнала в управляемый рабочий процесс администратора/руководителя: разбор, подтверждение, игнорирование, повторная проверка и эксплуатационный запуск runner-а. Срез не исправляет финансовые данные автоматически и не меняет семантику списаний, ledger, payroll, grants, reports или `billing.apply_decision()`.

## Текущее основание

Уже выполнено:

- read-only audit source: `operations/services/financial_integrity.py`;
- stable issue keys: `financial_integrity_issue_key(issue)`;
- persisted cache schema: `FinancialIntegrityCheckRun`, `FinancialIntegrityFinding`, migration `operations.0022`;
- runner service: `operations.services.financial_integrity_checks.run_financial_integrity_check()`;
- management command: `run_financial_integrity_check`;
- dashboard/work queue cached reader: UI читает active `FinancialIntegrityFinding` (`open`, `acknowledged`) и не запускает audit на GET.

Не выполнено:

- event table for status history;
- scheduled/background invocation;
- manager trend report.

## Выполнение 2026-07-15: audit/admin visibility

Выполнен первый кодовый срез `financial-integrity-audit-admin-visibility`.

- `FinancialIntegrityCheckRun` and `FinancialIntegrityFinding` зарегистрированы в `operations/apps.py` auditlog registry.
- Добавлены Django admin classes для check runs and findings: list display, filters, search, autocomplete links and date hierarchy.
- Добавлены tests: новые модели присутствуют в auditlog registry, присутствуют в admin registry, update finding-а пишет auditlog `LogEntry` с изменениями `status` and `triage_note`.
- Модели, миграции, runner semantics, billing/ledger/payroll/grants/reports/statuses не менялись.

Проверки:

- `ruff check operations/apps.py operations/admin.py operations/tests/test_auditlog.py` прошел;
- `pytest operations/tests/test_auditlog.py -q` прошел (`6 passed`, 1 прежнее предупреждение django-tasks);
- `manage.py check --settings=rehab_center.settings_test` прошел;
- `manage.py makemigrations --check --dry-run --settings=rehab_center.settings_test` показал `No changes detected`;
- полный `pytest -q --tb=short` прошел (`470 passed`, 1 прежнее предупреждение django-tasks);
- `git diff --check` показал только стандартные LF->CRLF warnings.
- `graphify update . --no-cluster` обновил code-index до `4137` nodes / `14844` edges; semantic extraction не запускалась.

Следующий кодовый срез по контракту: `financial-integrity-triage-service`.

## Выполнение 2026-07-15: triage service

Выполнен кодовый срез `financial-integrity-triage-service`.

- Добавлен `operations/services/financial_integrity_triage.py`.
- Service exposes explicit finding-only actions:
  - `acknowledge_finding()`: `open -> acknowledged`;
  - `return_finding_to_open()`: `acknowledged -> open`;
  - `ignore_finding()`: `open/acknowledged -> ignored`, requires non-empty note;
  - `reopen_finding()`: `ignored/resolved -> open`, clears `resolved_at/resolved_run`.
- Все actions требуют `actor`; triage fields обновляются через `triaged_by`, `triaged_at`, `triage_note`.
- Invalid transitions raise `FinancialIntegrityTriageError`.
- UI, URLs, templates, models, migrations, runner semantics, billing/ledger/payroll/grants/reports/statuses не менялись.

Проверки:

- `ruff check operations/services/__init__.py operations/services/financial_integrity_triage.py operations/tests/test_services.py` прошел;
- `pytest operations/tests/test_services.py -q --tb=short` прошел (`144 passed`, 1 прежнее предупреждение django-tasks);
- `manage.py makemigrations --check --dry-run --settings=rehab_center.settings_test` показал `No changes detected`;
- `manage.py check --settings=rehab_center.settings_test` прошел;
- полный `pytest -q --tb=short` прошел (`478 passed`, 1 прежнее предупреждение django-tasks);
- `git diff --check` показал только стандартные LF->CRLF warnings.
- `graphify update . --no-cluster` обновил code-index до `4161` nodes / `14938` edges; semantic extraction не запускалась.

Следующий кодовый срез по контракту: `financial-integrity-work-queue-triage-actions`.

## Выполнение 2026-07-15: work queue triage actions

Выполнен кодовый срез `financial-integrity-work-queue-triage-actions`.

- В `#queue-financial-integrity` добавлены POST-действия для active findings:
  - `open -> acknowledged` через кнопку `Принять`;
  - `acknowledged -> open` через кнопку `Вернуть`;
  - `open/acknowledged -> ignored` через кнопку `Игнорировать` с обязательной причиной.
- Добавлен route `financial_integrity_finding_triage` и view, который вызывает только `operations.services.financial_integrity_triage`.
- POST использует CSRF, текущий staff/admin permission через `is_admin_user`, safe `next` через `safe_next_url` и возвращает администратора в `#queue-financial-integrity`.
- Ошибочные действия показывают message и не меняют finding.
- `ignored` скрывается из dashboard/work queue через уже существующий active-reader (`open`, `acknowledged`).
- Модели, миграции, runner semantics, billing/ledger/payroll/grants/reports/statuses не менялись.

Проверки:

- `ruff check operations/views/dashboard.py operations/urls.py operations/views/__init__.py operations/tests/test_views.py` прошел;
- `pytest operations/tests/test_views.py::WorkQueueViewTests -q` прошел (`23 passed`);
- `manage.py check --settings=rehab_center.settings_test` прошел;
- `manage.py makemigrations --check --dry-run --settings=rehab_center.settings_test` показал `No changes detected`;
- полный `pytest -q --tb=short` прошел (`484 passed`, 1 прежнее предупреждение django-tasks);
- Playwright Browser QA fallback прошел на desktop 1280x900 и mobile 390x900: open finding показывает `Принять`/`Игнорировать`, acknowledge меняет статус и показывает `Вернуть`, ignore скрывает только ignored finding, mobile card не имеет horizontal overflow, console/page errors нет. Артефакты: `%TEMP%\rmcodex-browser-qa-financial-triage-actions`; QA data очищены, runserver на 8065 остановлен.
- `graphify update . --no-cluster` обновил code-index до `4171` nodes / `14961` edges; semantic extraction не запускалась.

## Выполнение 2026-07-15: finding detail

Выполнен кодовый срез `financial-integrity-finding-detail`.

- Добавлена отдельная страница `financial_integrity_finding_detail` для persisted `FinancialIntegrityFinding`.
- Work queue получил ссылку `Разбор` на detail page для каждого active finding.
- Страница показывает severity/status/code/message, first/last seen, first/last/resolved run, triage state, denormalized snapshot и source links на занятие, счет баланса и источник финансирования, когда FK еще доступны.
- При удаленных исходных объектах страница использует сохраненный snapshot и не ломается; scoped recheck скрывается, если нет связанного appointment.
- На detail page доступны те же safe triage actions через существующий service/view: acknowledge, return_to_open, ignore с обязательной причиной, reopen для ignored/resolved.
- Добавлен scoped recheck POST `financial_integrity_finding_recheck`, который запускает persisted runner только для связанного занятия и возвращает администратора на страницу разбора.
- Модели, миграции, runner semantics, billing/ledger/payroll/grants/reports/statuses не менялись.

Проверки:

- `ruff check operations/views/dashboard.py operations/urls.py operations/views/__init__.py operations/tests/test_views.py` прошел;
- `pytest operations/tests/test_views.py::WorkQueueViewTests -q` прошел (`27 passed`);
- `manage.py check --settings=rehab_center.settings_test` прошел;
- `manage.py makemigrations --check --dry-run --settings=rehab_center.settings_test` показал `No changes detected`;
- полный `pytest -q --tb=short` прошел (`488 passed`, 1 прежнее предупреждение django-tasks);
- Playwright Browser QA fallback прошел на desktop 1280x900 и mobile 390x900: work queue содержит detail link, detail page показывает source snapshot, actions, payload, acknowledge переключает action set, scoped recheck возвращает на detail page, mobile overflow `0`, console/page errors нет. Артефакты: `%TEMP%\rmcodex-browser-qa-financial-detail`; QA data очищены, runserver на 8066 остановлен.
- `graphify update . --no-cluster` обновил code-index до `4182` nodes / `15020` edges; semantic extraction не запускалась.

Следующий кодовый срез по контракту: `financial-integrity-runner-operations`.

## Инварианты

1. Triage actions are finding-only mutations. Они не меняют appointment, ledger, balance account, payroll, grant quota, payment or report facts.
2. `resolved` выставляет runner, когда issue больше не найден. UI не должен вручную переводить active finding в `resolved`, чтобы не скрыть реальную финансовую проблему.
3. `ignored` означает: finding не показывается в dashboard/work queue, но runner продолжает обновлять `last_seen_at` и snapshot, если issue снова виден.
4. `ignored` не должен автоматически reopening-иться текущим runner-ом. Reopen from ignored is explicit user action.
5. Все POST actions требуют staff/admin permission через текущий `is_admin_user`; если появится отдельная роль руководителя, `ignore` должен быть первым кандидатом на более строгий permission.
6. Любая scheduled/background проверка должна быть идемпотентной и не должна блокировать пользовательские GET-запросы.
7. No real personal data, secrets, production config or real Excel exports in fixtures/docs/commits.

## Статусы и переходы

Текущие статусы:

- `open`: найдено и требует внимания;
- `acknowledged`: принято в работу, все еще активно;
- `resolved`: runner больше не видит issue;
- `ignored`: сознательно скрыто из operational queue.

Разрешенные UI переходы:

| Action | From | To | Кто | Примечание |
| --- | --- | --- | --- | --- |
| acknowledge | `open` | `acknowledged` | staff/admin | фиксирует `triage_note`, `triaged_by`, `triaged_at` |
| return_to_open | `acknowledged` | `open` | staff/admin | если приняли ошибочно или надо вернуть в очередь |
| ignore | `open`, `acknowledged` | `ignored` | staff/admin now; ideally manager later | требует non-empty note |
| reopen | `ignored`, `resolved` | `open` | staff/admin | ручной возврат в active queue |
| run_scoped_check | any with appointment | status decided by runner | staff/admin | безопаснее ручного resolve |

Запрещенные UI переходы:

- manual `open/acknowledged -> resolved`;
- direct mutation of severity/code/source-object fields from UI;
- auto-fix of ledger/billing/account data from finding card.

## Audit trail choice

Первый безопасный кодовый путь без миграций:

- зарегистрировать `FinancialIntegrityCheckRun` and `FinancialIntegrityFinding` in `operations/apps.py` auditlog registry;
- добавить Django admin registration для read/search/debug;
- использовать existing fields `triage_note`, `triaged_by`, `triaged_at` for current state;
- rely on django-auditlog LogEntry for create/update/delete history.

Когда нужен отдельный DB-owner срез:

- если руководителю нужен человекочитаемый timeline per finding без захода в auditlog admin;
- если надо хранить typed events: `created`, `seen_again`, `acknowledged`, `ignored`, `reopened`, `resolved`;
- если надо показывать историю на finding detail page обычному администратору.

Event table нельзя добавлять параллельно с другими migration work. Один DB owner owns `operations/models.py` and migration chain.

## UX/UI контур

### Work queue

- Active findings remain in `#queue-financial-integrity`.
- Each card gets small POST actions:
  - `Принять в работу` for `open`;
  - `Вернуть в очередь` for `acknowledged`;
  - `Игнорировать` for `open/acknowledged`, with required note;
  - `Открыть разбор` to detail page.
- No action should be a plain GET mutation.
- POST redirects back to safe `next` URL and preserves anchor.

### Finding detail

Route proposal: `/financial-integrity/findings/<id>/`.

Detail shows:

- severity, status, code, message;
- source links: appointment, participant, ledger, balance account, funding source when FK exists;
- denormalized snapshot even if FK is null;
- first/last seen, first/last/resolved run;
- triage note, triaged_by, triaged_at;
- safe actions and scoped recheck if appointment exists;
- auditlog/events section only if implementation has a reliable source.

### Runner controls

First production-safe option:

- keep management command for full check;
- optional UI button `Запустить проверку сейчас` only for staff/admin and only after performance is acceptable on real data;
- show latest completed/failed run summary near financial queue section.

Scheduled/background option:

- start with external scheduler invoking `python manage.py run_financial_integrity_check --run-type scheduled`;
- do not add in-process scheduler until production deployment path is documented;
- if using `django-tasks`, add a separate contract for worker lifecycle, retries, and monitoring.

## Вертикальные срезы

### Slice 1: audit/admin visibility

Files likely touched:

- `operations/apps.py`;
- `operations/admin.py`;
- `operations/tests/test_auditlog.py`;
- `operations/tests/test_views.py` or focused admin tests if present.

Acceptance criteria:

- `FinancialIntegrityCheckRun` and `FinancialIntegrityFinding` are registered in auditlog.
- Admin can search/filter findings by status, severity, code, appointment/account/funding.
- No model/migration change.
- Full pytest passes.

### Slice 2: triage service

Files likely touched:

- `operations/services/financial_integrity_triage.py`;
- `operations/tests/test_services.py`.

Acceptance criteria:

- Service exposes explicit actions: acknowledge, return_to_open, ignore, reopen.
- Service rejects manual resolve of active finding.
- Ignore requires non-empty note.
- Triage fields are set consistently.
- No financial domain objects mutate.

### Slice 3: work queue triage actions

Files likely touched:

- `operations/views/dashboard.py` or dedicated financial integrity view module;
- `urls.py`;
- `templates/operations/work_queue.html`;
- `operations/tests/test_views.py`.

Acceptance criteria:

- Work queue active cards expose only valid POST actions for current status.
- Actions use CSRF, staff permission and safe `next`.
- Success/error messages are shown.
- Resolved/ignored findings remain hidden after action.
- Browser QA passes desktop/mobile; no overlap/overflow.

### Slice 4: finding detail

Files likely touched:

- dedicated view/template/urls;
- CSS only if existing styles are insufficient;
- focused view tests.

Acceptance criteria:

- Detail page renders source links and denormalized snapshot.
- Missing FK does not break rendering.
- Scoped recheck button runs runner only for that appointment and reports result.
- Browser QA passes desktop/mobile.

### Slice 5: runner operations

Files likely touched:

- management command tests;
- docs/deployment or operations docs;
- optional view only if UI manual full-run is approved.

Acceptance criteria:

- Production run schedule is documented.
- Failed run is visible in latest run summary.
- No GET request runs the full audit.
- Full pytest and smoke command pass.

## Parallel agents

Safe parallel split after this contract is accepted:

- Lead/DB owner: owns any `operations/models.py`, migrations, runner semantics, and status transition service.
- UI worker: may work on `templates/operations/work_queue.html` and CSS only after triage service contract is committed.
- Docs/QA worker: may update docs and run Browser QA, no model/migration edits.
- No two agents edit `operations/models.py` or migration chain concurrently.
- No worker changes `billing.py`, ledger posting, payroll, grant reports or appointment statuses in this epic.

## Open questions before code

1. Should `ignore` be allowed to all staff admins for now, or only superusers until a manager role exists?
2. Is django-auditlog enough for first production triage history, or do we need `FinancialIntegrityFindingEvent` before UI actions?
3. Should the first manual runner UI allow full check, scoped appointment check only, or no UI run button at all?
4. Desired production schedule: hourly, nightly, after billing actions, or manual only until real data volume is measured?
5. Should ignored findings appear in a separate manager-only archive page?
