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
- Financial-integrity event schema/service выполнен: добавлены `FinancialIntegrityFindingEvent`, migration `operations.0023_financialintegrityfindingevent`, admin/auditlog registration, idempotent event service, runner events `created/resolved/reopened`, triage events `acknowledged/returned_to_open/ignored/reopened` и scoped recheck event. Full pytest: 492 passed; Graphify code-index: 4222 nodes / 15254 edges; UI timeline/report еще не реализованы.

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
11. docs/20-group-payroll-policy-contract.md
12. docs/21-expenses-assets-contracts-contract.md

Дальше читай только нужное для задачи:
- БД, расписание, финансы, гранты, табели: docs/07-updated-domain-model-after-interview.md и docs/decisions/ADR-002-balance-accounts-ledger.md
- расходы центра, оборудование, контрагенты, договоры, донорская отчетность, Excel import write-path: docs/21-expenses-assets-contracts-contract.md
- параллельная работа агентов: docs/08-parallel-agent-execution-plan.md
- переносы, занятые окна, отсутствие специалиста и каскадные сдвиги: docs/09-cascade-reschedule-domain-slice.md
- атомарные цепочки переноса: docs/10-reschedule-chain-dependencies-contract.md
- терминальные статусы планов переноса: docs/11-plan-terminal-status-contract.md
- UX/UI: docs/03-ux-ui-and-implementation-plan.md и релевантные templates/static файлы
- стек/deploy: docs/decisions/ADR-001-django-postgresql-local-first.md и docs/PRODUCTION_DEPLOYMENT.md
- первичные требования интервью: docs/interviews/interview-director-2026-06-23.md

Если доступен Graphify и есть graphify-out/graph.json, сначала используй graphify query как индекс проекта. При расхождениях свежие docs/current-state.md, docs/07-updated-domain-model-after-interview.md, docs/12-project-stage-audit-and-pivot-plan.md, docs/13-schedule-capacity-v2-contract.md и код остаются источником правды.

Следующая задача:
Не начинать заново financial-integrity event/timeline/report работу, `group-payroll-policy-foundation`, `expenses-foundation-schema`, `expenses-basic-ui` или `expenses-manager-report`: они выполнены. Текущий активный контракт - `docs/21-expenses-assets-contracts-contract.md`. Следующий безопасный шаг, если продолжаем этот приоритет: `assets-registry` отдельным DB-owner или небольшой product UI для категорий/контрагентов. Не начинать approve/pay/contracts/import write-path без отдельного контракта.

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
## Последнее уточнение 2026-07-15

После текста промпта выше считать актуальным:

- `financial-integrity-detail-timeline-ui` выполнен: `/financial-integrity/findings/<id>/` показывает read-only timeline из `FinancialIntegrityFindingEvent` с event type/time/actor/status transition/note/run id.
- GET detail page не пишет events; запись событий остается только в runner/triage/scoped recheck paths.
- Проверки прошли: Ruff touched Python, focused `WorkQueueViewTests` (`30 passed`), Django check, migration dry-run `No changes detected`, full pytest (`492 passed`), Playwright Browser QA fallback desktop/mobile.
- Следующий безопасный шаг по docs/19: read-only manager trend report. Не начинать заново detail timeline UI, event schema/service, runner operations, finding detail или предыдущие financial-integrity срезы.
- Graphify code-index after financial-integrity detail timeline UI: `4229` nodes / `15275` edges; semantic extraction was not rerun.

## Последнее уточнение 2026-07-15: manager trend report закрыт

После текста промпта выше считать актуальным:

- `financial-integrity-manager-trend-report` выполнен: `/financial-integrity/report/` показывает read-only отчет руководителя по persisted `FinancialIntegrityCheckRun`, `FinancialIntegrityFinding` и `FinancialIntegrityFindingEvent`.
- Отчет поддерживает 7/30/90/custom periods, summary active/new/resolved/ignored/reopened/runs, возраст активных расхождений, code dynamics, current code/status structure, recent active findings, latest runs и ссылки на finding detail/work queue.
- Dashboard quick actions и work queue financial section ведут в отчет.
- GET отчета не запускает audit, не создает events/runs и не меняет billing/ledger/payroll/grants/status semantics.
- Проверки прошли: Ruff touched Python, focused `WorkQueueViewTests` (`33 passed`), Django check, migration dry-run `No changes detected`, full pytest (`495 passed`), Playwright Browser QA fallback desktop/mobile.
- Graphify code-index after manager report: `4236` nodes / `15294` edges; semantic extraction was not rerun.
- Docs/19 implementation line complete: event schema/service, detail timeline UI and manager trend report are all done. Следующий шаг должен быть новый контракт/новая доменная зона или конкретный bug/new requirement, а не повтор financial-integrity report/timeline/event work.

## Последнее уточнение 2026-07-15: group payroll policy contract

После текста промпта выше считать актуальным:

- Добавлен docs-only контракт `docs/20-group-payroll-policy-contract.md`.
- Это следующий безопасный payroll/DB контракт для оставшегося gap Stage 6: "для группы можно выбрать принцип начисления".
- Контракт предлагает additive поля `StaffCompensationRule.session_scope`, `group_pay_policy`, `group_fixed_amount`, snapshot-поля в `PayrollAccrual` и общий compensation helper для табеля и persisted payroll.
- Код, модели, миграции, payroll/report formulas, ledger/billing/grant/status semantics и UI пока не менялись.
- `FundingStaffAllocation.session_pay_amount` в первом срезе остается per-session override; не умножать грантовую ставку на детей группы без отдельного грантового контракта.
- Следующий безопасный срез: `group-payroll-policy-foundation` одним DB/payroll owner. Не параллелить `operations/models.py`, `operations/migrations/*`, `operations/services/payroll.py`, `operations/services/reports.py` и future shared compensation helper.
- Graphify code-index after contract: `4256` nodes / `15313` edges; semantic extraction was not rerun and no API key was written to project files.

## Последнее уточнение 2026-07-15: group payroll policy foundation выполнен

После текста промпта выше считать актуальным:

- `group-payroll-policy-foundation` выполнен по `docs/20-group-payroll-policy-contract.md`.
- `StaffCompensationRule` получил `session_scope`, `group_pay_policy`, `group_fixed_amount`.
- `PayrollAccrual` получил snapshots `session_scope_snapshot`, `group_pay_policy_snapshot`, `charged_participants_count_snapshot`, `pay_units_snapshot`.
- Добавлена migration `operations.0024_payrollaccrual_charged_participants_count_snapshot_and_more`.
- `operations.services.compensation.calculate_staff_compensation()` стал общей формулой для `reports.timesheet()` and `payroll.generate_accruals_for_staff()`.
- UI ставок показывает формат и принцип начисления в группе; расчетный лист показывает group policy snapshot and units.
- `FundingStaffAllocation.session_pay_amount` остается per-session grant override; не умножать грантовую ставку на детей группы без отдельного контракта.
- Проверки прошли: Ruff, Django check, migration dry-run, focused service/view tests, full pytest `500 passed`, Playwright Browser QA desktop/mobile. Артефакты: `%TEMP%\rmcodex-browser-qa-group-payroll-policy`; QA data очищены, runserver 8070 остановлен.
- Graphify code-index after foundation: `4275` nodes / `15347` edges; semantic extraction was not rerun and no API key was written to project files.
- Следующий шаг не должен заново делать group payroll foundation. Возможные следующие направления: отдельный контракт на grant payroll beyond per-session, управленческие отчеты payroll/grants, расходы/договоры или Excel import after model stabilization.

## Последнее уточнение 2026-07-15: expenses-assets-contracts contract

После текста промпта выше считать актуальным:

- Добавлен docs-only контракт `docs/21-expenses-assets-contracts-contract.md`.
- Контракт задает следующую финансовую зону: расходы центра, распределение расходов по источникам, оборудование, контрагенты, договоры и будущий Excel import preview.
- Важная граница: расходы центра не являются `Payment` и не должны списываться через `BalanceAccount`/`LedgerEntry` получателей.
- Предложены сущности `Counterparty`, `CenterExpenseCategory`, `CenterExpense`, `ExpenseFundingSplit`, позднее `EquipmentAsset`, `ContractTemplate`, `DonationContract`, `ServiceContract`.
- Код, модели, миграции, billing/ledger/payroll/grant/status semantics не менялись.
- Следующий безопасный кодовый шаг: `expenses-foundation-schema` одним DB-owner, additive models/migration + service validation + tests. Не параллелить `operations/models.py` и migration chain; UI расходов начинать отдельным срезом после schema commit.
- Graphify code-index after contract: `4308` nodes / `15379` edges; semantic extraction was not rerun and no API key was written to project files.

## Последнее уточнение 2026-07-15: expenses-foundation-schema выполнен

После текста промпта выше считать актуальным:

- `expenses-foundation-schema` выполнен по `docs/21-expenses-assets-contracts-contract.md`.
- Добавлены модели `Counterparty`, `CenterExpenseCategory`, `CenterExpense`, `ExpenseFundingSplit`.
- Добавлена migration `operations.0025_centerexpensecategory_counterparty_centerexpense_and_more`.
- Добавлен сервис `operations.services.expenses` для проверки суммы split-строк и готовности статусов `approved`/`paid`.
- Новые модели зарегистрированы в Django admin и auditlog; `ExpenseFundingSplit` не создает `LedgerEntry`.
- `BalanceAccount`, `LedgerEntry`, `Payment`, payroll, billing decisions и grant semantics не менялись.
- Проверки прошли: Ruff, Django check, migration dry-run `No changes detected`, focused expenses/auditlog tests `13 passed`, full pytest `507 passed`. Browser QA не требовалась, потому что product UI/templates/JS не менялись.
- Graphify code-index after foundation: `4353` nodes / `15919` edges; semantic extraction was not rerun and no API key was written to project files.
- Следующий безопасный срез: `expenses-basic-ui` или read-only manager report поверх новой схемы. Не начинать contracts/assets/import write-path до отдельного среза.

## Последнее уточнение 2026-07-15: expenses-basic-ui выполнен

После текста промпта выше считать актуальным:

- `expenses-basic-ui` выполнен по `docs/21-expenses-assets-contracts-contract.md` без изменений БД/моделей/миграций.
- Добавлены `/expenses/`, `/expenses/new/`, `/expenses/<id>/edit/`, `operations.views.expenses`, `CenterExpenseForm`, `ExpenseFundingSplitFormSet`, шаблоны списка/формы и ссылка "Расходы" в навигации.
- Черновик расхода можно создавать и редактировать; non-draft расход защищен от редактирования через этот UI.
- Split-строки проверяют duplicate `FundingSource` и показывают расхождение суммы расхода и суммы распределения.
- `LedgerEntry`, `BalanceAccount`, `Payment`, payroll, billing decisions, grant semantics и статусы занятий не менялись.
- Проверки прошли: Ruff, Django check, migration dry-run `No changes detected`, focused expense UI tests `12 passed`, full pytest `513 passed`, Browser QA desktop/mobile.
- Graphify code-index after UI: `4386` nodes / `16432` edges; semantic extraction was not rerun and no API key was written to project files.
- Следующий безопасный срез: `expenses-manager-report` read-only или небольшой product UI для категорий/контрагентов перед отчетом. Не начинать approve/pay/assets/contracts/import write-path до отдельного контракта.

## Последнее уточнение 2026-07-15: expenses-manager-report выполнен

После текста промпта выше считать актуальным:

- `expenses-manager-report` выполнен по `docs/21-expenses-assets-contracts-contract.md` без изменений БД/моделей/миграций.
- Добавлены `operations.services.expense_reports`, `/expenses/report/`, шаблон отчета, ссылка из реестра расходов.
- Отчет показывает период, категории, источники покрытия, статусы, расходы с расхождением split-сумм и строки расходов периода.
- Фильтр по `FundingSource` считает только split-долю выбранного источника, не полную сумму расхода.
- `LedgerEntry`, `BalanceAccount`, `Payment`, payroll, billing decisions, grant semantics и статусы занятий не менялись.
- Проверки прошли: Ruff, Django check, migration dry-run `No changes detected`, focused tests `18 passed`, full pytest `519 passed`, Browser QA desktop/mobile.
- Gemini/Google API key сохранен только в user-level Windows env, не в проектных файлах; точный ключ в репозитории не найден.
- Graphify code-index after report: `4421` nodes / `16565` edges; semantic extraction was not rerun in this slice.
- Следующий безопасный срез: `assets-registry` одним DB-owner или небольшой product UI категорий/контрагентов. Не начинать approve/pay/contracts/import write-path до отдельного контракта.
