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

Сначала обязательно прочитай:
1. docs/project-recovery-manifest.md
2. последние датированные разделы docs/current-state.md
3. docs/12-project-stage-audit-and-pivot-plan.md
4. docs/13-schedule-capacity-v2-contract.md
5. docs/14-financial-fact-source-contract.md
6. docs/15-financial-integrity-audit-contract.md

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
Реализовать первый кодовый срез по docs/15-financial-integrity-audit-contract.md: financial-integrity-audit-source. По умолчанию без миграций и UI: добавить read-only operations/services/financial_integrity.py, issue dataclass/codes для charge/participant/ledger расхождений, focused service tests и recovery docs. Не менять operations/models.py, migration chain, billing.apply_decision semantics, payroll/grant semantics, статусы или UI без отдельного контракта.

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
