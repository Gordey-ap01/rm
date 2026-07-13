# Контракт терминальных статусов плана переноса

Дата: 2026-07-13

Статус: принят для срезов plan-level terminal action visibility и terminal
plan action lock.

## Зачем нужен документ

`AppointmentRescheduleChain` уже защищен от повторной проверки в собственных
терминальных статусах. У `AppointmentReschedulePlan` была асимметрия:
detail-страница показывала активные действия, а сервисы могли пересчитать или
изменить шаги уже примененного или отмененного плана.

Это статусная бизнес-логика, поэтому правило фиксируется отдельно до изменения
сервиса и UI.

## Терминальные статусы плана

Терминальными считаются:

- `AppointmentReschedulePlan.Status.APPLIED`
- `AppointmentReschedulePlan.Status.CANCELLED`

## Правила

- Терминальный план остается доступен для просмотра.
- Терминальный план нельзя перепроверять через `revalidate_plan(plan)` или
  `revalidate_chain(chain)`.
- Терминальный план нельзя менять через `apply_step(step)`, `apply_chain(chain)`,
  `create_confirmations_for_step(step)` или
  `mark_review_conflict_step_resolved(step)`.
- Попытка действия над терминальным планом должна завершаться `ValidationError`.
- Сервис не должен менять `status`, `validation_summary`, статусы шагов,
  `validation_messages`, conflict snapshots, override-флаги шагов, статусы
  цепочек, назначения согласований или занятия.
- Detail UI должен скрывать формы изменения плана, шагов и цепочек для
  терминального плана и показывать read-only пояснение.

## Не входит в срез

- Повторное открытие примененного или отмененного плана.
- Новые статусы плана.
- Изменения расписания, ledger, payroll, списаний или согласований.
- Миграции БД.
- Изменение правил `apply_step()` или `apply_chain()`.

## Acceptance criteria

- `revalidate_plan(plan)` и `revalidate_chain(chain)` отклоняют `applied` и
  `cancelled` планы без мутаций.
- `apply_step(step)`, `apply_chain(chain)`, `create_confirmations_for_step(step)`
  и `mark_review_conflict_step_resolved(step)` отклоняют терминальный план без
  мутаций.
- POST actions на detail терминального плана не мутируют план, цепочки, шаги,
  занятия или согласования.
- Detail терминального плана показывает read-only подсказки вместо кнопок
  изменения.
- Существующая перепроверка активных планов остается рабочей.
- `manage.py check`, migration dry-run и релевантные tests проходят.
