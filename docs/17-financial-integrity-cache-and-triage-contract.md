# Контракт среза: financial-integrity-cache-and-triage

Дата: 2026-07-15

Статус: draft, готов к обсуждению перед любыми миграциями БД.

Назначение: перевести финансовый audit из синхронной проверки dashboard/work queue в production-scale контур с сохраненными finding-ами, triage-статусами и контролируемым запуском проверок, не меняя семантику списаний, ledger, payroll, grants и `billing.apply_decision()`.

## Выполнение 2026-07-15: schema and runner

Выполнен первый DB-backed кодовый срез `financial-integrity-cache-schema-and-runner`.

- Добавлены additive модели `FinancialIntegrityCheckRun` и `FinancialIntegrityFinding`.
- Добавлена миграция `operations/migrations/0022_financialintegritycheckrun_financialintegrityfinding_and_more.py`.
- Все source-object ссылки finding-а используют `SET_NULL`; audit snapshot дополнительно хранит denormalized display fields и JSON payload.
- Добавлен writer service `operations.services.financial_integrity_checks.run_financial_integrity_check()`.
- Runner создает check run, вызывает read-only `audit_appointments()`, upsert-ит finding-и по `financial_integrity_issue_key()`, обновляет counts и помечает unseen open/acknowledged findings as resolved.
- Resolved finding автоматически reopened, если тот же `issue_key` снова найден.
- Scoped run with explicit `appointments` resolves unseen findings only внутри выбранных appointments; full run resolves all unseen open/acknowledged findings.
- Dashboard/work queue еще не переключались на persisted findings; auto-fix/backfill/triage UI не добавлялись.
- Не менялись `billing.apply_decision()`, ledger posting, payroll, grants, reports, statuses.

Проверки:

- `ruff check operations/models.py operations/services/__init__.py operations/services/financial_integrity.py operations/services/financial_integrity_checks.py operations/tests/test_services.py` прошел;
- `pytest operations/tests/test_services.py -q --tb=short` прошел (`135 passed`, 1 прежнее предупреждение django-tasks);
- `manage.py check` прошел;
- `manage.py makemigrations --check --dry-run` показал `No changes detected`;
- полный `pytest -q --tb=short` прошел (`465 passed`, 1 прежнее предупреждение django-tasks);
- `git diff --check` показал только стандартные LF->CRLF warnings;
- `graphify update . --no-cluster` обновил code-index до `4102` nodes / `14589` edges; semantic extraction не запускалась.

Следующий кодовый срез по этому контракту: reader switch для dashboard/work queue на persisted open findings, но только после отдельного acceptance review и с Browser QA, потому что будут меняться views/templates.

## Выполнение 2026-07-15: management command

Добавлен безопасный способ наполнить persisted cache без shell-доступа:

- `operations/management/commands/run_financial_integrity_check.py`;
- команда вызывает `run_financial_integrity_check(run_type="management_command")`;
- выводит summary по checked appointments and issue counts;
- финансовые данные не исправляет, auto-fix/backfill не запускает, dashboard/work queue не переключает.

Проверки:

- `ruff check operations/management/commands/run_financial_integrity_check.py operations/services/financial_integrity_checks.py operations/tests/test_services.py` прошел;
- `pytest operations/tests/test_services.py -q --tb=short` прошел (`136 passed`, 1 прежнее предупреждение django-tasks);
- `manage.py check` прошел;
- `manage.py makemigrations --check --dry-run` показал `No changes detected`;
- полный `pytest -q --tb=short` прошел (`466 passed`, 1 прежнее предупреждение django-tasks);
- `git diff --check` показал только стандартные LF->CRLF warnings;
- `graphify update . --no-cluster` обновил code-index до `4109` nodes / `14604` edges; semantic extraction не запускалась.

## Подготовительный срез 2026-07-15: issue key foundation

До миграций выполнен безопасный helper-only срез:

- добавлен `operations.services.financial_integrity.financial_integrity_issue_key(issue)`;
- ключ строится как SHA-256 fingerprint по stable issue code и связанным object ids: appointment, participant, ledger entry, account, funding source;
- message/severity не входят в ключ, чтобы изменение текста или уровня важности не создавало дубликат persisted finding-а;
- разные participant/ledger contexts дают разные ключи, чтобы не склеивать разные финансовые расхождения;
- БД, модели, миграции, billing/ledger/payroll/grants/status semantics не менялись.

Проверки:

- `ruff check operations/services/financial_integrity.py operations/tests/test_services.py` прошел;
- `pytest operations/tests/test_services.py -q --tb=short` прошел (`130 passed`, 1 прежнее предупреждение django-tasks);
- `manage.py check` прошел;
- `manage.py makemigrations --check --dry-run` показал `No changes detected`;
- полный `pytest -q --tb=short` прошел (`460 passed`, 1 прежнее предупреждение django-tasks);
- `git diff --check` показал только стандартные LF->CRLF warnings;
- `graphify update . --no-cluster` обновил code-index до `4076` nodes / `14497` edges; semantic extraction не запускалась.

## Почему нужен отдельный контракт

Срез `dashboard-work-queue-financial-integrity-signal` сознательно считает audit синхронно по candidate appointments с charge/debit ledger. Это безопасно для первого read-only сигнала, но плохо масштабируется на большой истории и не дает руководителю нормальный процесс контроля: кто увидел расхождение, что с ним решили, когда оно исчезло, какие issue-ы повторяются.

Следующий шаг не должен расширять dashboard-query. Нужен отдельный слой:

- сохранять обнаруженные расхождения как finding-и;
- обновлять `last_seen_at` при повторном обнаружении;
- помечать finding resolved, когда audit перестал его видеть;
- позволять администратору/руководителю triage без auto-fix;
- показывать dashboard/work queue из сохраненного состояния, а не из полного пересчета;
- оставить auto-fix/backfill отдельным будущим контрактом.

## Инварианты

1. `operations.services.financial_integrity.audit_appointments()` остается источником детекции issue-ов.
2. Срез не меняет `billing.apply_decision()`, ledger posting rules, payroll/grant/report semantics.
3. Finding-и read-only относительно финансовых данных: triage меняет только состояние finding-а.
4. Никаких auto-fix, backfill, constraints на ledger или массового исправления старых данных.
5. Dashboard/work queue не должны запускать полный audit по истории при каждом GET после переключения на cache.
6. Любая миграция БД выполняется одним владельцем; нескольким агентам нельзя параллельно менять `operations/models.py` и migration chain.
7. Все новые модели должны иметь понятный `on_delete`, индексы под реальные access patterns и обратимую migration path.

## Предлагаемая доменная модель

### FinancialIntegrityCheckRun

Запись о запуске проверки.

Поля:

- `run_type`: `manual`, `scheduled`, `management_command`, возможно `system`.
- `status`: `running`, `completed`, `failed`.
- `started_at`, `finished_at`.
- `requested_by` nullable FK `auth.User` with `SET_NULL`.
- `candidate_count`, `issue_count`, `error_count`, `warning_count`, `info_count`.
- `error_message` nullable text.
- `created_at`, `updated_at`.

Индексы:

- `(status, started_at)`;
- `(run_type, started_at)`;
- `started_at DESC` для истории запусков.

### FinancialIntegrityFinding

Persisted finding, deduplicated across check runs.

Поля:

- `issue_key`: stable hash/fingerprint, unique.
- `code`: stable issue code from audit service.
- `severity`: `error`, `warning`, `info`.
- `status`: `open`, `acknowledged`, `resolved`, `ignored`.
- nullable FK links with `SET_NULL`: `appointment`, `appointment_participant`, `ledger_entry`, `account`, `funding_source`.
- `first_seen_at`, `last_seen_at`, `resolved_at`.
- `first_seen_run`, `last_seen_run`, `resolved_run` FK to `FinancialIntegrityCheckRun`, `SET_NULL`.
- `message`: last audit message.
- denormalized display fields: appointment date/service, participant name, account label, funding source label, ledger entry type/amount.
- `payload`: JSONField with raw IDs/facts needed for diagnosis.
- `triage_note` nullable text.
- `triaged_by` nullable FK `auth.User`, `SET_NULL`.
- `triaged_at` nullable timestamp.
- `created_at`, `updated_at`.

Индексы:

- unique `issue_key`;
- `(status, severity, last_seen_at)`;
- `(code, status)`;
- `(appointment, status)`;
- `(account, status)`;
- `(funding_source, status)`.

Обоснование денормализации: finding является audit-снимком. Если занятие, счет или источник позже изменены/удалены, руководителю все равно нужна история того, что именно проверка увидела.

### FinancialIntegrityFindingEvent, опционально

Журнал изменения triage-статусов.

Поля:

- `finding` FK cascade;
- `event_type`: `created`, `seen_again`, `acknowledged`, `ignored`, `resolved`, `reopened`;
- `old_status`, `new_status`;
- `actor` nullable FK `auth.User`;
- `note`;
- `created_at`.

Первый DB-срез может отложить event table, если acceptance ограничивается snapshot/cache. Но если руководителю важна история "кто закрыл и почему", event table лучше добавить сразу.

## Access patterns

- Dashboard: count open findings by severity, fast aggregate by indexed `status/severity`.
- Work queue: first 40 open findings ordered by severity priority and `last_seen_at DESC`.
- Finding detail/history: show finding, source object links and triage history.
- Manual check: run audit, upsert findings, resolve findings not seen in current run.
- Scheduled check: same as manual, without user.
- Manager report: trends by code/severity/funding source over date range.

## Миграционная стратегия

Фаза 1, schema-only:

- добавить модели `FinancialIntegrityCheckRun` and `FinancialIntegrityFinding`;
- опционально добавить `FinancialIntegrityFindingEvent`;
- не переключать dashboard/work queue;
- миграция должна быть additive and reversible.

Фаза 2, writer:

- management command or service `run_financial_integrity_check()`;
- строит candidate queryset, вызывает `audit_appointments()`, upserts finding-и by `issue_key`;
- обновляет counts in run;
- помечает open/acknowledged finding-и as resolved, если они не seen in current run;
- focused service tests on create/update/resolve/reopen.

Фаза 3, reader switch:

- dashboard/work queue читают persisted open findings;
- если run never completed, показывают controlled empty/info state, а не запускают full audit silently;
- dynamic audit fallback допустим только behind explicit setting/test helper, не в production path.

Фаза 4, triage UI:

- work queue/detail actions: acknowledge, ignore, add note, maybe reopen;
- все POST actions меняют только finding state, not financial data;
- Browser QA desktop/mobile.

Фаза 5, schedule:

- подключить scheduled task после проверки локального runtime and production deploy path;
- добавить last-run indicator and failure state.

## Первый кодовый срез, предлагаемый

Рабочее имя: `financial-integrity-cache-schema-and-runner`.

Минимальный состав:

- модели and migration for `FinancialIntegrityCheckRun` and `FinancialIntegrityFinding`;
- service function `run_financial_integrity_check(requested_by=None, run_type="manual")`;
- issue fingerprint helper;
- tests for finding create/update/resolve/reopen;
- no dashboard/work queue switch yet;
- no POST triage UI yet.

Acceptance criteria:

- `audit_appointments()` остается единственным источником issue detection.
- Re-running same audit does not duplicate finding by `issue_key`.
- Finding changes from `open` to `resolved` when issue disappears in a later completed run.
- Resolved finding reopens if same `issue_key` appears again.
- Counts on `FinancialIntegrityCheckRun` match created/seen issues by severity.
- Migration dry-run and full pytest pass.
- No changes to billing, ledger posting, payroll, grants, statuses or report calculations.

## Опасные зоны

- `issue_key` must be stable enough to deduplicate, but specific enough not to merge different participants/accounts.
- `SET_NULL` FKs plus denormalized display fields are safer than cascade deletion for audit history.
- Partial unique constraints are not needed for first version if `issue_key` is globally unique and reopened in-place.
- Do not put real personal data into payload beyond IDs and minimal diagnostic facts already visible to authorized admins.
- Do not make dashboard run the command synchronously on GET.

## Выполнение 2026-07-15: reader switch dashboard/work queue

Выполнен UI-reader срез `financial-integrity-cache-reader`.

- `operations/views/dashboard.py` больше не вызывает синхронный `audit_appointments()` на GET dashboard/work queue.
- Dashboard/work queue читают persisted active findings из `FinancialIntegrityFinding` со статусами `open` и `acknowledged`.
- `resolved` и `ignored` finding-и не поднимают счетчики и не появляются в очереди работ.
- Work queue показывает denormalized snapshot finding-а: message, severity/status, issue code, занятие/услугу, участника, счет/источник, ledger amount/type, `last_seen_at`, ссылки на занятие и счет, если исходные FK еще существуют.
- Запуск проверки остается в `run_financial_integrity_check()` и management command; auto-fix/backfill/triage UI не добавлялись.
- Не менялись `billing.apply_decision()`, ledger posting, payroll, grants, reports, statuses, модели и миграции.

Проверки:

- `ruff check operations/views/dashboard.py operations/tests/test_views.py` прошел;
- `pytest operations/tests/test_views.py::WorkQueueViewTests -q` прошел (`17 passed`);
- `manage.py check --settings=rehab_center.settings_test` прошел;
- `manage.py makemigrations --check --dry-run --settings=rehab_center.settings_test` показал `No changes detected`;
- `pytest operations/tests/test_views.py -q` прошел (`235 passed`, 1 прежнее предупреждение django-tasks);
- полный `pytest -q --tb=short` прошел (`468 passed`, 1 прежнее предупреждение django-tasks);
- Playwright Browser QA fallback прошел на desktop 1280x900 и mobile 390x900: dashboard metric/focus link, work queue section, issue code, service context, timestamp, appointment/account links, warning style, no console/page/request errors, no horizontal overflow. Артефакты: `%TEMP%\rmcodex-browser-qa-financial-integrity-cache-reader`; QA data очищены, runserver на 8064 остановлен.
- `graphify update . --no-cluster` обновил code-index до `4112` nodes / `14639` edges; semantic extraction не запускалась.

## Parallel agents

До утверждения контракта работают только read-only reviewers. После утверждения:

- один DB owner owns `operations/models.py`, migration chain, runner service and schema tests;
- отдельный UI agent may work later on persisted-reader templates only after DB owner has committed schema/runner;
- no concurrent edits to `operations/models.py`, migrations, `billing.py`, payroll/grant semantics.

## Open questions

1. Нужен ли event table сразу, или достаточно `triage_note/triaged_by/triaged_at` в finding для первого production среза?
2. Кто имеет право ставить `ignored`: только руководитель или администратор тоже?
3. Должен ли `ignored` снова открываться автоматически, если finding seen again after source data changed, или оставаться ignored до ручного reopen?
4. Какой schedule нужен в production: вручную, каждый час, ежедневно ночью, после каждого billing action?
5. Нужен ли отдельный manager report по financial integrity trends в этом же эпике или позже?
