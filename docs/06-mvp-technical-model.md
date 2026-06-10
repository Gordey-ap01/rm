# MVP technical model

Дата: 2026-05-14

Этот документ описывает минимальную техническую модель для первого Django/PostgreSQL каркаса. Названия моделей предварительные.

## Core справочники

### Child

Получатель/благополучатель.

Поля MVP:

- `last_name`;
- `first_name`;
- `middle_name`;
- `birth_date`;
- `status`;
- `notes`;
- `primary_parent`.

### ParentGuardian

Представитель или опекун.

Поля MVP:

- `last_name`;
- `first_name`;
- `middle_name`;
- `phone`;
- `phone_alt`;
- `email`;
- `relationship_type`;
- `notes`.

### StaffMember

Специалист.

Поля MVP:

- `full_name`;
- `specializations`;
- `phone`;
- `email`;
- `status`;
- `color`;
- `can_use_mobile`.

### Service

Услуга или направление занятия.

Поля MVP:

- `name`;
- `code`;
- `category`;
- `default_duration_minutes`;
- `default_price`;
- `is_active`;
- `color`.

### Room

Помещение.

Поля MVP:

- `name`;
- `room_type`;
- `capacity`;
- `is_active`;
- `color`.

## Schedule

### Appointment

Конкретное занятие или бронь.

Поля MVP:

- `child`;
- `staff_member`;
- `service`;
- `room`;
- `starts_at`;
- `ends_at`;
- `status`;
- `attendance_status`;
- `billing_decision`;
- `billing_account`;
- `source_appointment`;
- `series`;
- `admin_note`;
- `specialist_note`;

Статусы MVP:

- `draft`;
- `proposed`;
- `confirmed`;
- `completed`;
- `cancelled`;
- `no_show`;
- `rescheduled`;
- `reserved`.

`billing_decision`:

- `undecided`;
- `charge`;
- `do_not_charge`.

Критичные ограничения:

- активные занятия не пересекаются по получателю;
- активные занятия не пересекаются по специалисту;
- активные занятия не пересекаются по помещению;
- отмененные и черновики не блокируют слот.

### AppointmentSeries

Серия занятий.

Поля MVP:

- `child`;
- `service`;
- `staff_member`;
- `room`;
- `start_date`;
- `end_date`;
- `days_of_week`;
- `time`;
- `duration_minutes`;
- `status`;

## Balance accounts

### FundingSource

Источник финансирования.

Поля MVP:

- `name`;
- `source_type`: personal, grant, sponsor, charity_fund, test;
- `starts_on`;
- `ends_on`;
- `transfer_policy`;
- `notes`.

### BalanceAccount

Счет баланса получателя.

Поля MVP:

- `child`;
- `funding_source`;
- `unit`: sessions или money;
- `service_scope`: any или specific_service;
- `service`;
- `initial_amount`;
- `valid_from`;
- `valid_until`;
- `status`;
- `notes`.

Правило:

- если `service_scope = any`, счет можно предложить для любой услуги;
- если `service_scope = specific_service`, счет можно предложить только для выбранной услуги;
- остаток считается из `LedgerEntry`.

### LedgerEntry

Операция по счету.

Поля MVP:

- `account`;
- `entry_type`: credit, debit, correction;
- `amount`;
- `appointment`;
- `created_by`;
- `reason`;
- `created_at`;

Для счетов в занятиях `amount` измеряется в занятиях. Для счетов в рублях `amount` измеряется в рублях. Списания хранятся отрицательным значением или типом `debit`; конкретный способ выбрать при реализации.

## Specialist mobile

Минимальные экраны:

1. Сегодня: календарь/лента занятий.
2. Неделя: компактный календарь.
3. Занятие: детали, кнопки `проведено`, `не проведено`, заметка.
4. Сводка: отработанные занятия за период.

Важно: отметка специалиста не списывает баланс напрямую. Она создает факт для проверки администратором.

## Director dashboard

Минимальные блоки:

- проведенные занятия за период;
- загрузка по специалистам;
- остатки по счетам баланса;
- получатели с низким остатком;
- спорные занятия без решения списания;
- занятия, ожидающие переноса.

## Test data

Для первого запуска seed-данные:

- 5 получателей;
- 5 родителей;
- 4 специалиста;
- 8 услуг;
- 5 помещений;
- 3 источника финансирования;
- 8 счетов баланса;
- 2 недели расписания;
- несколько отмен и переносов.
