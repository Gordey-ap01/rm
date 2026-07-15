# Контракт среза: group-payroll-policy

Дата: 2026-07-15

Статус: proposed. Docs-only контракт создан перед любыми изменениями БД, payroll, grant payroll, табеля или расчетных листов.

Назначение: закрыть оставшийся gap после интервью 2026-06-23: для группового занятия руководитель должен выбирать принцип начисления зарплаты специалисту. Контракт нужен до правок `operations/models.py`, migration chain, `operations/services/payroll.py`, `operations/services/reports.py`, UI ставок или тестов payroll.

## Контекст

Уже есть:

- `AppointmentParticipant` - участники группового занятия и participant-level списания;
- `AppointmentStaffAssignment` - несколько специалистов на одном занятии;
- `AppointmentChargeFact` - общий read-only факт "занятие списано";
- `StaffCompensationRule` - ставка специалиста по услуге, источнику финансирования, длительности и периоду;
- `FundingStaffAllocation.session_pay_amount` - приоритетная грантовая ставка специалиста за занятие;
- `PayrollAccrual`, `PayrollSheet`, `PayrollSheetLine` - persisted начисления и расчетные листы;
- табель специалиста показывает payable lines и сумму после решения администратора `списать`;
- persisted payroll не привязывает group accrual к произвольному участнику, если в группе списано несколько получателей.

Сейчас не закрыто:

- для группы нельзя выбрать принцип начисления;
- `reports.timesheet()` и `payroll.generate_accruals_for_staff()` отдельно считают сумму и могут разъехаться при добавлении новой логики;
- `PayrollAccrual` не хранит snapshot группового принципа и количества списанных участников;
- грантовая ставка `FundingStaffAllocation.session_pay_amount` всегда трактуется как сумма за одну group/session assignment, без явного group policy.

## Инварианты

1. Зарплатное начисление создается по назначению специалиста, а не по получателю.
2. Основание начисления появляется только после финансового факта списания: для новых данных это `AppointmentParticipant.billing_decision=charge` с `billing_account`.
3. Индивидуальное занятие сохраняет текущее поведение.
4. Групповое занятие с несколькими специалистами начисляет каждому специалисту независимо по его правилу или грантовому распределению.
5. Групповое занятие со смешанными источниками финансирования не должно случайно применять source-specific или grant allocation rate, если общий источник не определен.
6. Approved/paid `PayrollAccrual` и `PayrollSheet` нельзя переписывать при пересчете.
7. Любое новое правило должно быть audit-friendly: расчетный лист должен показывать, по какому принципу и с каким числом единиц посчитана сумма.

## Решение

Первый кодовый срез должен добавить явную group payroll policy в существующую модель ставок и вынести расчет суммы в общий service/helper, который используют и табель, и persisted payroll.

Рекомендуемый путь: расширить `StaffCompensationRule`, а не создавать отдельную таблицу на первом срезе. Причина: текущий доступ к ставкам уже строится вокруг `staff + service + funding_source + duration + period`; group policy является свойством правила начисления, а не самостоятельным бизнес-объектом.

Если в реальной эксплуатации появятся сложные договоренности по проектам, сменам или фиксированным месячным суммам, их надо проектировать отдельным контрактом. Не смешивать это с первым group/session payroll policy срезом.

## Предлагаемые изменения модели

### StaffCompensationRule

Добавить поля:

- `session_scope` - choices:
  - `all` - правило действует для индивидуальных и групповых занятий; default, сохраняет текущее поведение;
  - `individual` - только индивидуальные занятия;
  - `group` - только групповые занятия.
- `group_pay_policy` - choices:
  - `per_session` - один раз за групповое занятие/назначение специалиста; default, сохраняет текущее поведение;
  - `per_charged_participant` - умножить базовую сумму правила на число списанных участников группы;
  - `fixed_group_amount` - для группы использовать отдельную фиксированную сумму за занятие.
- `group_fixed_amount` - nullable `DecimalField`, обязателен только при `group_pay_policy=fixed_group_amount`.

Правила:

- для индивидуального занятия `group_pay_policy` не влияет;
- `per_session` с `rate_type=per_session` возвращает `amount`;
- `per_session` с `rate_type=hourly` возвращает `amount * minutes / 60`;
- `per_charged_participant` умножает результат базового расчета на `charged_participants_count`;
- `fixed_group_amount` возвращает `group_fixed_amount` один раз на назначение специалиста, независимо от `rate_type`;
- matching ставок должен учитывать `session_scope`; более специфичный `group`/`individual` приоритетнее `all` при прочих равных.

Индексы:

- существующий index `["staff_member", "service", "funding_source", "is_active"]` оставить;
- добавить или заменить индексом, где `session_scope` участвует в подборе: `["staff_member", "session_scope", "service", "funding_source", "is_active"]`.

Constraints:

- `group_fixed_amount >= 0`, если задан;
- если `group_pay_policy=fixed_group_amount`, `group_fixed_amount` должен быть `NOT NULL`;
- если `group_pay_policy != fixed_group_amount`, `group_fixed_amount` может быть `NULL`.

Миграция должна быть additive: все новые поля получают defaults, существующие правила остаются `session_scope=all`, `group_pay_policy=per_session`, `group_fixed_amount=NULL`.

### PayrollAccrual

Добавить snapshot-поля:

- `session_scope_snapshot` - строка, выбранная из matched rule или `all` для старых/грантовых случаев;
- `group_pay_policy_snapshot` - `per_session` по default;
- `charged_participants_count_snapshot` - положительное число; для индивидуальных и legacy одиночных занятий `1`;
- `pay_units_snapshot` - decimal or positive integer snapshot; для `per_session` и `fixed_group_amount` = `1`, для `per_charged_participant` = число списанных участников.

Назначение snapshot-полей: расчетный лист и audit должны объяснять сумму даже после изменения ставок.

Не backfill-ить старые начисления в миграции. Existing rows получают defaults, но их `amount` не пересчитывается.

### FundingStaffAllocation

Первый срез не добавляет отдельную group policy в `FundingStaffAllocation`.

Текущий смысл `session_pay_amount` сохраняется как "стоимость занятия специалисту" за одно занятие/назначение, то есть `per_session`.

Если руководителю нужна грантовая ставка "за каждого списанного участника группы" или фиксированная сумма за проект/месяц, это отдельный контракт: он затрагивает грантовые бюджеты, выполнение квоты и отчет для спонсора. Нельзя молча умножать `session_pay_amount` на число детей без явного требования, потому что в интервью грантовая квота описана как количество занятий специалиста.

## Общий helper расчета

Добавить service-level helper, например `operations/services/compensation.py`, который используется в обоих местах:

- `operations/services/reports.py::timesheet()`;
- `operations/services/payroll.py::generate_accruals_for_staff()`.

Вход:

- appointment;
- staff;
- start/end snapshot;
- `AppointmentChargeFact`;
- matched `StaffCompensationRule` или `FundingStaffAllocation`;
- duration minutes.

Выход dataclass:

- payable;
- rule;
- allocation;
- funding_source;
- rate_type;
- rate_amount;
- amount;
- session_scope;
- group_pay_policy;
- charged_participants_count;
- pay_units;
- rate label;
- note.

Важно: табель и persisted payroll не должны иметь разные реализации формулы.

## Первый кодовый срез

Рабочее имя: `group-payroll-policy-foundation`.

Состав:

1. Additive migration для `StaffCompensationRule` и `PayrollAccrual` snapshot fields.
2. Обновить form/admin/list UI ставок, чтобы руководитель видел session scope и group pay policy.
3. Вынести расчет в общий helper.
4. Переключить `timesheet()` и `generate_accruals_for_staff()` на helper без изменения индивидуального поведения.
5. Добавить focused tests.
6. Обновить расчетный лист/табель только минимально: показывать policy/count/units в note/rate label, без redesign.

Не входит:

- fixed project/monthly payroll по грантам;
- изменение `FundingStaffAllocation.session_pay_amount` semantics;
- изменение `billing.apply_decision()` или ledger;
- автоматическое списание/перенос при отмене;
- экспорт в бухгалтерию;
- Excel import с записью в БД;
- React/UI rewrite.

## Acceptance Criteria

Индивидуальное занятие:

- текущие tests по `StaffCompensationRule.RateType.PER_SESSION` и `HOURLY` остаются зелеными;
- group policy fields не меняют индивидуальную сумму.

Группа, `per_session`:

- группа с 2 списанными участниками и 1 специалистом дает одно начисление специалисту;
- сумма равна текущему поведению;
- `charged_participants_count_snapshot=2`, `pay_units_snapshot=1`.

Группа, `per_charged_participant`:

- группа с 2 списанными и 1 несписанным участником умножает сумму только на 2;
- `do_not_charge` и `undecided` не входят в multiplier;
- hourly rule умножает уже рассчитанную hourly amount на число списанных участников;
- note/rate label объясняет multiplier.

Группа, `fixed_group_amount`:

- сумма равна `group_fixed_amount` один раз на назначение специалиста;
- `rate_type` индивидуальной части не влияет на group fixed amount;
- валидация запрещает fixed policy без `group_fixed_amount`.

Несколько специалистов:

- каждый `AppointmentStaffAssignment` получает собственное начисление по правилу своего специалиста;
- у ассистента может быть другая policy/rate.

Смешанные источники:

- group with mixed funding still does not use source-specific rule or `FundingStaffAllocation.session_pay_amount`;
- generic `StaffCompensationRule` может применить group policy и multiplier;
- note сохраняет "смешанные источники финансирования".

Грантовая ставка:

- `FundingStaffAllocation.session_pay_amount` остается per-session override;
- group with common grant funding and allocation creates one accrual per staff assignment with amount `session_pay_amount`;
- no silent multiplication by charged participants.

Persisted payroll:

- draft accruals update idempotently;
- approved/paid accruals are locked and not rewritten;
- new snapshot fields explain generated amount.

UI:

- руководитель может создать/изменить ставку с `session_scope` and `group_pay_policy`;
- invalid fixed group policy shows form error;
- list/detail rate labels make group policy visible enough for administrator/manager.

## Проверки

Минимум после первого кодового среза:

```powershell
.\.venv-test\Scripts\python.exe -m ruff check operations
.\.venv-test\Scripts\python.exe manage.py check --settings=rehab_center.settings_test
.\.venv-test\Scripts\python.exe manage.py makemigrations --check --dry-run --settings=rehab_center.settings_test
.\.venv-test\Scripts\python.exe -m pytest operations/tests/test_services.py -q --tb=short
.\.venv-test\Scripts\python.exe -m pytest operations/tests/test_views.py::StaffCompensationRuleViewTests -q --tb=short
.\.venv-test\Scripts\python.exe -m pytest -q --tb=short
git diff --check
```

Browser QA нужен, потому что меняется UI ставок/табеля/расчетного листа:

- desktop manager rate form/list;
- mobile manager rate form/list;
- если меняется template табеля или payroll sheet, desktop/mobile smoke этих экранов.

## Риски

- `StaffCompensationRule` уже используется как общая ставка; без `session_scope` group-specific rate невозможно отделить от individual rate.
- Дублирование формулы в `reports.py` и `payroll.py` опасно: табель может показать одну сумму, а persisted payroll создать другую.
- Existing approved payroll rows должны остаться historical truth.
- Fixed group amount может быть неправильно понят как fixed monthly/project salary; первый срез трактует его только как fixed amount за одно групповое занятие.
- Грантовая per-recipient salary policy требует отдельного решения, потому что может изменить смысл освоения квоты и отчета спонсору.

## Параллельные агенты

До завершения migration/helper foundation - один DB/payroll owner.

Нельзя параллельно менять:

- `operations/models.py`;
- `operations/migrations/*`;
- `operations/services/payroll.py`;
- `operations/services/reports.py`;
- будущий `operations/services/compensation.py`.

После DB-owner commit можно параллелить:

- UI worker: form/list/rate labels/templates/browser QA;
- tests/docs worker: regression matrix and recovery docs;
- reviewer: read-only review of migration and formula parity.
