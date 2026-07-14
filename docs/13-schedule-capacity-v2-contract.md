# Контракт среза: schedule-capacity-v2

Дата: 2026-07-14

Статус: draft, готов к реализации первым кодовым срезом после отдельной проверки.

Назначение: вывести следующий этап из reschedule-loop и стабилизировать базовые правила расписания: индивидуальные и групповые занятия, несколько специалистов, вместимость кабинетов, рабочее время, отпуска/отгулы и осознанные override-решения администратора.

## Источники

- `docs/07-updated-domain-model-after-interview.md`;
- `docs/12-project-stage-audit-and-pivot-plan.md`;
- `operations/models.py`;
- `operations/forms.py`;
- `operations/services/scheduling.py`;
- `operations/views/scheduling_helpers.py`;
- `operations/views/appointments.py`;
- `templates/operations/schedule.html`;
- тесты `operations/tests/*` вокруг форм, сервисов, календаря и карточки занятия.

## Почему это следующий срез

После интервью главный продуктовый риск не в том, чтобы еще раз отполировать реестр переносов, а в надежности записи людей в расписание. Администратор должен уверенно понимать:

- можно ли поставить индивидуальное занятие;
- можно ли поставить групповое занятие;
- сколько получателей и специалистов уже находятся в кабинете;
- кто из специалистов занят, в отпуске, на отгуле или вне рабочего графика;
- какое нарушение разрешено одноразово и кто его разрешил.

Этот слой является основанием для грантов, зарплаты, табеля, программ и будущего импорта. Если расписание допускает скрытые конфликты, следующие доменные срезы будут строиться на ненадежных данных.

## Текущее состояние кода

Уже есть:

- `Room.limit_staff_count`, `Room.max_staff_count`, `Room.limit_recipient_count`, `Room.max_recipient_count`, `Room.allow_group_sessions`;
- `Appointment.session_type`, `Appointment.title`;
- `AppointmentParticipant` как строка получателя в занятии;
- `AppointmentStaffAssignment` как строка назначения специалиста;
- `AppointmentRoomOverride` с причиной и автором;
- `StaffAvailability` и `TimeOffRequest`;
- `room_usage_counts()` для подсчета snapshot-строк и legacy fallback;
- `appointment_group_conflicts()` в `operations/forms.py`;
- `find_overlaps()` и `find_free_slots()` в `operations/services/scheduling.py`;
- `AppointmentForm` и `AppointmentMoveForm`, которые уже частично проверяют группы, нескольких специалистов, лимиты кабинета и выход вне графика.

Главная проблема: правила еще не сведены в один доменный контракт. Часть проверок использует `AppointmentParticipant`/`AppointmentStaffAssignment`, часть продолжает смотреть на legacy `Appointment.child`/`Appointment.staff_member`. Это допустимо как переходный режим, но опасно без явных инвариантов и тестов.

## Инварианты среза

1. Получатель не может одновременно находиться в двух активных занятиях.
2. Специалист не может одновременно вести два индивидуальных или групповых занятия, если нет отдельного будущего правила совместного ведения одной общей групповой сессии.
3. Индивидуальное занятие блокирует выбранного специалиста на все пересекающееся время.
4. Групповое занятие допускает несколько получателей только через `AppointmentParticipant`.
5. Групповое занятие допускает нескольких специалистов только через `AppointmentStaffAssignment`.
6. Кабинет с `limit_staff_count=true` не может превысить `effective_max_staff_count`.
7. Кабинет с `limit_recipient_count=true` не может превысить `effective_max_recipient_count`.
8. Кабинет с `allow_group_sessions=false` не принимает занятие с несколькими получателями без явного override.
9. Override кабинета требует явного действия администратора, причины и записи `AppointmentRoomOverride`.
10. Выход специалиста вне графика, отпуска или отгула требует явного действия администратора и причины на уровне занятия/назначения специалиста.
11. Legacy `Appointment.child` и `Appointment.staff_member` остаются fallback только для старых записей без snapshot-строк.
12. Если snapshot-строки уже есть, они являются источником правды для конфликтов, отображения, списаний, табеля и уведомлений.

## Не входит в срез

- переработка ledger, платежей и списаний;
- зарплатные начисления и расчетные листы;
- грантовые квоты и распределения;
- программы, каскады, серии и перенумерация;
- импорт Excel;
- удаление legacy-полей `Appointment.child`, `Appointment.staff_member`, `Appointment.billing_account`;
- новый React-календарь;
- автоматическое применение новых reschedule-планов.

## Первый кодовый срез после контракта

Рабочее имя: `schedule-capacity-validation-source`.

Цель: сделать один проверяемый источник истины для конфликтов вместимости и доступности, затем подключить его к существующим формам/сервисам без миграций.

Ожидаемый состав:

- аудит и, при необходимости, перенос `appointment_group_conflicts()` из `operations/forms.py` в доменный service layer, чтобы forms/views/services не зависели от form-модуля;
- унификация сообщений по конфликтам получателей, специалистов, кабинетов и группового запрета;
- проверка, что `AppointmentForm`, `AppointmentMoveForm`, helpers свободных/занятых окон и calendar drag-and-drop используют один контракт;
- регрессии для direct model validation, где это возможно без поломки переходного legacy режима.

Кодовый срез не должен добавлять миграции, если аудит не обнаружит реальную нехватку поля или ограничения.

### Реализовано 2026-07-14

- Добавлен `operations/schedule_validation.py` как общий source-of-truth для `appointment_group_conflicts()`, `appointment_conflicts()`, `conflict_messages()`, `staff_unavailability_reason()` и `build_local_datetime()`.
- `operations/forms.py`, `operations/services/scheduling.py`, `operations/services/rescheduling_plans.py` и `operations/views/scheduling_helpers.py` переключены на общий модуль без изменения поведения.
- `Appointment.clean()` теперь использует общий schedule validation contract для существующих snapshot-занятий: `AppointmentParticipant` и `AppointmentStaffAssignment` являются источником правды, legacy-поля используются как fallback только при отсутствии snapshot-строк.
- Добавлены focused tests для helper-level и model-level проверки snapshot-участников, snapshot-специалистов, вместимости кабинета и недоступности ассистента.
- Миграции не добавлялись.

Осталось в первом кодовом срезе:

- сверить `operations/api.py::move_conflict_messages()` и calendar drag-and-drop с `operations.schedule_validation`;
- сверить подсказки свободных/занятых окон с тем же контрактом;
- не менять ledger/payroll/grants/statuses.

## Acceptance criteria первого кодового среза

- Индивидуальное занятие не сохраняется, если выбранный специалист уже занят в пересекающееся время.
- Групповое занятие не сохраняется, если любой получатель уже занят в пересекающееся время.
- Групповое занятие с несколькими специалистами не сохраняется, если любой назначенный специалист занят.
- Кабинет с лимитом одного специалиста блокирует второе пересекающееся назначение специалиста.
- Кабинет с лимитом нескольких специалистов разрешает пересечение до лимита.
- Кабинет с лимитом одного получателя блокирует групповое занятие без override.
- Кабинет с `allow_group_sessions=false` требует override для занятия с несколькими получателями.
- Override кабинета создает `AppointmentRoomOverride` с причиной и автором.
- Выход вне графика/отпуска/отгула блокируется без override и сохраняет override-причину при явном разрешении.
- Legacy-занятия без snapshot-строк продолжают проходить через fallback.
- Занятия со snapshot-строками не восстанавливают устаревших legacy-участников/специалистов в conflict checks.

## Проверки

Минимум после первого кодового среза:

```powershell
.\.venv-test\Scripts\python.exe manage.py test operations.tests.test_forms
.\.venv-test\Scripts\python.exe manage.py test operations.tests.test_services
.\.venv-test\Scripts\python.exe manage.py test operations.tests.test_views
.\.venv-test\Scripts\python.exe manage.py check
.\.venv-test\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv-test\Scripts\python.exe -m ruff check operations
.\.venv-test\Scripts\python.exe -m pytest -q
git diff --check
```

Browser QA нужна, если изменены шаблоны, calendar API, JS или видимые тексты предупреждений. Проверять desktop и mobile: создание/редактирование занятия, ручной перенос, календарь.

## Риски

- `Appointment.clean()` выполняется до создания snapshot-строк у нового занятия, поэтому прямую model validation нельзя слепо приравнять к form-level validation.
- `Appointment.save()` сейчас синхронизирует legacy и snapshot-строки; изменение порядка может повлиять на ledger, табель и мобильный кабинет.
- Слишком раннее удаление legacy fallback сломает старые записи и импортированные данные.
- Перенос helper-логики из forms в services может создать циклические импорты, если не развести зависимости аккуратно.
- DB-level exclusion constraints уже частично существуют, но не должны меняться без отдельного миграционного решения и backfill-аудита.

## Файловые границы

Разрешено в первом кодовом срезе:

- `operations/schedule_validation.py` как общий доменный helper-модуль без зависимости от forms/views;
- `operations/services/scheduling.py`;
- новый небольшой модуль в `operations/services/`, если нужен для разрыва импорта;
- `operations/forms.py`;
- `operations/views/scheduling_helpers.py`;
- `operations/views/appointments.py` только в местах, где отображаются/используются результаты проверок;
- релевантные tests.

Запрещено без отдельного решения:

- `operations/models.py`;
- `operations/migrations/*`;
- ledger/payroll/grant services;
- импорт Excel;
- массовая переработка `templates/operations/schedule.html`;
- новая frontend-архитектура или React.

Если в ходе аудита окажется, что без изменения моделей или миграций нельзя выполнить acceptance criteria, работу остановить и обновить этот контракт перед кодом.

## Параллельные агенты

До завершения первого кодового среза - один ведущий агент.

Допустимы параллельно только read-only задачи:

- reviewer проверяет контракт и список тестов;
- UX-аналитик описывает будущий сценарий администратора без правки кода;
- documentation agent обновляет recovery-файлы.

Нельзя двум агентам одновременно менять `operations/models.py`, миграции, `operations/forms.py` или `operations/services/scheduling.py`.
