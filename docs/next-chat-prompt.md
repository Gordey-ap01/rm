# Промпт для следующего чата по проекту "Радость моя"

Скопировать этот текст в новый чат, если работа продолжится отдельно.

```text
Продолжаем проект "Радость моя" в репозитории D:\РадостьМояАвтоматизация\RMcodex.

Цель: не срочный MVP, а серьезный рабочий продукт для реабилитационного центра: расписание, финансы, программы занятий, гранты, табели, кабинет специалиста и отчеты руководителя.

Текущий этап на 2026-07-15:
- Stage 5 табличной UX/UI стабилизации закрыт.
- Базовая модель групповых занятий уже есть: AppointmentParticipant, AppointmentStaffAssignment, кабинетные лимиты, представители, participant-level списания, табель и часть грантов/payroll.
- Persisted-планы переноса расписания и цепочки переноса достаточно закрыты для текущей фазы: plan/step/confirmation, staff_absence plan, review_conflict handling, room override on apply, chain schema/build/revalidate/apply, registry/work queue/dashboard attention, terminal action locks/history copy.
- После аудита зафиксирован reschedule-loop: больше не брать очередной non-DB UX/control микросрез вокруг /reschedule-plans/, chain/step anchors, архивных подсказок, registry/work queue/dashboard сигналов без конкретного бага, требования пользователя или нового утвержденного контракта.
- Новый фокус: docs/13-schedule-capacity-v2-contract.md и первый кодовый срез schedule-capacity-validation-source.
- Foundation первого schedule-capacity среза уже сделан: добавлен operations/schedule_validation.py, а forms/services/view helpers переключены на него без миграций; добавлены ScheduleValidationTests; полный pytest прошел: 437 passed.
- Model-level validation уже продолжена: Appointment.clean() проверяет существующие snapshot-участники и snapshot-назначения специалистов как источник правды; полный pytest прошел: 440 passed.
- Calendar drag-and-drop/API уже продолжен: operations/api.py::move_conflict_messages() использует operations.schedule_validation.appointment_validation_conflicts(); API проверяет snapshot-участников/специалистов, legacy fallback, доступность специалистов, лимиты кабинета и запрет групп; полный pytest прошел: 441 passed; Graphify code-index: 3953 nodes / 14224 edges. Browser QA не выполнена, потому что callable Browser tool в сессии не открылся.
- Подсказки свободных окон уже продолжены: operations/services/scheduling.py::find_overlaps() и find_free_slots() используют общий appointment_group_conflicts() для групповых получателей, нескольких специалистов, кабинетных лимитов и запрета групп; массовый перенос по отсутствию специалиста подбирает кандидатов через обновленный find_free_slots(); полный pytest прошел: 444 passed; Graphify code-index: 3961 nodes / 14242 edges.
- Первый backend/service/API срез schedule-capacity-validation-source закрыт acceptance review 2026-07-14: общий schedule validation подключен к form/model/API/free-slots/manual-move/shift-helper слоям без миграций и без изменений ledger/payroll/grants/statuses.
- Первый срез по docs/14-financial-fact-source-contract.md выполнен 2026-07-15: добавлен operations/services/financial_facts.py, payroll/reports используют общий AppointmentChargeFact для факта "занятие списано"; полный pytest прошел: 449 passed; Graphify code-index: 3988 nodes / 14263 edges.
- Первый срез по docs/15-financial-integrity-audit-contract.md выполнен 2026-07-15: добавлен operations/services/financial_integrity.py с read-only audit_appointments() и issue codes для charge/participant/ledger расхождений; полный pytest прошел: 455 passed; Graphify code-index: 4026 nodes / 14410 edges.
- Первый UI/operations срез по docs/16-financial-integrity-surfacing-contract.md выполнен 2026-07-15: dashboard показывает financial integrity metric/focus-card и учитывает issue-ы в priority_total; work queue имеет summary item "Финансовый контроль" и section #queue-financial-integrity; полный pytest прошел: 457 passed; Playwright Browser QA desktop/mobile прошел; Graphify code-index: 4052 nodes / 14461 edges.
- Добавлен docs/17-financial-integrity-cache-and-triage-contract.md: DB-backed контракт для persisted financial integrity findings, check runs, triage statuses and runner.
- Helper-only foundation по docs/17 выполнен: `financial_integrity_issue_key(issue)` строит stable SHA-256 fingerprint для future persisted finding dedupe; service tests прошли: 130 passed; полный pytest прошел: 460 passed; Graphify code-index: 4076 nodes / 14497 edges.
- DB-backed schema/runner по docs/17 выполнен: добавлены FinancialIntegrityCheckRun/FinancialIntegrityFinding, migration operations.0022, service `financial_integrity_checks.run_financial_integrity_check()`; service tests прошли: 135 passed; полный pytest прошел: 465 passed; Graphify code-index: 4102 nodes / 14589 edges. Dashboard/work queue еще читают синхронный audit, reader switch не начат.
- Management command `run_financial_integrity_check` добавлена: запускает persisted financial integrity runner and prints summary counts; service tests прошли: 136 passed; полный pytest прошел: 466 passed; Graphify code-index: 4109 nodes / 14604 edges.
- Financial-integrity cached reader выполнен: dashboard/work queue читают persisted active FinancialIntegrityFinding (`open`/`acknowledged`) вместо live `audit_appointments()` на GET; `resolved`/`ignored` скрыты. WorkQueueViewTests прошли: 17 passed; view tests: 235 passed; полный pytest: 468 passed; Playwright desktop/mobile Browser QA прошел; Graphify code-index: 4112 nodes / 14639 edges.
- Добавлен docs/18-financial-integrity-triage-and-runner-contract.md: следующий контракт для triage actions, finding detail, auditlog/event choice and scheduled/manual runner policy. Код/миграции в этом docs-only срезе не менялись.
- Financial-integrity audit/admin visibility выполнен: FinancialIntegrityCheckRun/Finding зарегистрированы в auditlog registry and Django admin; auditlog tests прошли: 6 passed; полный pytest: 470 passed; миграций нет; Graphify code-index: 4137 nodes / 14844 edges.
- Financial-integrity triage service выполнен: добавлен operations/services/financial_integrity_triage.py с actions acknowledge/return_to_open/ignore/reopen; service tests прошли: 144 passed; полный pytest: 478 passed; миграций нет; Graphify code-index: 4161 nodes / 14938 edges.
- Financial-integrity work queue triage actions выполнен: `/work-queue/#queue-financial-integrity` имеет POST actions `Принять`, `Вернуть`, `Игнорировать` через triage service, CSRF/staff/safe-next checks; WorkQueueViewTests прошли: 23 passed; полный pytest: 484 passed; Playwright desktop/mobile Browser QA прошел; миграций нет; Graphify code-index: 4171 nodes / 14961 edges.
- Financial-integrity finding detail выполнен: `/financial-integrity/findings/<id>/` показывает source links, denormalized snapshot, triage state, payload, safe actions and scoped appointment recheck; WorkQueueViewTests прошли: 27 passed; полный pytest: 488 passed; Playwright desktop/mobile Browser QA прошел; миграций нет; Graphify code-index: 4182 nodes / 15020 edges.
- Financial-integrity runner operations выполнен: `/work-queue/#queue-financial-integrity` показывает latest `FinancialIntegrityCheckRun` summary/status/counts и failed-run error message без запуска full audit on GET; full-run UI button не добавлен; production manual/cron command documented in `docs/PRODUCTION_DEPLOYMENT.md`; WorkQueueViewTests прошли: 30 passed; полный pytest: 491 passed; Playwright desktop/mobile Browser QA прошел; миграций нет; Graphify code-index: 4191 nodes / 15044 edges.
- Docs-only контракт financial-integrity history/report добавлен: `docs/19-financial-integrity-history-and-manager-report-contract.md` описывает будущий DB-owner срез `FinancialIntegrityFindingEvent`, timeline finding-а и read-only manager trend report. Код/модели/миграции не менялись; Graphify code-index: 4205 nodes / 15057 edges.

Сначала обязательно прочитай:
1. docs/project-recovery-manifest.md
2. последние датированные разделы docs/current-state.md
3. docs/12-project-stage-audit-and-pivot-plan.md
4. docs/13-schedule-capacity-v2-contract.md
5. docs/14-financial-fact-source-contract.md
6. docs/15-financial-integrity-audit-contract.md
7. docs/16-financial-integrity-surfacing-contract.md
8. docs/17-financial-integrity-cache-and-triage-contract.md
9. docs/18-financial-integrity-triage-and-runner-contract.md
10. docs/19-financial-integrity-history-and-manager-report-contract.md

Дальше читай только нужное для задачи:
- БД, расписание, финансы, гранты, табели: docs/07-updated-domain-model-after-interview.md и docs/decisions/ADR-002-balance-accounts-ledger.md
- параллельная работа агентов: docs/08-parallel-agent-execution-plan.md
- переносы, занятые окна, отсутствие специалиста и каскадные сдвиги: docs/09-cascade-reschedule-domain-slice.md
- атомарные цепочки переноса: docs/10-reschedule-chain-dependencies-contract.md
- терминальные статусы планов переноса: docs/11-plan-terminal-status-contract.md
- UX/UI: docs/03-ux-ui-and-implementation-plan.md и релевантные templates/static файлы
- стек/deploy: docs/decisions/ADR-001-django-postgresql-local-first.md и docs/PRODUCTION_DEPLOYMENT.md
- первичные требования интервью: docs/interviews/interview-director-2026-06-23.md

Если доступен Graphify и есть graphify-out/graph.json, сначала используй graphify query как индекс проекта. При расхождениях свежие docs/current-state.md, docs/07-updated-domain-model-after-interview.md, docs/12-project-stage-audit-and-pivot-plan.md, docs/13-schedule-capacity-v2-contract.md и код остаются источником правды.

Следующая задача:
Не начинать заново `dashboard-work-queue-financial-integrity-signal`, `financial-integrity-cache-schema-and-runner`, `run_financial_integrity_check`, `financial-integrity-cache-reader`, контракт docs/18, `financial-integrity-audit-admin-visibility`, `financial-integrity-triage-service`, `financial-integrity-work-queue-triage-actions`, `financial-integrity-finding-detail` или `financial-integrity-runner-operations`: они выполнены. Docs-only контракт docs/19 для `FinancialIntegrityFindingEvent`/manager trend report уже создан. Следующий безопасный шаг по этой линии: DB-owner Slice 1 из docs/19, либо возврат к более приоритетной доменной зоне после сверки `current-state`. Не менять billing.apply_decision semantics, payroll/grant semantics, статусы appointment/payment, event table или auto-fix/backfill вне docs/19 и без одного DB owner для миграций.

Критические правила:
- Не продолжать reschedule UX/control микросрезы без конкретного бага или нового требования.
- Не править БД, финансы, ledger, payroll, гранты или статусы без свежего контракта.
- Один агент владеет operations/models.py и migration chain.
- До первого schedule-capacity кодового среза нескольким агентам нельзя одновременно менять operations/forms.py и operations/services/scheduling.py.
- Делать изменения вертикальными срезами: валидация + UI/API integration + тесты + recovery docs.
- Не коммитить секреты, production-конфиги, реальные персональные данные и реальные Excel-выгрузки.
- При контрольной точке обновлять только изменившиеся разделы docs/current-state.md, docs/project-recovery-manifest.md и этот файл.
```

## Как использовать

1. Начать новый чат в этом же проекте.
2. Вставить текст из блока выше.
3. Не прикладывать полную копию проекта: модель должна читать манифест и только нужные документы.
