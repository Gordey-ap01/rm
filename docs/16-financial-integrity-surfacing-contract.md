# Контракт среза: financial-integrity-surfacing

Дата: 2026-07-15

Статус: первый UI/operations срез выполнен 2026-07-15 без миграций.

Назначение: после read-only `financial_integrity` сервиса показать администратору и руководителю финансовые расхождения в существующем операционном контуре, не исправляя данные автоматически и не меняя финансовую семантику.

## Выполнение 2026-07-15

Выполнен кодовый срез `dashboard-work-queue-financial-integrity-signal`.

- `operations/views/dashboard.py` подключает read-only audit source `operations.services.financial_integrity` и строит candidate queryset по занятиям с charge-решениями или debit ledger-проводками.
- Dashboard показывает метрику финансовых расхождений и focus-card "Проверить финансы"; `priority_total` учитывает найденные issue-ы.
- Work queue получила summary item "Финансовый контроль" и секцию `#queue-financial-integrity` с severity, issue code/message, контекстом занятия/участника/счета/источника и ссылками на занятие/счет.
- В секции нет POST forms, auto-fix, backfill или mutation actions.
- `static/operations/app.css` получил стили `status-warning`, `status-danger`, `status-info` для визуального разделения severity.
- Lazyweb callable tools в этой сессии через `tool_search` не найдены; fallback выполнен по существующим dashboard/work queue паттернам проекта.
- Browser QA выполнен через временный Playwright fallback вне репозитория на `rehab_center.settings_test`: `/` и `/work-queue/#queue-financial-integrity` проверены на desktop 1280x900 и mobile 390x900, status 200, console/page/http errors нет, horizontal overflow нет, warning card style применен. Артефакты: `%TEMP%\rmcodex-browser-qa-financial-integrity-signal`.
- QA-синтетика `QAFinancialIntegrity*` после проверки удалена из test runtime; технический `qa_admin` оставлен как локальный QA-user.

Проверки:

- `ruff check operations/views/dashboard.py operations/tests/test_views.py` прошел;
- `manage.py check` прошел;
- `manage.py makemigrations --check --dry-run` показал `No changes detected`;
- `pytest operations/tests/test_views.py::WorkQueueViewTests -q` прошел (`15 passed`);
- полный `pytest -q --tb=short` прошел (`457 passed`, 1 прежнее предупреждение django-tasks);
- `git diff --check` показал только стандартные LF->CRLF warnings рабочей копии.
- `graphify update . --no-cluster` обновил code-index до `4052` nodes / `14461` edges; semantic extraction не запускалась, ключи в проектные файлы не записывать.

Не менялись: `operations/models.py`, migration chain, `billing.apply_decision()` semantics, payroll/grant/report calculations, статусы, production-конфиги и реальные данные.

Остаточный риск: audit сейчас считается синхронно на dashboard/work queue по всем candidate appointments с charge/debit ledger. Для большой production-истории следующий контракт должен рассмотреть cached audit snapshot/background task/pagination вместо расширения этого UI-среза.

## Lazyweb / UI research

По правилам проекта UI-срез должен сначала использовать Lazyweb. В текущей сессии `tool_search` не нашел `lazyweb_search`/`lazyweb_health`, поэтому Lazyweb database недоступна. Fallback для первого среза:

- использовать существующий UI-паттерн проекта: `dashboard_focus_items`, `work_queue_summary_items`, панели `ops-focus`, очередь задач, `status-pill`;
- не вводить новый визуальный язык, hero, card-heavy redesign или React;
- проверить результат Browser QA на desktop/mobile после изменения templates/views.

## Почему это следующий срез

`operations/services/financial_integrity.py` уже умеет находить финансовые issue-ы, но сейчас это только сервисный слой. Если issue-ы не видны в ежедневной работе, администратор продолжит принимать решения по расписанию, балансу, табелю и грантам, не замечая расхождения между `charge`, participants и ledger.

Первый UI-срез должен дать компактный, рабочий сигнал:

- на dashboard: счетчик и focus-card "Проверить финансы";
- в work queue: отдельный раздел со списком первых issue-ов;
- без auto-fix, без POST actions, без редизайна grant/payroll/balances.

## Текущее состояние

Уже есть:

- `operations/services/financial_facts.py::AppointmentChargeFact`;
- `operations/services/financial_integrity.py::audit_appointments()`;
- dashboard focus cards and metrics;
- work queue summary cards and task sections;
- Browser plugin/skill может быть доступен в текущем приложении, но Lazyweb MCP не найден.

## Инварианты

1. UI-срез read-only: никаких `.save()`, `.update()`, `.delete()`, POST actions, auto-fix/backfill.
2. Источник issue-ов - `operations.services.financial_integrity`, не новая копия логики во view.
3. Dashboard показывает только компактный счетчик и next action, чтобы не перегрузить руководителя.
4. Work queue показывает issue code/severity/message и ссылки на занятие/счет, если объект есть.
5. Список work queue ограничен, но detail должен честно говорить, что это первые issue-ы из проверки.
6. Mixed funding informational issue не должен выглядеть как критическая ошибка.
7. Срез не меняет payroll/grant report/balance calculations.
8. Любое auto-fix/backfill/constraint требует отдельного миграционного контракта.
9. UI должен работать на desktop/mobile без горизонтального overflow.
10. Browser QA обязателен, потому что templates/views меняются.

## Не входит в первый срез

- новые модели, поля и миграции;
- auto-fix/backfill;
- изменение `billing.apply_decision()`;
- изменение payroll/grant/balance расчетов;
- grant report/balances/payroll redesign;
- CSV/Excel export of issue list;
- permissions beyond existing staff/admin dashboard/work queue access.

## Первый кодовый срез

Рабочее имя: `dashboard-work-queue-financial-integrity-signal`.

Ожидаемый состав:

- view helper в `operations/views/dashboard.py`, который собирает financial integrity issue-ы для кандидатов с `charge` или debit ledger;
- dashboard focus-card and metric for issue count;
- work queue summary card and section `#queue-financial-integrity`;
- tests in `operations/tests/test_views.py` for dashboard/work queue count and section rendering;
- Browser QA desktop/mobile for dashboard and work queue;
- recovery docs.

## Acceptance criteria первого среза

- Dashboard показывает финансовый focus-card, если audit нашел issue.
- Dashboard `priority_total` учитывает financial integrity issues.
- Work queue summary содержит отдельный item "Финансовый контроль".
- Work queue имеет section `#queue-financial-integrity` with issue rows.
- Issue rows show severity, message, appointment link when available, account/funding context when available.
- Mixed funding info appears as non-danger tone.
- No issue state returns neutral/empty state without breaking dashboard/work queue.
- No POST forms or mutation actions are added.
- `pytest operations/tests/test_views.py -q`, `manage.py check`, `makemigrations --check --dry-run`, full `pytest -q` pass.
- Browser QA desktop/mobile has no console/page errors and no horizontal overflow.

## Проверки

```powershell
.\.venv-test\Scripts\python.exe -m ruff check operations/views/dashboard.py operations/tests/test_views.py
.\.venv-test\Scripts\python.exe manage.py check
.\.venv-test\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv-test\Scripts\python.exe -m pytest operations/tests/test_views.py -q
.\.venv-test\Scripts\python.exe -m pytest -q
git diff --check
```

Browser QA:

- `/` or dashboard route on desktop 1365x900 and mobile 390x844;
- `/work-queue/#queue-financial-integrity` desktop/mobile;
- verify issue count/card/section visible with synthetic data, no horizontal overflow, no console/page errors;
- clean synthetic data after QA.

## Файловые границы

Разрешено:

- `operations/views/dashboard.py`;
- `templates/operations/dashboard.html`;
- `templates/operations/work_queue.html`;
- `static/operations/app.css`;
- `operations/tests/test_views.py`;
- recovery docs.

Запрещено без отдельного контракта:

- `operations/models.py`;
- `operations/migrations/*`;
- `operations/services/billing.py`;
- `operations/services/payroll.py`;
- `operations/services/reports.py` semantics;
- auto-fix/backfill;
- grant/balance/payroll redesign;
- Excel import/export.

## Параллельные агенты

Один ведущий агент меняет view/template/tests. Параллельно допустим только read-only design review or QA. Нельзя параллельно менять models/migrations/billing/payroll/report calculations.
