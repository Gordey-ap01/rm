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
Не начинать заново financial-integrity event/timeline/report работу, `group-payroll-policy-foundation`, `expenses-foundation-schema`, `expenses-basic-ui`, `expenses-manager-report` или `assets-registry`: они выполнены. Текущий активный контракт - `docs/21-expenses-assets-contracts-contract.md`. Следующий безопасный шаг, если продолжаем этот приоритет: `contracts-registry` отдельным DB-owner или небольшой product UI для категорий/контрагентов. Не начинать approve/pay/import write-path без отдельного контракта.

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

## Последнее уточнение 2026-07-16: assets-registry выполнен

После текста промпта выше считать актуальным:

- `assets-registry` выполнен по `docs/21-expenses-assets-contracts-contract.md`.
- Добавлены `EquipmentAsset`, migration `operations.0026_equipmentasset`, admin/auditlog registration, `EquipmentAssetForm`, `/assets/`, `/assets/new/`, `/assets/<id>/edit/`, шаблоны и навигация "Оборудование".
- Актив можно связать только с расходом покупки категории `equipment`; non-empty inventory number уникален; списание/архивирование меняет только статус и не удаляет расход покупки.
- `LedgerEntry`, `BalanceAccount`, `Payment`, payroll, billing decisions, grant semantics и статусы занятий не менялись.
- Проверки прошли: Ruff, Django check, migration dry-run `No changes detected`, focused tests `11 passed`, full pytest `528 passed`, Browser QA desktop/mobile.
- Graphify code-index after assets: `4461` nodes / `16971` edges; semantic extraction was not rerun in this slice.
- Следующий безопасный срез: `contracts-registry` одним DB-owner или небольшой product UI категорий/контрагентов. Не начинать approve/pay/import write-path до отдельного контракта.

## Последнее уточнение 2026-07-16: contracts-registry выполнен

После текста промпта выше считать актуальным:

- `contracts-registry` выполнен по `docs/21-expenses-assets-contracts-contract.md`.
- Добавлены `ContractTemplate`, `DonationContract`, `ServiceContract`, migration `operations.0027_contracttemplate_donationcontract_servicecontract`, admin/auditlog registration, формы, `/contracts/`, create/edit routes для шаблонов, договоров пожертвования и договоров с получателями, шаблоны и навигация "Договоры".
- Договоры можно создавать без файла; можно связать с шаблоном и `Document`. Договор пожертвования связан с `FundingSource`; договор с получателем связан с `Child` и `RecipientRepresentative`-подписантом.
- Валидация запрещает подписанта от другого получателя, non-signer и документ другого получателя; проверяет порядок дат, положительный лимит суммы и уникальность номера+даты внутри типа договора.
- `LedgerEntry`, `BalanceAccount`, `Payment`, payroll, billing decisions, grant semantics и статусы занятий не менялись.
- Проверки прошли: Ruff, Django check, migration dry-run `No changes detected`, focused tests `14 passed`, full pytest `540 passed`, Browser QA desktop/mobile.
- Python Playwright был установлен только в `.venv-test` для QA; это не production dependency и не проектный файл. Артефакты Browser QA: `%TEMP%\rmcodex-browser-qa-contracts-registry`; runserver `8077` остановлен.
- Graphify code-index after contracts: `4531` nodes / `18018` edges; semantic extraction was not rerun in this slice.
- Следующий безопасный срез: `contracts-generation-and-import-preview` отдельным срезом для генерации файла договора из шаблона и preview Excel-импорта без записи в БД, либо небольшой product UI справочников категорий/контрагентов. Не начинать approve/pay/import write-path с записью в БД без отдельного контракта.

## Последнее уточнение 2026-07-17: contracts-generation-and-import-preview выполнен

После текста промпта выше считать актуальным:

- `contracts-generation-and-import-preview` выполнен по `docs/21-expenses-assets-contracts-contract.md`; все 6 срезов docs/21 закрыты.
- Добавлены read-only PDF-download для договоров пожертвования и договоров с получателями; PDF строится из структурных данных и не сохраняется как `Document`.
- Добавлен `/contracts/import-preview/` для Excel/CSV/TSV preview контрагентов, расходов, договоров пожертвования и договоров с получателями без записи в БД.
- Preview проверяет колонки, даты, суммы, справочники, статусы и подписанта договора; показывает готовые строки, ошибки и предупреждения.
- Реальная генерация по Word placeholders, сохранение финального `Document`, approve/pay и import write-path с записью в БД не реализованы и требуют отдельных контрактов.
- `LedgerEntry`, `BalanceAccount`, `Payment`, payroll, billing decisions, grant semantics и статусы занятий не менялись; миграций нет.
- Проверки прошли: Ruff, Django check, migration dry-run `No changes detected`, focused tests `14 passed`, full pytest `547 passed`, Browser QA desktop/mobile с PDF download и CSV upload preview.
- Graphify code-index after slice: `4570` nodes / `18329` edges; semantic extraction was not rerun and no API key was written to project files.
- Следующее безопасное направление: новый отдельный контракт на product UI справочников категорий/контрагентов, Word-template legal generation, approve/pay или import write-path. Не начинать эти write-path без нового контракта.

## Последнее уточнение 2026-07-18: category-counterparty-directory UI выполнен

После текста промпта выше считать актуальным:

- `docs/22-category-counterparty-directory-contract.md` добавлен и выполнен.
- Добавлен product UI `/directories/expenses/` для категорий расходов и контрагентов.
- Категории можно создавать, редактировать, включать и отключать; отключение не удаляет старые расходы.
- Контрагентов можно создавать, редактировать, архивировать и восстанавливать через существующий soft-delete; старые расходы и договоры сохраняются.
- Расходы, договоры и import preview теперь ведут в этот справочник, чтобы исправлять missing category/counterparty без Django admin.
- Новых моделей/миграций нет; `LedgerEntry`, `BalanceAccount`, `Payment`, payroll, billing decisions, grant semantics и статусы занятий не менялись.
- Проверки прошли: Ruff, Django check, migration dry-run `No changes detected`, related tests `24 passed`, full pytest `553 passed`, Browser QA desktop/mobile.
- Graphify code-index after slice: `4614` nodes / `18538` edges; semantic extraction was not rerun and no API key was written to project files.
- Следующее безопасное направление: отдельный контракт на Word-template legal generation, approve/pay расходов, import write-path или расширение юридических реквизитов контрагентов.

## Последнее уточнение 2026-07-18: contract-word-generation выполнен

После текста промпта выше считать актуальным:

- `docs/23-contract-word-generation-contract.md` добавлен и выполнен.
- Добавлена runtime dependency `python-docx==1.2.0`.
- Добавлен `operations.services.contract_documents`: генерация `.docx` для `ServiceContract` и `DonationContract`, replacement placeholders в загруженном `.docx`, fallback `.docx` без файла шаблона.
- В `/contracts/` добавлены POST-кнопки `Word`; URL: `/contracts/services/<id>/word/`, `/contracts/donations/<id>/word/`.
- Договор с получателем создает или обновляет `Document(category=contract)` у получателя и привязывает его к `ServiceContract.document`.
- Договор пожертвования пока только скачивает `.docx`, без создания `Document`, потому что текущая модель `Document` требует `child`.
- Новых моделей/миграций нет; `LedgerEntry`, `BalanceAccount`, `Payment`, payroll, billing decisions, grant semantics и статусы занятий не менялись.
- Проверки прошли: Ruff, Django check, migration dry-run `No changes detected`, focused contract tests `13 passed`, related tests `24 passed`, full pytest `557 passed`, Python Playwright desktop/mobile Browser QA.
- Graphify code-index after slice: `4670` nodes / `18767` edges; semantic extraction was not rerun and no API key was written to project files.

## Последнее уточнение 2026-07-18: inventory исходных документов для шаблонов

После текста промпта выше считать актуальным:

- Добавлен `docs/24-document-template-source-inventory.md`.
- Локальная папка `docshablon/` содержит реальные исходные `.doc/.docx` для будущих шаблонов и добавлена в `.gitignore`; raw samples не коммитить, не копировать в docs и не отправлять в публичные артефакты.
- Образцы обезличенно разложены по контурам: пожертвование разовое/регулярное, договоры услуг с получателем, присмотр/уход, материнский капитал, безвозмездные услуги за счет пожертвований, B2B-договор организации в пользу получателя, фото/видео согласие.
- Главные gaps перед юридически полноценной генерацией: реквизиты центра, паспорт/адрес представителя, адрес получателя, спецификация услуг договора, связь договора с финансированием/сертификатом, сохранение документов без обязательного `Child`, snapshot подписанных реквизитов, общий шаблон для согласий.
- Код, модели, миграции, расписание, billing/ledger/payroll/grants/status semantics не менялись.
- Graphify: `graphify update .` был запущен после docs/24, но отказался перезаписать граф из-за shrink 4670 -> 4630 nodes. `--force` не использовать без отдельного решения; raw `docshablon/` не отправлялись в semantic extraction.
- Следующий безопасный кодовый срез: `template-placeholder-expansion-v2` без миграций. DB-срезы по реквизитам/документам/сертификатам/B2B делать только после отдельного контракта и одним владельцем migration chain.
- Следующее безопасное направление: отдельный контракт на модель документов для договоров пожертвования без `Child`, юридические реквизиты центра/контрагентов, approve/pay расходов или import write-path.

## Latest clarification 2026-07-18: template-placeholder-expansion-v2 complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/25-template-placeholder-expansion-v2-contract.md` is implemented.
- Contract Word template placeholders are now grouped in `operations.services.contract_documents` and shown on `ContractTemplate` create/edit pages.
- Existing service/donation `.docx` generation replaces supported v2 placeholders; future legal/spec/certificate placeholders become `_______________`.
- New uploaded contract template files must be `.docx`; local `docshablon/` legacy `.doc` samples must be converted outside the app first.
- No DB/models/migrations or ledger/balance/payment/payroll/billing/grant/schedule/status semantics changed in this slice.
- Tests/QA passed: Ruff, Django check, migration dry-run `No changes detected`, focused/related contract tests `24 passed`, full pytest `561 passed`, Python Playwright desktop/mobile QA for `/contracts/templates/new/`.
- `docshablon/` is ignored and contains sensitive source examples; never commit raw samples or send them to Graphify semantic extraction.
- Graphify was not forced after this slice; previous update refused shrink `4670 -> 4630`. Keep Graphify as an index unless a sanitized corpus update is explicitly planned.
- Next safe step: draft a separate DB-owner contract for center legal profile plus generalized document targets/signed snapshots, or take a smaller no-migration template/document UI slice. Do not start donation `Document` storage, B2B, consents, acts, approve/pay or import write-path without a new explicit contract.

## Latest clarification 2026-07-18: document-target-foundation complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/26-legal-document-targets-and-center-profile-contract.md` is added.
- Implemented migration `operations.0028_document_counterparty_document_target_type_and_more`.
- `Document` now has `target_type`, nullable `child` with `SET_NULL`, and optional `counterparty`; recipient docs still require `child`, counterparty docs require `counterparty`.
- `/documents/` and `/documents/new/` now show and create documents by target, not only by recipient.
- Service contract Word still saves recipient contract files; donation contract Word now saves/updates counterparty contract files and links `DonationContract.document`.
- PDF downloads remain read-only. No ledger/balance/payment/billing/payroll/grant/schedule/status semantics changed.
- Tests/QA passed: Ruff, Django check, migration dry-run `No changes detected`, focused document/contract tests `31 passed`, full pytest `566 passed`, Python Playwright desktop/mobile QA for documents list/form.
- Graphify code-index after this slice: `4731` nodes / `18868` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private and must not be sent to semantic extraction.
- Next safe step: `center-legal-profile-foundation` from docs/26, then signed legal snapshots. Keep B2B/consents/acts, approve/pay and import write-path separate unless a new contract explicitly combines them.

## Latest clarification 2026-07-18: center-legal-profile-foundation complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `CenterLegalProfile` is implemented by migration `operations.0029_centerlegalprofile`.
- UI route: `/center/legal-profile/`; navigation item: "Центр".
- The profile stores center legal, license, address, contact and bank requisites; only one active profile is allowed.
- `center.*` placeholders in new service/donation Word generation use the active profile. Existing generated files are not rewritten.
- No ledger/balance/payment/billing/payroll/grant/schedule/status semantics changed.
- Tests/QA passed: Ruff, Django check, migration dry-run `No changes detected`, related focused tests `43 passed`, full pytest `571 passed`, Python Playwright desktop/mobile QA for `/center/legal-profile/`.
- Graphify code-index after this slice: `4757` nodes / `19251` edges. Semantic extraction was not rerun.
- Next safe step: `contract-signed-snapshot` from docs/26. Do not start B2B/consents/acts or legally signed versions before snapshot semantics are explicit.

## Latest clarification 2026-07-18: contract-signed-snapshot complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `ContractLegalSnapshot` is implemented by migration `operations.0030_contractlegalsnapshot`.
- One generated contract `Document` has one legal snapshot. The snapshot links to exactly one `ServiceContract` or `DonationContract`, protects document/contract deletion with `PROTECT`, and stores JSON snapshots of contract, center, recipient, representative, counterparty, funding source and template values.
- Service and donation Word generation creates/updates the snapshot together with the saved `Document`; repeated Word generation updates the same snapshot for the same document.
- Generation rejects a linked `Document` that already has a legal snapshot for another contract, before rewriting the file.
- `/contracts/` shows "реквизиты зафиксированы" for files with legal snapshots.
- This is not immutable signed-file versioning yet; B2B contracts, consents, acts, signature status and signed version archives need separate contracts.
- No ledger/balance/payment/billing/payroll/grant/schedule/status semantics changed.
- Tests/QA passed: Ruff, Django check, migration dry-run `No changes detected`, focused snapshot/document/contract/audit tests `37 passed`, full pytest `574 passed`, Python Playwright desktop/mobile QA for `/contracts/` with artifacts `%TEMP%\rmcodex-browser-qa-contract-snapshots`.
- Graphify code-index after this slice: `4790` nodes / `19559` edges. Semantic extraction was not rerun. Keep `docshablon/` private/ignored and do not send raw samples to semantic extraction.
- Next safe step: draft/implement a new explicit contract for `legal-template-families` or immutable signed versions. Do not start B2B/consents/acts/approve-pay/import write-path without a new contract.

## Latest clarification 2026-07-18: legal-template-families complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/27-legal-template-families-contract.md` is added and `template-family-choice-foundation` is implemented.
- Migration `operations.0031_alter_contracttemplate_template_type` expands `ContractTemplate.TemplateType` choices while preserving existing values.
- New families: recipient free service, recipient care, recipient certificate/maternity-capital, project donation, future B2B organization service, photo/video consent and acts.
- `ContractTemplate.service_contract_template_types()` and `donation_contract_template_types()` are the shared allowlists for model validation and form querysets.
- Current `ServiceContract` accepts only recipient-service families; current `DonationContract` accepts only donation/project/sponsor families. B2B/consent/act templates are catalog-only until separate domain contracts exist.
- No ledger/balance/payment/billing/payroll/grant/schedule/status semantics changed.
- Tests/QA passed: Ruff, Django check, migration dry-run `No changes detected`, focused contract tests `33 passed`, full pytest `579 passed`, Python Playwright desktop/mobile QA for `/contracts/templates/new/` with artifacts `%TEMP%\rmcodex-browser-qa-template-families`.
- Graphify code-index after this slice: `4810` nodes / `19584` edges. Semantic extraction was not rerun.
- Next safe step: representative/child legal fields, service-contract spec/funding, or another explicit legal-document contract. Do not start B2B/consent/act generation without separate contracts.

## Latest clarification 2026-07-18: representative-child-legal-fields complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/28-representative-child-legal-fields-contract.md` is added and implemented.
- Migration `operations.0032_child_registration_address_child_residential_address_and_more` adds additive legal fields only.
- `ParentGuardian` stores passport series/number, issuing authority/date and registration address. `Child` stores registration and residential addresses.
- Forms for representatives/recipients expose the new fields; recipient detail shows recipient registration/residential addresses.
- Service-contract Word generation fills representative passport/address and recipient address placeholders, and `ContractLegalSnapshot` stores the same legal values at generation time.
- No ledger/balance/payment/billing/payroll/grant/schedule/status semantics changed.
- Tests/QA passed: Ruff, Django check, migration dry-run `No changes detected`, focused recipient/contract tests `44 passed`, full pytest `582 passed`, Python Playwright desktop/mobile QA for representative/recipient forms and recipient detail with artifacts `%TEMP%\rmcodex-browser-qa-legal-fields`.
- Graphify code-index after this slice: `4826` nodes / `19603` edges. Semantic extraction was not rerun; keep `docshablon/` private/ignored and do not send raw samples to semantic extraction.
- Next safe step: `service-contract-spec-and-funding` or `certificate-contract-link`; B2B/consent/act generation, approve/pay and import write-path need separate contracts.

## Latest clarification 2026-07-18: service-contract-spec-and-funding complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/29-service-contract-spec-and-funding-contract.md` is added and implemented.
- Migration `operations.0033_servicecontractline_servicecontract_funding_source_and_more` adds nullable `ServiceContract.funding_source` and `ServiceContractLine`.
- `ServiceContractLine` stores service, legal service name, quantity, unit, unit price, period, sort order and notes. Line amount is computed, not stored.
- Service contract create/edit saves the spec line formset atomically with the contract and ignores untouched blank extra rows with default unit.
- `/contracts/` shows funding source, spec summary and computed contract amount for service contracts.
- Word generation fills `funding_source.*`, `contract.amount`, `service_spec.rows` and first-line `service_spec.*`; `ContractLegalSnapshot` stores funding source, service lines and total amount.
- No ledger/balance/payment/billing/payroll/grant/schedule/status semantics changed.
- Tests/QA passed: Ruff, Django check, migration dry-run `No changes detected`, focused contract tests `40 passed`, full pytest `588 passed`, Python Playwright desktop/mobile QA for service contract form/list with artifacts `%TEMP%\rmcodex-browser-qa-service-contract-spec`.
- Graphify code-index after this slice: `4868` nodes / `20003` edges. Semantic extraction was not rerun; keep `docshablon/` private/ignored and do not send raw samples to semantic extraction.
- Next safe step: `certificate-contract-link`, immutable signed-file archive, or a separate B2B/consent/act contract. Do not connect service contract lines to ledger/import write-path without a new contract.

## Latest clarification 2026-07-18: certificate-contract-link complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/30-certificate-contract-link-contract.md` is added and implemented.
- Migration `operations.0034_servicecontract_certificate_and_more` adds nullable `ServiceContract.certificate` and a certificate/status index.
- `ServiceContract.clean()` rejects certificates that belong to another child.
- `ServiceContractForm` filters certificate choices by selected child on bound POST and edit forms; the field remains optional for drafts.
- `Certificate` is registered in Django admin so `ServiceContractAdmin.autocomplete_fields` can reference it safely.
- `/contracts/` shows certificate type and number for linked service contracts.
- Word generation fills `certificate.type`, `certificate.number`, `certificate.total_amount`, `certificate.remaining_amount`, `certificate.valid_from`, `certificate.valid_until`; `certificate.payer_name` remains blank fallback until payer modeling exists.
- `ContractLegalSnapshot.contract_snapshot["certificate"]` stores certificate id/type/number/amounts/dates.
- No certificate balance mutation, ledger/balance/payment/billing/payroll/grant/schedule/status semantics changed.
- Tests/QA passed: Ruff, Django check, migration dry-run `No changes detected`, focused contract tests `43 passed`, full pytest `591 passed`, Python Playwright desktop/mobile QA for service contract certificate form/list with artifacts `%TEMP%\rmcodex-browser-qa-certificate-contract-link`.
- Graphify code-index after this slice: `4885` nodes / `20203` edges. Semantic extraction was not rerun; keep `docshablon/` private/ignored and do not send raw samples to semantic extraction.
- Next safe step: immutable signed-file archive, B2B contract contract, consent/act generation contract, or certificate payer/source modeling. Do not mutate certificate остатки or ledger from contracts without a new contract.

## Latest clarification 2026-07-18: immutable-contract-signed-file-archive complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/31-immutable-contract-signed-file-archive-contract.md` is added and implemented.
- Migration `operations.0035_contractsignedfile` adds `ContractSignedFile`.
- `ContractSignedFile` stores one immutable signed archive for exactly one service/donation contract: source `Document`, archive file, original filename, content type, size, SHA-256, signed date, uploader, status and frozen snapshot copies.
- Model save prevents changes to archived contract/file/checksum/snapshot fields after creation; void status is the non-destructive correction path.
- Archive services require an existing generated `Document` with `ContractLegalSnapshot`, copy the file, compute checksum and do not create financial facts.
- `/contracts/` shows latest active archive links and POST archive actions for contracts with snapshots; `/contracts/signed-files/<id>/download/` serves the archived file.
- No ledger/balance/payment/billing/payroll/grant/certificate-balance/schedule/appointment-status semantics changed.
- Tests/QA passed: Ruff, Django check, migration dry-run `No changes detected`, focused contract tests `49 passed`, full pytest `597 passed`, in-app Browser desktop/mobile QA for service/donation archive links. In-app Browser does not support download events; download route is covered by Django test.
- Runserver `8099` stopped; synthetic `BQA-SIGNED-*` QA data cleaned.
- Graphify code-index after this slice: `4932` nodes / `20613` edges. Semantic extraction was not rerun; keep `docshablon/` private/ignored.
- Next safe step: B2B organization-service contract contract, consent template generation, legal acts, or certificate payer/source modeling. Keep approve/pay/import write-path and certificate-balance mutation under separate contracts.

## Latest clarification 2026-07-18: organization-service-contract complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/32-organization-service-contract-contract.md` is added and implemented.
- Migration `operations.0036_organizationservicecontract_and_more` adds `OrganizationServiceContract` and `OrganizationServiceContractLine`.
- `ContractLegalSnapshot` and `ContractSignedFile` support `contract_kind=organization_service` with nullable `organization_contract`; exactly-one-contract constraints remain enforced.
- B2B contracts link `Counterparty`, optional `FundingSource`, organization-service template and service specification lines. They do not require a recipient or representative.
- Word generation creates/updates counterparty `Document(category=contract)`, stores a legal snapshot and fills center/counterparty/funding/contract/service-spec placeholders.
- `/contracts/` has a separate B2B block with create/edit, Word, PDF, archive and signed archive download link.
- No ledger/balance/payment/billing/payroll/grant/certificate-balance/schedule/status/acts/import semantics changed.
- Tests/QA passed: Ruff, Django check, migration dry-run `No changes detected`, focused contract/view tests `55 passed`, full pytest `603 passed`, in-app Browser desktop/mobile QA for B2B list/snapshot/archive link. In-app Browser does not support download events; Word/download routes are covered by Django tests.
- Runserver `8100` stopped; synthetic `BQA-ORG-*` QA data cleaned.
- Graphify code-index after this slice: `4996` nodes / `21474` edges. Semantic extraction was not rerun; keep `docshablon/` private/ignored.
- Next safe step: consent template generation, legal acts, certificate payer/source modeling, or another explicit contract. Do not connect B2B contracts to payments, ledger, balances, payroll, grants, schedules or import write-path without a new contract.

## Latest clarification 2026-07-19: consent-template-generation complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/33-consent-template-generation-contract.md` is added and implemented.
- Migration `operations.0037_consent_signatory_representative_consent_template_and_more` adds optional `Consent.signatory_representative` and `Consent.template`.
- `Consent` validates same-recipient signatory, consent template allowlist, recipient consent document ownership and date order.
- `ContractTemplate.consent_template_types()` allows `consent_photo_video` and `other`.
- Word generation for consent creates/updates `Document(target_type=recipient, category=consent)`, links it to `Consent.document` and fills `center.*`, `child.*`, `representative.*`, `consent.*`.
- `/consents/` shows signatory, template, generated document link and POST `Word` action.
- No consent signed archive/snapshot, new `DocumentTemplate`, acts, schedule blocking, email/public consent flows, ledger/balance/payment/billing/payroll/grant/status/import semantics changed.
- Tests/QA passed: Ruff, Django check, migration dry-run `No changes detected`, focused legal/consent tests `26 passed`, full pytest `607 passed`, in-app Browser desktop/mobile QA for consent list and Word-triggered document link. In-app Browser does not support download events; Word download route is covered by Django tests.
- Runserver `8101` stopped; synthetic `BQAConsent*` QA data cleaned.
- Graphify code-index after this slice: `5028` nodes / `21578` edges. Semantic extraction was not rerun; keep `docshablon/` private/ignored.
- Next safe step: legal acts, signed archive/snapshot for consents, certificate payer/source modeling, or another explicit contract. Do not connect consents to schedule blocking/public permissions/finance/grants/import write-path without a new contract.

## Latest clarification 2026-07-19: contract-acts-generation complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/34-contract-acts-generation-contract.md` is added and implemented.
- Migration `operations.0038_alter_document_category_contractact` adds `Document.Category.ACT` and `ContractAct`.
- `ContractAct` links exactly one `ServiceContract` or `OrganizationServiceContract`, stores act number/date/period/amount/status/template/document and snapshot JSON fields.
- Act Word generation saves/updates `Document(category=act)` and fills `act.*`, center/contract/recipient/representative/counterparty/funding/service-spec placeholders.
- `/contracts/` has an acts block, create/edit routes and POST `Word` action.
- No signed archive for acts, consent archive, appointment linkage, ledger/balance/payment/billing/payroll/grant/status/import semantics changed.
- Verification passed: Ruff touched Python/migration, Django check, migration dry-run `No changes detected`, focused contract/view tests `62 passed`, full pytest `612 passed`, Playwright desktop/mobile QA for acts; QA data was cleaned and runserver `8102` stopped.
- Graphify code-index after this slice: `5086` nodes / `22128` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private.
- Save-point commit created: `48c491a feat: add contract act generation`; final secret scan and `git diff --check` passed before commit.

## Latest clarification 2026-07-19: act-signed-file-archive complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/35-act-signed-file-archive-contract.md` is added and implemented.
- Migration `operations.0039_contractactsignedfile` adds `ContractActSignedFile`.
- `ContractActSignedFile` stores immutable signed archive copies for generated acts: one `ContractAct`, source `Document(category=act)`, archive file, original filename, content type, size, SHA-256, signed date, uploaded user, active/void status and frozen snapshots.
- Immutable fields cannot be changed after creation; correction path is `status=void` with `void_reason`.
- `/contracts/` shows latest active signed archive links for acts and POST `Зафиксировать` after Word generation/snapshot. Download route: `/contracts/acts/signed-files/<id>/download/`.
- Existing `ContractSignedFile` for contracts was not changed.
- No appointment linkage, act payment workflow, ledger/balance/payment/billing/payroll/grant/status/schedule/import semantics changed.
- Verification passed: Ruff touched Python/migration, Django check, migration dry-run `No changes detected`, focused contract/view tests `66 passed`, full pytest `616 passed`, Playwright desktop/mobile QA for act archive UI/download.
- Browser QA synthetic `BQA-ACTARCH-*` data was cleaned and local runserver `8103` stopped.
- Graphify code-index after this slice: `5122` nodes / `22406` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private.
- Next safe work: signed archive/snapshot for consents, certificate payer/source modeling, or another explicit contract. Do not connect acts to appointments, finance, grants, payroll or import write-path without a new contract.

## Latest clarification 2026-07-19: consent-signed-file-archive complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/36-consent-signed-file-archive-contract.md` is added and implemented.
- Migration `operations.0040_consentsignedfile` adds `ConsentSignedFile`.
- `ConsentSignedFile` stores immutable signed archive copies for generated consents: one `Consent`, source `Document(category=consent, target_type=recipient)`, archive file, original filename, content type, size, SHA-256, signed date, uploaded user, active/void status and frozen consent/center/recipient/representative/template snapshots.
- Immutable fields cannot be changed after creation; correction path is `status=void` with `void_reason`.
- `/consents/` shows latest active signed archive links and POST `Зафиксировать` after Word generation. Download route: `/consents/signed-files/<id>/download/`.
- Existing `ContractSignedFile` for contracts and `ContractActSignedFile` for acts were not changed.
- No schedule blocking, public consent flow, appointment linkage, ledger/balance/payment/billing/payroll/grant/status/import semantics changed.
- Verification passed: Ruff touched Python/migration, Django check, migration dry-run `No changes detected`, focused model/view tests `34 passed`, full pytest `620 passed`, Playwright desktop/mobile QA for consent archive UI/download.
- Browser QA synthetic `BQA-CONSENTARCH*` data was cleaned and local runserver `8104` stopped.
- Graphify code-index after this slice: `5157` nodes / `22691` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private and no API key is stored in project files.
- Next safe work: certificate payer/source modeling, external signed-file upload flow, consent legal snapshot if needed, or another explicit contract. Do not connect consents to schedule blocking/public permissions/finance/grants/import write-path without a new contract.

## Latest clarification 2026-07-19: external-signed-file-upload complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/37-external-signed-file-upload-contract.md` is added and implemented.
- No new migrations were created.
- `SignedArchiveUploadForm` validates uploaded signed files: `.pdf`, `.docx`, `.jpg`, `.jpeg`, `.png`, max 15 МБ.
- Existing archive POST actions for service/donation/B2B contracts, acts and consents now support optional `signed_file`.
- No attached file means old behavior: copy current generated Word.
- Attached file means archive uploaded payload while preserving the same source generated `Document` and frozen snapshots.
- `/contracts/` and `/consents/` show compact upload controls next to `Зафиксировать`.
- Download routes return the uploaded file payload; uploaded scans do not create new `Document` rows.
- No models/migrations, archive immutability, ledger/balance/payment/billing/payroll/grant/status/schedule/import semantics changed.
- Verification passed: Ruff touched Python, Django check, migration dry-run `No changes detected`, focused upload/view tests `13 passed`, full pytest `624 passed`, Playwright desktop/mobile QA for service contract + consent upload/download.
- Browser QA synthetic `BQA-SIGNED-UPLOAD*` data was cleaned and local runserver `8105` stopped.
- Graphify code-index after this slice: `5186` nodes / `22856` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private and no API key is stored in project files.
- Next safe work: certificate payer/source modeling, consent legal snapshot if needed, or another explicit contract. Do not connect document uploads to finance/schedule/grants/public permission flows/import write-path without a new contract.

## Latest clarification 2026-07-19: certificate-payer-source complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/38-certificate-payer-source-contract.md` is added and implemented.
- Migration `operations.0041_certificate_funding_source_certificate_payer_name_and_more` adds nullable certificate payer/source fields and lookup indexes.
- `Certificate` now has nullable `funding_source`, `payer_representative`, `payer_name` plus `payer_display_name` and same-child validation for payer representative.
- Word generation fills `certificate.funding_source`, `certificate.payer_name`, `certificate.payer_relationship`; legal snapshots store certificate payer/source details.
- `/contracts/` shows the certificate payer for linked service contracts when set; Django admin shows/searches source and payer fields; `Certificate` is registered in auditlog.
- No certificate balance mutation, ledger/balance/payment/billing/payroll/grant/schedule/status/import semantics changed.
- Verification passed: Ruff touched Python and full `operations`, Django check, migration dry-run `No changes detected`, focused contract/view/auditlog tests, full pytest `626 passed`, Playwright desktop/mobile QA for `/contracts/`.
- Browser QA synthetic `BQA-CERTPAYER*` data was cleaned and local runserver `8106` stopped.
- Graphify code-index after this slice: `5206` nodes / `22885` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private and no API key is stored in project files.
- Next safe work: consent legal snapshot if needed, certificate CRUD/import preview, or another explicit contract. Do not connect certificates to balance mutation, payments, ledger, grants, schedules or import write-path without a new contract.

## Latest clarification 2026-07-19: recipient-certificate-crud complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/39-recipient-certificate-crud-contract.md` is added and implemented.
- No migrations were created.
- `CertificateForm` fixes the recipient from the route, filters payer representatives by that recipient and validates non-negative amounts, remaining amount <= total amount and date order.
- Added recipient certificate create/edit routes and re-exported views.
- Recipient detail now has a certificate create action and `recipient-certificates-table` with type, number, source, payer, balance, validity and edit action.
- No certificate balance mutation, ledger/balance/payment/billing/payroll/grant/schedule/status/import semantics changed.
- Verification passed: Ruff touched Python and full `operations`, Django check, migration dry-run `No changes detected`, focused recipient tests `6 passed`, full pytest `630 passed`, Playwright desktop/mobile QA for recipient detail/create/edit.
- Browser QA synthetic `BQA-CERTCRUD*` data was cleaned and local runserver `8107` stopped.
- Graphify code-index after this slice: `5229` nodes / `22978` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private and no API key is stored in project files.
- Next safe work: consent legal snapshot if needed, certificate import preview, or another explicit contract. Do not connect certificates to balance mutation, payments, ledger, grants, schedules or import write-path without a new contract.

## Latest clarification 2026-07-19: certificate-import-preview complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/40-certificate-import-preview-contract.md` is added and implemented.
- No migrations were created.
- `/contracts/import-preview/` now supports type `certificates` / `Сертификаты`.
- Preview maps certificate columns by Russian labels and English aliases.
- Row validation checks recipient lookup, certificate type, amounts, date order, funding source existence and payer representative ownership.
- Duplicate certificate number for a recipient is a warning.
- Preview is read-only: it does not create `Certificate` and does not mutate balances, payments, ledger, payroll, grants, schedules or statuses.
- Verification passed: Ruff touched Python and full `operations`, Django check, migration dry-run `No changes detected`, focused import-preview tests `9 passed`, full pytest `632 passed`, Playwright desktop/mobile QA for certificate CSV upload.
- Browser QA synthetic `BQA-CERTIMPORT*` data was cleaned and local runserver `8108` stopped.
- Graphify code-index after this slice: `5245` nodes / `23031` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private and no API key is stored in project files.
- Next safe work: consent legal snapshot if needed, certificate import write-path only after a separate explicit contract, or another approved vertical slice. Do not connect certificate preview to balance mutation, payments, ledger, grants, schedules or import write-path without a new contract.

## Latest clarification 2026-07-19: certificate-import-write-path contract proposed

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- `docs/41-certificate-import-write-path-contract.md` is added as a proposed DB-owner contract.
- Code, models, migrations, templates and tests were not changed in this docs-only slice.
- Contract requires a two-step persisted import flow: preview creates `ImportBatch`/`ImportBatchRow`; explicit apply creates `Certificate` rows atomically.
- The proposed first write-path must be idempotent: repeated apply on one batch must not create duplicates.
- Existing certificate with same recipient and non-empty number should be skipped, not updated or duplicated.
- Autocreating recipients, representatives, funding sources, contracts, balance accounts or payments from certificate import is out of scope.
- Any future `Certificate` DB unique constraint on number is marked potentially dangerous and separate from the first write-path.
- Graphify code-index after this docs-only slice: `5262` nodes / `23047` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private and no API key is stored in project files.

## Latest clarification 2026-07-19: import-batch-foundation complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- First DB-owner slice from `docs/41-certificate-import-write-path-contract.md` is implemented.
- Migration `operations.0042_importbatch_importbatchrow_and_more` adds `ImportBatch` and `ImportBatchRow`.
- `ImportBatch` stores kind/status, original filename, source SHA-256, uploader/apply metadata, row counters, header snapshot and error summary.
- `ImportBatchRow` stores row number/status, raw/normalized values, errors, warnings and future target reference fields.
- New import models are registered in Django admin and auditlog.
- Certificate preview now persists batch + rows only for `certificates`; other preview types remain unchanged.
- `/contracts/import-preview/` shows saved batch summary after certificate preview.
- Apply/write-path is not implemented yet: no `Certificate`, `BalanceAccount`, `Payment`, `LedgerEntry`, payroll, grants, schedules or statuses are created/changed from the file.
- Verification passed: Ruff touched Python and full `operations`, Django check, migration dry-run `No changes detected`, focused import/audit tests `9 passed`, full pytest `632 passed`, Playwright desktop/mobile QA for persisted certificate preview batch.
- Browser QA synthetic `BQA-IMPORTBATCH*` data was cleaned and local runserver `8109` stopped.
- Graphify code-index after this slice: `5282` nodes / `23504` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private and no API key is stored in project files.
- Next safe work: certificate import apply endpoint using persisted batch rows, or another explicit vertical slice. Apply must remain idempotent and must not create finance/schedule facts.

## Latest clarification 2026-07-19: certificate-import-apply complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- Second write-path slice from `docs/41-certificate-import-write-path-contract.md` is implemented without new migrations.
- `apply_certificate_import_batch()` applies only persisted certificate import batches using `transaction.atomic()` and row/batch locks.
- Batches with invalid rows are blocked; terminal batches are idempotent and do not create duplicate certificates.
- Valid rows create `Certificate` records and mark `ImportBatchRow` as `applied` with `target_model=operations.Certificate` and `target_pk`.
- Existing certificate with the same recipient and non-empty number is skipped, not updated or duplicated.
- Added POST route `/imports/batches/<id>/apply/`; admin/staff context and hold-confirm hidden `confirm_apply=1` are required.
- `/contracts/import-preview/` shows the hold-to-confirm apply button only for saved valid certificate preview batches.
- Apply does not create or mutate `BalanceAccount`, `Payment`, `LedgerEntry`, payroll, grants, schedules, appointment billing/statuses or contracts.
- Verification passed: Ruff touched Python and full `operations`, Django check, migration dry-run `No changes detected`, focused import/view tests `59 passed`, full pytest `638 passed`, Playwright desktop/mobile QA for preview + hold-to-confirm apply.
- Browser QA synthetic `BQA-CERT-APPLY-*` data was cleaned and local runserver `8110` stopped.
- Graphify code-index after this slice: `5308` nodes / `23671` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private and no API key is stored in project files.
- Next safe work: batch result/history links or a separate certificate-balance contract. Do not connect certificates to balances, payments, ledger, grants, schedules or appointment statuses without a new contract.

## Latest clarification 2026-07-19: import-batch-detail complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- Follow-up UI slice from `docs/41-certificate-import-write-path-contract.md` is implemented without new migrations.
- Added read-only `/imports/batches/<id>/` detail page with batch status/counters and row-level statuses/errors/warnings.
- Applied certificate rows resolve `target_model=operations.Certificate` + `target_pk` and link to the recipient card `#recipient-certificates`.
- Apply endpoint redirects to detail after success, validation error or missing hold confirmation.
- `/contracts/import-preview/` links saved preview batches to detail; detail keeps hold-to-confirm apply when applicable.
- No `BalanceAccount`, `Payment`, `LedgerEntry`, payroll, grants, schedules, appointment billing/statuses or contracts changed.
- Verification passed: Ruff touched Python and full `operations`, Django check, migration dry-run `No changes detected`, focused `ContractRegistryViewTests` `50 passed`, full pytest `639 passed`, Playwright desktop/mobile QA for preview -> detail -> hold apply -> certificate target link.
- Browser QA synthetic `BQA-CERT-DETAIL-*` data was hard-cleaned and local runserver `8111` stopped.
- Graphify code-index after this slice: `5320` nodes / `23709` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private and no API key is stored in project files.
- Next safe work: batch list/history page across many imports or a separate certificate-balance contract. Do not connect certificates to balances, payments, ledger, grants, schedules or appointment statuses without a new contract.

## Latest clarification 2026-07-19: import-batch-list complete

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- Follow-up UI slice from `docs/41-certificate-import-write-path-contract.md` is implemented without new migrations.
- Added read-only `/imports/batches/` list page for recent import batches.
- List shows latest 100 batches with type, file, status, row counters, applied/skipped counts, dates and links to detail.
- `/contracts/import-preview/` links to "История пакетов"; batch detail links back to "Все пакеты".
- List does not apply batches and does not mutate data.
- No `BalanceAccount`, `Payment`, `LedgerEntry`, payroll, grants, schedules, appointment billing/statuses or contracts changed.
- Verification passed: Ruff touched Python and full `operations`, Django check, migration dry-run `No changes detected`, focused `ContractRegistryViewTests` `51 passed`, full pytest `640 passed`, Playwright desktop/mobile QA for preview -> batch list -> detail.
- Browser QA synthetic batch-list data was cleaned and local runserver `8112` stopped.
- Graphify code-index after this slice: `5326` nodes / `23716` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private and no API key is stored in project files.
- Next safe work: filtering/search/pagination for long import history or a separate certificate-balance contract. Do not connect certificates to balances, payments, ledger, grants, schedules or appointment statuses without a new contract.

## Latest clarification 2026-07-19: certificate-balance-ledger contract proposed

Treat all text above as superseded by this latest checkpoint when there is a conflict.

- Added docs-only `docs/42-certificate-balance-ledger-contract.md`.
- Decision: `Certificate` is not a second financial ledger; current spendable certificate balance must be derived from linked `BalanceAccount.current_balance`.
- Proposed first DB-owner slice: nullable one-to-one `Certificate.balance_account`, same-child/money-unit/funding validation and idempotent linked-account creation service.
- Opening certificate balance should be an opening `LedgerEntry(CREDIT)` with `BalanceAccount.initial_amount=0`; no `Payment` is created.
- Appointment charging should use the existing billing path against the linked `BalanceAccount`; `Certificate.remaining_amount` is not mutated by debits.
- Deferred risks: rename `remaining_amount`, auto-backfill existing certificates, DB amount constraints without preflight and certificate-number uniqueness.
- Code/models/migrations/templates/tests were not changed in this docs-only slice.
- Graphify code-index after this slice: `5344` nodes / `23733` edges. Semantic extraction was not rerun; raw `docshablon/` remains ignored/private and no API key is stored in project files.
