# Контракт атомарных цепочек переноса расписания

Дата: 2026-07-02

Статус: контракт принят как направление. Срез 1 "Схема и read-only цепочка"
реализован 2026-07-02 через миграцию `operations.0021_reschedule_chains`.
Применение цепочки, построение цепочки сервисом и UX цепочки еще не начинались.

## Зачем нужен документ

Текущие `AppointmentRescheduleStep` уже позволяют хранить варианты переноса,
согласовывать один вариант и применять один шаг. После применения одного шага
остальные нетерминальные шаги того же исходного занятия закрываются как
`skipped`, потому что сейчас это альтернативы.

Настоящая цепочка - другой доменный объект. Пример: занятие A нужно поставить
в окно B, занятие B - в свободное окно C, затем A можно поставить в старое
окно B. Это не набор альтернатив, а зависимый порядок действий. Такой порядок
нельзя выводить только из `position`, потому что `position` сейчас используется
для сортировки предложений.

## Глобальный статус проекта

- Базовая доменная модель после интервью уже включает групповые занятия,
  участников занятия, несколько специалистов, лимиты кабинетов, представителей,
  грантовые квоты, зарплатные правила и preview-импорт.
- Stage 5 UX/UI табличная стабилизация закрыта на уровне основных рабочих
  таблиц и мобильных карточек.
- Блок persisted-планов переноса уже умеет: создавать план для занятия,
  создавать план отсутствия специалиста, отправлять согласования по шагу,
  применять одиночный валидный шаг, разрешать кабинетный override, закрывать
  `review_conflict` после ручного решения, показывать реестр контроля и
  периодные метрики.
- Следующая крупная зона - атомарные цепочки переноса нескольких занятий без
  случайного нарушения расписания.

## Локальный статус текущего блока

Сделано:

- `AppointmentReschedulePlan` и `AppointmentRescheduleStep` существуют.
- `AppointmentConfirmation.reschedule_step` связывает согласования с шагом.
- `confirmation_status` и `confirmation_summary` на шаге пересчитываются сервисом.
- `apply_step()` работает в транзакции, повторно валидирует расписание, не
  принимает решение по списанию и не меняет ledger/payroll.
- После успешного `apply_step()` альтернативы того же `source_appointment`
  получают `skipped`.

Не сделано:

- Нет явной сущности цепочки.
- Нет зависимостей между шагами.
- Нет топологического порядка применения.
- Нет atomic all-or-nothing применения нескольких шагов.
- Нет UI, который отличает "варианты" от "цепочки".
- Нет валидации циклов и цепочек без свободного буферного окна.

## Термины

- Альтернатива: несколько `move` шагов для одного `source_appointment`, из
  которых администратор выбирает один.
- Цепочка: несколько шагов для разных исходных занятий, где один шаг освобождает
  окно для другого.
- Предшественник: шаг, который должен примениться раньше.
- Последователь: шаг, который можно применить только после предшественника.
- Буферное окно: реально свободное окно, в которое можно перенести последнее
  занятие цепочки, чтобы освободить следующий слот.

## Рекомендуемая модель БД

### `AppointmentRescheduleChain`

Новая таблица. Хранит одну применяемую цепочку внутри плана.

Поля:

- `plan` FK -> `AppointmentReschedulePlan`, `CASCADE`, indexed.
- `title` `CharField(200)`, blank.
- `status`: `draft`, `ready`, `stale`, `applying`, `applied`, `failed`,
  `cancelled`.
- `apply_policy`: сначала только `atomic_all_or_nothing`.
- `created_by` FK -> `User`, nullable, `SET_NULL`.
- `applied_by` FK -> `User`, nullable, `SET_NULL`.
- `applied_at` nullable datetime.
- `validation_summary` JSONField default dict.
- `admin_note` text blank.
- стандартные `created_at`, `updated_at`.

Индексы:

- `(plan, status)`;
- `(status, updated_at)`.

Ограничения:

- `applied_at` допускается только для `applied`/`failed` на уровне service
  validation; DB-level check можно добавить позже, если не усложнит миграцию.

### Изменения `AppointmentRescheduleStep`

Добавить nullable поля, чтобы существующие шаги остались совместимыми:

- `chain` nullable FK -> `AppointmentRescheduleChain`, `SET_NULL`,
  related_name `steps`.
- `chain_position` nullable `PositiveIntegerField`.
- `chain_required` boolean default `False`.

Индексы:

- `(chain, chain_position)`;
- `(chain, status)`.

Ограничения:

- Unique `(chain, chain_position)` только когда `chain IS NOT NULL`.
- На первом срезе не делать `chain` обязательным: старые и альтернативные шаги
  остаются без цепочки.

### `AppointmentRescheduleStepDependency`

Новая таблица. Хранит directed edge между шагами.

Поля:

- `plan` FK -> `AppointmentReschedulePlan`, `CASCADE`, indexed.
- `chain` FK -> `AppointmentRescheduleChain`, `CASCADE`, indexed.
- `predecessor_step` FK -> `AppointmentRescheduleStep`, `CASCADE`,
  related_name `unlocks_successors`.
- `successor_step` FK -> `AppointmentRescheduleStep`, `CASCADE`,
  related_name `dependency_edges`.
- `relation_type`: `frees_target_slot`, `must_apply_before`.
- `reason` text blank.
- `snapshot` JSONField default dict.
- стандартные `created_at`, `updated_at`.

Индексы:

- `(chain, successor_step)`;
- `(chain, predecessor_step)`;
- `(relation_type, chain)`.

Ограничения:

- unique `(chain, predecessor_step, successor_step, relation_type)`;
- check `predecessor_step != successor_step`;
- service validation обязана проверять, что `plan`, `chain`,
  `predecessor_step.plan` и `successor_step.plan` совпадают. Это лучше держать
  в сервисе, потому что БД check не умеет надежно сравнивать связанные строки.

## Почему не только `position`

`position` уже означает порядок показа предложений. Если использовать его как
порядок применения, система снова смешает альтернативы и зависимые шаги.

Для цепочек нужен отдельный порядок:

- `chain_position` - порядок отображения внутри цепочки;
- dependencies - фактический directed graph применения;
- topological order - порядок, в котором сервис применяет шаги в транзакции.

## Правила применения цепочки

Сервис `apply_chain(chain, *, actor)` должен:

1. Работать в `transaction.atomic()`.
2. Брать `select_for_update()` по chain, plan, steps и всем исходным занятиям.
3. Запретить применение, если chain не `ready`.
4. Пересчитать статусы согласования каждого `move` шага.
5. Запретить применение, если есть `waiting`, `declined`, `stale`, `failed`
   или не-`move` шаги в обязательной цепочке.
6. Проверить directed graph на cycle.
7. Построить topological order.
8. Применять шаги в порядке, где предшественник сначала освобождает слот.
9. Использовать существующий `AppointmentMoveForm` или общий helper, чтобы не
   раздвоить правила участников, специалистов, кабинетов и override.
10. Если любой шаг падает, откатить всю транзакцию и поставить chain `failed`
    только если это можно сделать вне rollback-блока отдельной записью ошибки.
11. Не менять ledger/payroll и не принимать решение о списании.

## Ограничение первого среза

Первый кодовый срез по этому контракту должен поддерживать только ациклические
цепочки с буферным свободным окном. Обмен местами A<->B без свободного буфера
не делать в первом срезе: он требует другого алгоритма временного освобождения
слотов и несет высокий риск повреждения расписания.

## Вертикальные срезы реализации

### Срез 1. Схема и read-only цепочка

Статус: выполнено 2026-07-02.

Acceptance criteria:

- миграция добавляет `AppointmentRescheduleChain`,
  `AppointmentRescheduleStepDependency` и nullable поля на step;
- существующие планы и шаги продолжают работать без backfill;
- admin показывает chain/dependency только для проверки;
- `makemigrations --check --dry-run`, `manage.py check`, focused tests и полный
  pytest проходят.

Факт реализации:

- добавлены модели `AppointmentRescheduleChain` и
  `AppointmentRescheduleStepDependency`;
- на `AppointmentRescheduleStep` добавлены nullable `chain`,
  `chain_position`, `chain_required`;
- добавлены admin registration и auditlog registration;
- добавлены focused tests на optional chain-поля, dependency validation и
  auditlog registration;
- полный pytest после реализации: `393 passed`.

### Срез 2. Построение цепочки без применения

Статус: следующий безопасный срез.

Acceptance criteria:

- сервис умеет создать chain из набора шагов и зависимостей;
- сервис проверяет совпадение plan/chain у всех edges;
- сервис отклоняет cycle и зависимость внутри одного `source_appointment`,
  если это альтернатива, а не цепочка;
- UI detail показывает блок "Цепочка" отдельно от альтернативных шагов.

### Срез 3. Перепроверка chain

Acceptance criteria:

- `revalidate_chain(chain)` проверяет все шаги и dependencies;
- при новом конфликте chain становится `stale`;
- согласования `waiting/declined` блокируют готовность chain;
- тесты покрывают занятую буферную ячейку и отказ согласования.

### Срез 4. Atomic apply chain

Acceptance criteria:

- `apply_chain()` применяет несколько шагов в одной транзакции;
- при ошибке на любом шаге ни одно занятие не остается частично перенесенным;
- альтернативы примененных исходных занятий закрываются `skipped`;
- ledger/payroll/списания остаются неизменными;
- Browser QA проверяет detail chain на desktop/mobile.

### Срез 5. UX руководителя и администратора

Acceptance criteria:

- реестр показывает отдельный маркер "Цепочка";
- detail показывает порядок, зависимости, блокировки и причину stale;
- администратор видит одну основную кнопку применения цепочки только когда
  chain `ready`;
- руководитель видит количество готовых/устаревших/проваленных цепочек в
  периодных метриках.

## Риски

- Частичный перенос нескольких занятий опаснее одиночного шага. Нужна строгая
  транзакция и focused tests на rollback.
- Нельзя параллельно менять `operations/models.py`, migration chain и
  `operations/services/rescheduling_plans.py` несколькими агентами.
- Цепочки без буферного окна не реализовывать до отдельного контракта.
- Согласования цепочки могут конфликтовать с согласованиями отдельных шагов:
  источник истины должен остаться на step, chain только агрегирует readiness.

## Параллельная работа

До завершения среза 1 работает один владелец БД/миграций.

Можно параллельно:

- read-only reviewer контракта;
- UX-черновик detail chain без правки кода;
- документация production/backup.

Нельзя параллельно:

- двум агентам менять `operations/models.py`;
- двум агентам менять `operations/migrations/*`;
- агенту UI менять `rescheduling_plans.py` до фиксации сервисного контракта.
