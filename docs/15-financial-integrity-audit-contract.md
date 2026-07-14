# Контракт среза: financial-integrity-audit

Дата: 2026-07-15

Статус: первый read-only кодовый срез `financial-integrity-audit-source` выполнен 2026-07-15 без миграций.

Назначение: после появления общего `AppointmentChargeFact` дать администратору и будущим агентам безопасный способ находить финансовые расхождения до любых автоисправлений, DB constraints, импорта Excel или изменений `billing.apply_decision()`.

## Почему это следующий срез

Финансовый факт списания теперь считается единообразно для payroll, табеля и части грантовых отчетов. Следующий риск - старые или ручные данные, где состояние может быть внутренне противоречивым:

- `billing_decision=charge` есть, но debit `LedgerEntry` отсутствует;
- participant помечен как `charge`, но не имеет `billing_account`;
- legacy `Appointment.billing_decision=charge` противоречит snapshot-участникам;
- debit ledger остается привязанным к занятию, которое больше не считается списанным;
- ledger привязан к participant/account, которые не совпадают по получателю или занятию;
- mixed funding группа корректна для payroll, но должна быть явно видима как не имеющая единого funding source.

Перед миграциями и импортом нельзя чинить такие данные вслепую. Нужен read-only audit source, который сначала покажет проблему и даст тестируемый контракт.

## Текущее состояние

Уже есть:

- `operations/services/financial_facts.py::appointment_charge_fact()` как единый read-only факт списания;
- `AppointmentParticipant.clean()` и `LedgerEntry.clean()` с валидацией новых записей;
- `billing.apply_decision()` как штатный путь создания debit ledger;
- balance/grant/payroll reports, которые зависят от ledger и charge facts;
- тесты на legacy fallback, mixed funding, missing debit ledger в helper-е.

Главный пробел: нет сервиса, который агрегирует финансовые нарушения в понятный список issue-ов и может использоваться в management report, dashboard/work queue или future migration preflight.

## Инварианты

1. Audit-срез read-only: он не меняет `Appointment`, `AppointmentParticipant`, `LedgerEntry`, `BalanceAccount`, payroll sheets или ledger history.
2. Источник факта списания - `appointment_charge_fact()`; не создавать новую копию логики.
3. Issue должен иметь стабильный code, severity, human message и ссылки на appointment/participant/ledger/account, если они есть.
4. `charge + account + no debit ledger` считается warning/error в audit, но не чинится автоматически.
5. `charge + no account` считается invalid-charge issue, потому что такое списание не может быть финансовым фактом.
6. При наличии participants legacy `Appointment.billing_decision/billing_account` не создает финансовый факт, но stale legacy charge должен быть видим как cleanup issue.
7. Ledger debit, который ссылается на appointment без charge fact, должен быть видим как возможный stale ledger issue.
8. Mixed funding группа не является ошибкой сама по себе; это informational issue только если нужен единый funding source для конкретного отчета.
9. Старые approved/paid payroll sheets и ledger history не переписываются audit-срезом.
10. Любой auto-fix/backfill/constraint после audit требует отдельного миграционного контракта и одного владельца migration chain.

## Не входит в первый срез

- новые модели, поля и миграции;
- автоматическое исправление данных;
- изменение `billing.apply_decision()` semantics;
- пересчет или переписывание существующих `PayrollAccrual`, approved/paid sheets и ledger history;
- UI-редизайн dashboard, balances, grant report или timesheet;
- импорт Excel с записью в БД;
- DB constraints или data migration.

## Первый кодовый срез

Рабочее имя: `financial-integrity-audit-source`.

Цель: добавить read-only сервис финансового аудита, который строит список issue-ов на основе существующих данных и общего `AppointmentChargeFact`.

Ожидаемый состав:

- новый `operations/services/financial_integrity.py`;
- dataclass `FinancialIntegrityIssue` с полями:
  - `code`;
  - `severity`;
  - `message`;
  - `appointment`;
  - `participant`;
  - `ledger_entry`;
  - `account`;
  - `funding_source`;
- функция аудита для queryset/date range, например `audit_appointments(...)`;
- focused tests на missing debit ledger, participant charge without account, stale legacy charge with participants, stale appointment-linked debit ledger, mixed funding informational issue;
- recovery docs.

## Acceptance criteria первого среза

- Audit на корректном списании через `billing.apply_decision()` не возвращает error issue.
- Participant `billing_decision=charge` без `billing_account` возвращает стабильный issue code.
- Charge fact с account, но без debit ledger возвращает стабильный issue code.
- Занятие с participants, где legacy `Appointment.billing_decision=charge`, но participants не списаны, возвращает cleanup issue и не считается финансовым фактом.
- Debit ledger, привязанный к appointment без charge fact, возвращает stale ledger issue.
- Mixed funding group возвращает informational issue без блокирующей severity.
- Сервис не выполняет `.save()`, `.update()`, `.delete()` и не создает ledger/payroll rows.
- `pytest operations/tests/test_services.py -q`, `manage.py check`, `manage.py makemigrations --check --dry-run` проходят.

## Выполнение 2026-07-15

- Добавлен `operations/services/financial_integrity.py` с read-only `FinancialIntegrityIssue`, stable issue codes/severity и `audit_appointments()`.
- Сервис использует `appointment_charge_fact()` как источник факта списания и не создает отдельную копию финансовой логики.
- Покрыты issue-сценарии: valid charge without issue, participant charge without account, missing debit ledger, stale legacy charge with participants, stale debit ledger without charge fact, mixed funding informational issue.
- Срез не менял `operations/models.py`, migration chain, `billing.apply_decision()`, payroll/grant semantics, статусы, templates или UI.
- Проверки: touched-file Ruff прошел; `pytest operations/tests/test_services.py -q` прошел (`127 passed`); `manage.py check` прошел; `manage.py makemigrations --check --dry-run` показал `No changes detected`; полный `pytest -q` прошел (`455 passed`, 1 прежнее предупреждение django-tasks).
- Graphify code-index обновлен: `graphify update . --no-cluster` переизвлек `142/142` code files и записал `4026` nodes / `14410` edges. Semantic docs extraction не запускалась; LLM/API ключи в проектные файлы не записывать.

## Проверки

Минимум после первого среза:

```powershell
.\.venv-test\Scripts\python.exe -m ruff check operations/services/financial_integrity.py operations/tests/test_services.py
.\.venv-test\Scripts\python.exe manage.py check
.\.venv-test\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv-test\Scripts\python.exe -m pytest operations/tests/test_services.py -q
.\.venv-test\Scripts\python.exe -m pytest -q
git diff --check
```

Browser QA не требуется, пока срез не подключен к templates/views. Если issue-ы выводятся в dashboard/work queue/balances/grant report, нужен отдельный UI acceptance и Browser QA.

## Файловые границы

Разрешено:

- `operations/services/financial_integrity.py`;
- `operations/tests/test_services.py`;
- recovery docs;
- при необходимости только read-only helper import из `operations/services/financial_facts.py`.

Запрещено без отдельного контракта:

- `operations/models.py`;
- `operations/migrations/*`;
- `operations/services/billing.py` semantics;
- auto-fix/backfill;
- templates/views/UI;
- payroll/grant report semantics;
- Excel import write path.

## Параллельные агенты

Первый срез делает один ведущий агент, потому что он задает финансовый issue contract. Параллельно допустим только read-only review тестовых сценариев или документации. Никто параллельно не меняет `operations/models.py`, migration chain, `billing.py`, payroll или report UI.
