# Контракт среза: financial-fact-source

Дата: 2026-07-15

Статус: draft, готов к первому refactor-only кодовому срезу без миграций.

Назначение: после стабилизации базовых правил расписания зафиксировать единый источник истины для финансового факта "занятие списано". Этот факт должен одинаково использоваться в балансе, грантовом отчете, табеле, persisted payroll и управленческих сводках.

## Почему это следующий срез

После интервью ключевой риск сместился от расписания к связке "списание -> грантовый факт -> зарплатное начисление -> отчет руководителя". Сейчас в коде уже есть participant-level списания, ledger, грантовые квоты, выделения получателям и persisted payroll, но вычисление списанного факта дублируется:

- `operations/services/billing.py` создает или отвязывает `LedgerEntry` при решении администратора;
- `operations/services/payroll.py` имеет собственный `ChargedContext` и `_charged_context()`;
- `operations/services/reports.py` имеет отдельные `_charged_funding_source()`, `_charged_funding_source_ids()` и `_billing_decision_label()`;
- `grant_report()` считает часть факта по ledger, а часть план/статусных метрик по счетам и участникам.

Такой разнобой опасен для реабилитационного центра: один и тот же групповой урок со смешанными источниками финансирования может выглядеть по-разному в табеле, payroll и грантовом отчете.

## Текущее состояние

Уже есть:

- `BalanceAccount` с единицами учета `sessions` / `money`;
- `LedgerEntry` с `appointment` и `appointment_participant`;
- `Payment` и пополнение счета через ledger;
- transfer между счетами с учетом политики `FundingSource.transfer_policy`;
- `AppointmentParticipant.billing_decision`, `billing_account`, `price_snapshot`;
- legacy `Appointment.billing_decision`, `billing_account` для старых занятий без участников;
- `GrantRecipientAllocation`, `FundingServiceQuota`, `FundingStaffAllocation`;
- `StaffCompensationRule`, `PayrollAccrual`, `PayrollSheet`, `PayrollSheetLine`;
- отчеты `timesheet()` и `grant_report()`;
- тесты для смешанных групп, grant allocation facts, payroll idempotency и legacy fallback.

Главная проблема: нет общего доменного объекта/сервиса, который отвечает на вопросы:

- какие участники фактически списаны;
- какой счет и источник финансирования стоят за списанием;
- есть ли подтверждающая debit-проводка ledger;
- можно ли считать источник финансирования единым для payroll/grant rate;
- какой legacy fallback допустим, если `AppointmentParticipant` еще нет.

## Инварианты

1. Если у занятия есть `AppointmentParticipant`, финансовым источником правды по списанию являются участники, а не legacy `Appointment.billing_decision`.
2. Legacy `Appointment.billing_decision` и `Appointment.billing_account` используются только для занятий без `AppointmentParticipant`.
3. Участник считается списанным только если `billing_decision=charge` и указан `billing_account`.
4. Списание должно иметь debit `LedgerEntry` по тому же `appointment` и `appointment_participant`, если оно создано через штатный `billing.apply_decision()`.
5. Отсутствующая ledger-проводка при `charge + account` не должна молча превращаться в другой финансовый факт: первый срез должен минимум выявлять это состояние в общем контексте.
6. Для группы с несколькими списанными участниками из одного источника финансирования можно вернуть общий `funding_source`.
7. Для группы со смешанными источниками финансирования общий `funding_source` равен `None`, чтобы grant/payroll-specific ставки не применялись случайно.
8. Payroll начисляется по назначению специалиста, но право на начисление возникает от финансового факта списания.
9. Grant report считает факт освоения по ledger/account в периоде и не должен подтягивать чужую услугу или чужой период выделения.
10. Approved/paid payroll sheets и ledger history нельзя переписывать refactor-only срезом.

## Не входит в первый срез

- новые модели и миграции;
- изменение бухгалтерской семантики `LedgerEntry`;
- удаление legacy `Appointment.billing_decision` / `billing_account`;
- автоматическое исправление старых расхождений ledger;
- расходы центра, оборудование, административная зарплата;
- импорт Excel с записью в БД;
- UI-редизайн grant/payroll экранов;
- изменение статусов занятий, reschedule plans или расписания.

## Первый кодовый срез

Рабочее имя: `financial-fact-source-foundation`.

Цель: вынести общий read-only helper/service для финансового факта списания и переключить на него payroll/timesheet/grant helper-логику без изменения поведения и без миграций.

Ожидаемый состав:

- добавить `operations/services/financial_facts.py` или близкий по смыслу модуль без зависимости от views/forms;
- описать dataclass-контекст списания, достаточный для payroll и reports:
  - `is_charged`;
  - списанные `AppointmentParticipant`;
  - единый `funding_source` или признак mixed funding;
  - debit ledger entries;
  - выбранный single participant / single ledger для случаев, где это безопасно;
  - человекочитаемая note/label;
- заменить локальные `_charged_context()` в `payroll.py` и `_charged_funding_source*()` / `_billing_decision_label()` в `reports.py` на общий helper;
- сохранить существующее поведение для mixed funding, legacy fallback и отсутствия ставки;
- добавить регрессии на parity между старым ожидаемым поведением payroll/timesheet/grant report и новым helper-ом.

## Acceptance criteria первого среза

- Payroll для одиночного списанного snapshot-участника привязывает `appointment_participant`, `ledger_entry` и `funding_source`.
- Payroll для группы со смешанными источниками не применяет grant/source-specific ставку и пишет понятную note.
- Timesheet показывает тот же billing decision label, что до refactor.
- Grant report продолжает считать debit ledger по счету, услуге и периоду выделения.
- Legacy-занятие без `AppointmentParticipant` продолжает работать через `Appointment.billing_decision` / `billing_account`.
- Занятие с `AppointmentParticipant`, но без списанных участников, не создает payroll fact даже если legacy поле устарело.
- Одно и то же занятие не дает двойной факт из legacy и participants.
- `pytest operations/tests/test_services.py -q` и `pytest operations/tests/test_views.py -q` проходят.
- `manage.py makemigrations --check --dry-run` показывает `No changes detected`.

## Проверки

Минимум после первого среза:

```powershell
.\.venv-test\Scripts\python.exe -m ruff check operations/services/financial_facts.py operations/services/payroll.py operations/services/reports.py operations/tests/test_services.py
.\.venv-test\Scripts\python.exe manage.py check
.\.venv-test\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv-test\Scripts\python.exe -m pytest operations/tests/test_services.py -q
.\.venv-test\Scripts\python.exe -m pytest operations/tests/test_views.py -q
.\.venv-test\Scripts\python.exe -m pytest -q
git diff --check
```

Browser QA не требуется для первого refactor-only среза, если templates/views не меняются. Если меняются видимые payroll/grant/timesheet экраны, нужен desktop/mobile smoke.

## Риски

- Старые данные могли содержать `billing_decision=charge` без ledger debit. Первый срез не должен сам чинить такие данные, но должен сделать состояние видимым в общем контексте.
- В группах с несколькими списанными участниками не всегда можно безопасно выбрать один `LedgerEntry` или один `AppointmentParticipant` для persisted payroll.
- Grant report использует ledger timestamps, а payroll/timesheet используют дату занятия. Это разные бизнес-вопросы; нельзя случайно свести их к одному фильтру.
- `transfer_between_accounts()` создает `LedgerEntry.EntryType.TRANSFER`, это не факт проведенного занятия и не должно попадать в payroll.
- Approved/paid payroll accruals/sheets нельзя переписывать при refactor.
- Любое изменение модели ledger/payroll требует отдельного миграционного контракта и одного владельца migration chain.

## Файловые границы

Разрешено в первом срезе:

- `operations/services/financial_facts.py`;
- `operations/services/payroll.py`;
- `operations/services/reports.py`;
- focused tests в `operations/tests/test_services.py` и при необходимости `operations/tests/test_views.py`;
- recovery docs.

Запрещено без отдельного контракта:

- `operations/models.py`;
- `operations/migrations/*`;
- изменение `billing.apply_decision()` semantics;
- изменение статусов занятий/reschedule plans;
- массовый UI redesign grant/payroll/timesheet;
- импорт Excel с записью в БД.

## Параллельные агенты

До завершения первого среза - один ведущий агент для `payroll.py`, `reports.py` и нового financial-facts module.

Параллельно допустимы только read-only задачи:

- reviewer проверяет контракт и тестовые сценарии;
- аналитик сверяет grant/payroll UX без правки кода;
- documentation agent обновляет recovery.

Нельзя двум агентам одновременно менять `operations/services/payroll.py`, `operations/services/reports.py`, `operations/models.py` или migration chain.
