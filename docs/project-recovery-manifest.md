# Манифест восстановления проекта

Дата: 2026-07-10

Назначение: компактная точка входа для продолжения проекта "Радость моя" после потери чата, платформы, интернета или контекста. Этот файл не заменяет PRD, ADR, доменную модель и журнал состояния. Он указывает, где лежит актуальная информация и в каком порядке ее читать.

## Правило экономии контекста

- Не пересказывать весь проект, если сведения уже сохранены в документации.
- Обновлять только изменившиеся разделы и файлы.
- При восстановлении сначала читать этот манифест, затем только документы, нужные для текущей задачи.
- Graphify использовать как индекс связей и навигации, а не как вторую копию всей документации.
- Если изменилась конфигурация skills, состав источников правды или порядок восстановления, обновлять этот манифест.

## Источники правды

| Файл | Роль | Когда читать |
| --- | --- | --- |
| `docs/project-recovery-manifest.md` | Входная точка восстановления, индекс документов, skills и протокол контрольных точек. | Всегда первым. |
| `docs/current-state.md` | Текущее состояние, выполненные срезы, свежий журнал изменений, ближайшие риски. | Всегда вторым; в первую очередь последние датированные разделы. |
| `docs/07-updated-domain-model-after-interview.md` | Живой доменный контракт после интервью 2026-06-23: занятия, участники, специалисты, кабинеты, гранты, табели. | Перед задачами по БД, расписанию, финансам, грантам, табелям. |
| `docs/08-parallel-agent-execution-plan.md` | Контракты параллельной работы агентов, зоны владения файлами и режим read-only reviewer; свежий статус брать из `current-state`. | Перед распараллеливанием и перед изменениями `operations/models.py`/миграций. |
| `docs/09-cascade-reschedule-domain-slice.md` | Контракт и статус первого среза persisted-планов переноса расписания. | Перед любыми изменениями переноса, цепочек согласования, `AppointmentMoveForm`, `scheduling.py`, моделей плана или миграций. |
| `docs/10-reschedule-chain-dependencies-contract.md` | Контракт будущей модели атомарных цепочек переноса: chain/dependency schema, порядок применения, риски миграций и вертикальные срезы. | Перед кодом по длинным цепочкам переноса нескольких занятий. |
| `docs/01-prd.md` | Базовые продуктовые цели и крупные сценарии. | Для сверки продукта; при конфликте новее `current-state` и `07`. |
| `docs/03-ux-ui-and-implementation-plan.md` | UX/UI карта и план рабочих экранов; содержит предупреждение 2026-06-29, что старый MVP-контекст уступает свежей доменной модели и `current-state`. | Перед изменениями интерфейса администратора/руководителя. |
| `docs/05-domain-rules-mvp.md` | Старые доменные правила MVP. | Только как историческая база; проверять против `07`. |
| `docs/06-mvp-technical-model.md` | Старый технический baseline. | Для понимания исходной модели; проверять против кода и `07`. |
| `docs/decisions/ADR-001-django-postgresql-local-first.md` | Архитектурное решение по стеку: Django/PostgreSQL/local-first подход. | При изменениях стека, deployment или хранилища. |
| `docs/decisions/ADR-002-balance-accounts-ledger.md` | Архитектурное решение по счетам баланса и ledger. | Перед изменениями финансов, списаний, платежей, грантов. |
| `docs/interviews/interview-director-2026-06-23.md` | Первичный источник требований после интервью. | Когда нужно проверить смысл требования или спорный бизнес-процесс. |
| `docs/next-chat-prompt.md` | Короткий переносимый промпт для новой сессии. | При ручном старте нового чата. |
| `graphify-out/GRAPH_REPORT.md`, `graphify-out/graph.json` | Графовый индекс проекта. | Для навигации по связям файлов/моделей; требует обновления после крупных изменений. |

## Порядок восстановления контекста

1. Открыть `docs/project-recovery-manifest.md`.
2. Прочитать последние датированные разделы `docs/current-state.md`.
3. Если задача затрагивает БД, расписание, финансы, гранты, табели или статусы, прочитать `docs/07-updated-domain-model-after-interview.md` и ADR-002.
4. Если задача затрагивает переносы, отсутствие специалиста, занятые окна или каскадные сдвиги расписания, прочитать `docs/09-cascade-reschedule-domain-slice.md`.
5. Если задача затрагивает атомарные цепочки применения нескольких переносов, прочитать `docs/10-reschedule-chain-dependencies-contract.md`.
6. Если задача затрагивает параллельную работу, прочитать `docs/08-parallel-agent-execution-plan.md`.
7. Если задача UX/UI, прочитать `docs/03-ux-ui-and-implementation-plan.md` и соответствующие шаблоны/JS.
8. Если задача по коду, читать только релевантные файлы: модели, сервисы, формы, views, шаблоны и тесты вокруг изменяемого сценария.
9. Перед широким поиском по проекту сначала выполнить `graphify query "<вопрос>" --budget 500-1500`, если граф доступен и актуален.

## Skills и назначение

| Skill | Назначение в проекте |
| --- | --- |
| `database-schema-designer` | Проектирование БД, ограничений, индексов, опасных миграций. Использовать для доменной модели и миграционных планов. |
| `graphify` | Графовый индекс проекта, поиск связей между документами, моделями, сервисами и UI. Не хранить в нем полные копии документации вручную. |
| `documentation-and-adrs` | Контрольные точки, ADR, changelog, обновление документации без дублирования. |
| `planning-and-task-breakdown` | Разбиение больших требований на вертикальные срезы с acceptance criteria. |
| `frontend-ui-engineering` / `frontend-design` | Рабочие интерфейсы администратора и руководителя, когда задача касается UX/UI. |
| `browser-testing-with-devtools` / Browser plugin | Визуальная QA в браузере, если инструмент доступен в текущей сессии. |
| `code-review-and-quality` | Проверка изменений перед завершением крупного среза. |
| `git-workflow-and-versioning` | Ветки, staging, commits и координация незакоммиченных изменений. |

## Восстановление инструментов

### Graphify

Graphify - это навигационный граф проекта. Он помогает искать связи между документами, моделями, сервисами, views и шаблонами, но не заменяет свежие документы и код.

Причина ошибки `no LLM API key for semantic extraction`: кодовая часть графа извлекается структурно, а измененные `.md`/документы требуют LLM для семантического разбора. В текущем окружении ключ не виден процессу Codex.

Как исправить без записи секрета в репозиторий:

```powershell
# Временно, только для текущего терминала
$env:GEMINI_API_KEY = "<ключ>"
graphify . --update --no-viz

# Постоянно для пользователя Windows; после этого перезапустить терминал/Codex
[Environment]::SetEnvironmentVariable("GEMINI_API_KEY", "<ключ>", "User")
```

Можно использовать другой backend, если текущая версия Graphify его поддерживает, но предпочтительный безопасный путь для этого проекта - переменная окружения `GEMINI_API_KEY` или `GOOGLE_API_KEY`. Значение ключа нельзя записывать в `.env`, документацию, git, чат или production-конфиги.

### Browser QA

Browser QA - это проверка живого интерфейса в браузере: скриншоты desktop/mobile, overflow, кликабельность, console errors и поведение JS. Это не заменяется Django tests, потому что тесты не видят реальную верстку.

Если in-app Browser/DevTools tool не доступен через discovery в текущей сессии, есть два безопасных пути:

1. Включить Browser plugin или Chrome DevTools MCP в Codex и повторить QA через браузерный инструмент.
2. Использовать временный Playwright-фолбэк вне репозитория, не добавляя `package.json` и зависимости в проект. Такой запуск должен писать все npm-файлы во временный каталог, а не в `D:\РадостьМояАвтоматизация\RMcodex`.

## Протокол контрольной точки

При запросе "сделай контрольную точку" или при риске потери контекста:

1. Обновить только изменившиеся разделы `docs/current-state.md`.
2. Если появилось архитектурное решение, добавить или обновить ADR в `docs/decisions/`.
3. Если изменились источники правды, skills или порядок восстановления, обновить этот манифест.
4. Если изменился план параллельной работы, обновить `docs/08-parallel-agent-execution-plan.md`.
5. Если изменился переносимый стартовый контекст, обновить `docs/next-chat-prompt.md`.
6. Обновить Graphify инкрементально: `graphify . --update --no-viz`. Если команда недоступна или падает, записать риск в `current-state`.
7. Зафиксировать проверки: тесты, `manage.py check`, линтеры, `git diff --check` или причину, почему проверка не запускалась.

## Текущее состояние восстановления

- Последний существенный срез: read-only схема атомарных цепочек переноса. Через миграцию `operations.0021_reschedule_chains` добавлены `AppointmentRescheduleChain`, `AppointmentRescheduleStepDependency`, nullable поля `chain/chain_position/chain_required` на `AppointmentRescheduleStep`, admin и auditlog registration. До этого реализовано закрытие альтернативных шагов после применения одного варианта, периодные read-only метрики `/reschedule-plans/`, применение одиночного шага с одноразовым разрешением кабинета (`AppointmentRoomOverride` с причиной и автором), read-only контроль реестра, ручной разбор `review_conflict`, `staff_absence` план без отмены занятий, ручное применение `approved`-шага, первый persisted-план переноса, связь `AppointmentConfirmation.reschedule_step`, отправка согласований и итог `confirmation_status/confirmation_summary`, а Stage 5 UX/UI закрыт финальным аудитом `data-label`.
- Срез 2 по `docs/10-reschedule-chain-dependencies-contract.md` реализован: сервис `create_chain_for_steps()` создает chain/dependencies из выбранных `move`-шагов без применения расписания, проверяет cycle/mismatch и запрещает трактовать альтернативы одного `source_appointment` как цепочку. Detail плана показывает read-only блок цепочек.
- Кодовая база уже содержит модель групповых занятий, участников, назначений специалистов, лимитов кабинетов, грантовых квот, payroll и preview-импорта.
- Последняя полная проверка после построения цепочки без применения: focused `ReschedulingPlanServiceTests` + `ReschedulePlanViewTests` прошел (`36 passed`), `.\.venv-test\Scripts\python.exe manage.py check`, `.\.venv-test\Scripts\python.exe manage.py makemigrations --check --dry-run`, `.\.venv-test\Scripts\python.exe -m ruff check operations` и полный `.\.venv-test\Scripts\python.exe -m pytest -q` прошли; 398 тестов, 1 прежнее предупреждение django-tasks о будущей замене `CheckConstraint.check`.
- Проверка миграций: `.\.venv-test\Scripts\python.exe manage.py makemigrations --check --dry-run` прошла с `No changes detected`.
- В `operations/management/commands/seed_demo.py` изменен только порядок импортов через `ruff --fix`; логика seed-команды и демо-данные не менялись.
- `git diff --check` по файлам UX-срезов и стабилизации табеля, грант-отчета, дашборда, рабочей очереди, низких балансов в очереди, мобильного кабинета специалиста, предпросмотра импорта, справочников кабинетов/услуг/специалистов/источников финансирования/ставок специалистов, общего шаблона форм, форм кабинетов, форм услуг, форм источников финансирования, форм специалистов, форм ставок специалистов, расчетного листа payroll, массового переноса, отмены занятия, ручного переноса, формы платежа, списка балансов, форм балансовых счетов, карточки получателя, списка получателей, форм получателя/представителей, форм программ/каскадов, edge-case удаления балансового счета, all-archived источников и period-aware статуса ставок, миграции средств между каскадами, публичного согласования занятия, списка рекомендаций, списка документов, списка согласий, форм документов/согласий/рекомендаций, экрана "Завтра", форм грантовых квот/распределений и `seed_demo.py` дал только предупреждения о будущей замене LF на CRLF.
- Browser QA выполнена через in-app Browser: desktop 1280px и mobile 390px для дашборда, рабочей очереди, грант-отчета и табеля; общестраничного overflow и console errors не найдено.
- Для UX-срезов после первоначальной Browser QA выполнен Playwright-фолбэк вне репозитория: 28 GET-страниц проверены на desktop 1280px и mobile 390px, всего 56 проверок. HTTP 4xx/5xx, console/page errors и горизонтальный overflow не найдены; выборочно просмотрены скриншоты мобильной формы счета баланса, desktop-списка ставок, мобильного грант-отчета и desktop-импорта получателей. In-app Browser tool по-прежнему не был доступен через discovery в этой сессии; Playwright установлен во временный каталог `%TEMP%\rmcodex-playwright-qa`, скриншоты и JSON-отчет лежат во временном `%TEMP%\rmcodex-browser-qa`.
- Объектная Browser QA выполнена через тот же Playwright-фолбэк: 28 URL с реальными ID локальной dev-базы проверены на desktop/mobile, всего 56 проверок. HTTP 4xx/5xx, console/page errors и общестраничного overflow не найдено. Найден и исправлен мобильный UX-долг в карточке получателя: таблица счетов баланса на 390px превращена в карточки с видимыми кнопками `Править`/`Пополнить`. Артефакты лежат во временных `%TEMP%\rmcodex-browser-qa-objects` и `%TEMP%\rmcodex-browser-qa-recipient-fix`.
- Карточка получателя дополнительно стабилизирована: таблицы представителей, блоков программ, предстоящих и прошедших занятий в `recipient_detail` на 390px превращены в карточки с `data-label`; будущие и прошедшие занятия показывают участников группы и назначения специалистов без горизонтального скролла. Browser QA desktop/mobile чистая; артефакты лежат во временном `%TEMP%\rmcodex-browser-qa-recipient-detail-tables`.
- Реестры поддержки получателя дополнительно стабилизированы: таблицы рекомендаций, документов и согласий на 390px превращены в карточки с `data-label`; action-ячейка рекомендаций и table wrappers без горизонтального скролла. Browser QA desktop/mobile чистая; артефакты лежат во временном `%TEMP%\rmcodex-browser-qa-support-tables`.
- Мастер расписания каскада дополнительно стабилизирован: preview-таблица "Предложенные окна" на 390px превращена в карточки с `data-label`; проверен POST `action=preview`, создание занятий не запускалось. Browser QA desktop/mobile чистая; артефакты лежат во временном `%TEMP%\rmcodex-browser-qa-program-wizard-table`.
- Dashboard дополнительно стабилизирован: таблицы "Сегодня" и "Низкие балансы" на 390px превращены в карточки с `data-label`; Browser QA desktop/mobile чистая, проверены 2 `ops-table`, без overflow. Артефакты лежат во временном `%TEMP%\rmcodex-browser-qa-dashboard-tables`.
- Экран "Завтра" дополнительно стабилизирован: таблица "Занятия дня" на 390px превращена в карточки с `data-label`; проверено групповое занятие с несколькими получателями и специалистами. Browser QA desktop/mobile чистая; артефакты лежат во временном `%TEMP%\rmcodex-browser-qa-tomorrow-table`.
- Рабочая очередь администратора дополнительно стабилизирована: таблица низких балансов в `#queue-balances` на 390px превращена в карточки с `data-label`; кнопки "Пополнить"/"Счет" остаются внутри viewport. Browser QA desktop/mobile чистая; артефакты лежат во временном `%TEMP%\rmcodex-browser-qa-work-queue-balances`.
- Справочники дополнительно стабилизированы: общий `directory-table` в списках получателей, услуг, кабинетов, источников финансирования, специалистов и ставок на 390px превращен в карточки с `data-label`; Browser QA desktop/mobile чистая для 6 URL. Артефакты лежат во временном `%TEMP%\rmcodex-browser-qa-directory-tables`.
- Предпросмотр импорта получателей дополнительно стабилизирован: таблицы распознанных колонок и строк файла на 390px превращены в карточки с `data-label`; Browser QA desktop/mobile чистая, проверены POST preview без записи в БД. Артефакты лежат во временном `%TEMP%\rmcodex-browser-qa-import-preview-tables`.
- Список балансов дополнительно стабилизирован: таблица счетов на 390px превращена в карточки с `data-label`; Browser QA desktop/mobile чистая, проверены кнопки "Править"/"Пополнить"/"Удалить" без POST-действий. Артефакты лежат во временном `%TEMP%\rmcodex-browser-qa-balances-table`.
- Финальный аудит `templates/operations/*.html` показал, что видимых `<td>` без `data-label` больше нет; пустая строка расчетного листа payroll размечена `data-label="Строки"`.
- `payroll_sheet_detail` дополнительно проверен через локальную QA-фикстуру payroll: стандартная валидация отклонила занятие вне окна 09:00-18:00, затем создан draft расчетного листа с 1 строкой на 700 руб. Найден и исправлен мобильный UX-долг строк начислений: таблица payroll на 390px превращена в карточки с `data-label`. Повторная desktop/mobile Browser QA чистая; артефакты лежат во временных `%TEMP%\rmcodex-browser-qa-payroll-sheet` и `%TEMP%\rmcodex-browser-qa-payroll-sheet-fix`.
- Мобильный кабинет специалиста дополнительно стабилизирован: таблицы "Рабочий график" и "Отпуск и отгулы" в `specialist_home` на 390px превращены в карточки с `data-label`, кнопка действия рабочего окна остается внутри viewport. Browser QA desktop/mobile чистая; артефакты лежат во временном `%TEMP%\rmcodex-browser-qa-specialist-home`.
- Табель специалиста дополнительно стабилизирован: таблицы дней периода, итогов, сохраненных начислений, расчетных листов и расшифровки начислений в `staff_timesheet` на 390px превращены в карточки с `data-label`. Browser QA desktop/mobile чистая; артефакты лежат во временном `%TEMP%\rmcodex-browser-qa-staff-timesheet`.
- Карточка занятия дополнительно стабилизирована: таблицы участников, специалистов, операций баланса, истории email, других занятий и audit в `appointment_detail` на 390px превращены в карточки с `data-label`; фиксированная сетка форм списания/каскадов заменена на auto-fit, чтобы поля и кнопки не обрезались в двухколоночной detail-grid. Browser QA desktop/mobile чистая; артефакты лежат во временном `%TEMP%\rmcodex-browser-qa-appointment-detail`.
- Грант-отчет дополнительно стабилизирован: таблицы баланса, квот, выделений получателям, сертификатов и скидок в `grant_report` на 390px превращены в карточки с `data-label`; внутренний scrollWidth у action-таблиц устранен, кнопка "Удалить" в действиях грант-отчета больше не наследует зеленый primary-стиль. Browser QA desktop/mobile чистая; артефакты лежат во временном `%TEMP%\rmcodex-browser-qa-grant-report`.
- POST-пути занятия дополнительно проверены через Browser QA на mobile 390px: списание конкретного участника группы создает debit ledger, отмена день-в-день без ack остается на форме с ошибкой, отмена с ack меняет статус без автоматического списания, ручной перенос группы создает новое занятие с 2 участниками и 2 специалистами. Код не менялся; артефакты лежат во временном `%TEMP%\rmcodex-browser-qa-post-flows`.
- Для Browser QA локальная dev-база была мигрирована до `operations.0020`, создан/обновлен технический `qa_admin`; это локальное состояние окружения, не production. В последнем срезе создана и затем очищена синтетическая фикстура `browserqa_reschedule_alternatives`; detail плана `/reschedule-plans/18/` проверен через bundled Playwright на desktop/mobile: после применения первого варианта альтернатива стала "Пропущен", форм повторного применения не осталось, overflow и console errors нет.
- Graphify-граф обновлен 2026-07-12 после установки недостающего Python-пакета `openai` в interpreter из `graphify-out/.graphify_python`. Semantic extraction через Gemini прошла для 48 docs; `graphify-out/graph.json` обновлен до 3433 nodes / 12881 edges, `GRAPH_REPORT.md` - до 203 communities. Старый curated graph сохранен Graphify в ignored backup `graphify-out/2026-07-12/`. `graphify query` снова видит свежие `apply_chain()`, `revalidate_chain()` и `create_chain_for_steps()`.
- Latest chain slice status 2026-07-12: `apply_chain(chain)` is implemented, registry-level chain UX/metrics are implemented, and Browser QA passed via bundled Playwright + system Chrome on desktop 1365x900 and mobile 390x844. The checked URL was `/reschedule-plans/?focus=chain_ready&metrics_period=7`; artifacts are in `%TEMP%\rmcodex-browser-qa-chain-metrics`.

## Ближайшие задачи

- Обновить semantic-часть Graphify, когда будет доступен LLM API key или локальный backend для docs/papers/images; code-only граф уже обновляется локально командой `graphify update . --no-cluster`.
- Continue with the next non-DB operations slice after registry-level chain metrics, dashboard/work queue signals, and successful Browser QA. A separate manager chain dashboard is deferred until real usage shows a concrete gap. Do not change ledger/payroll or add migrations without a new approved contract.
- Не начинать новые миграции без явного владельца БД и сверки с `docs/07-updated-domain-model-after-interview.md`, `docs/08-parallel-agent-execution-plan.md` и `docs/09-cascade-reschedule-domain-slice.md`.

## Постоянные риски

- Нельзя параллельно менять `operations/models.py` и chain миграций несколькими агентами.
- Нельзя коммитить секреты, production-конфиги, реальные персональные данные и реальные Excel-выгрузки.
- Старые legacy-поля `Appointment.child`, `Appointment.staff_member`, `Appointment.billing_account` нельзя удалять до полного backfill и переключения отчетов.
- Граф Graphify полезен как индекс, но устаревший граф не должен считаться источником правды против свежих документов и кода.

- Graphify was repaired on 2026-07-12 with a temporary in-process Google AI Studio key; do not record that key in project files. If the key was pasted into chat, prefer rotating it in Google AI Studio after this maintenance window.
- Latest dashboard/work queue slice 2026-07-12: active ready/stale/failed reschedule chains are now surfaced in the main dashboard and administrator work queue without DB changes. Dashboard focus/metric links point to existing `/reschedule-plans/?focus=chain_*`; work queue has `#queue-reschedule-chains` with direct plan links. Verification: focused tests, related view tests, `manage.py check`, migration dry-run, `ruff check operations`, `git diff --check`, full pytest (`410 passed`), and Playwright desktop/mobile Browser QA all passed. Artifacts: `%TEMP%\rmcodex-browser-qa-dashboard-chain`.
- Latest chain ordering/filter slice 2026-07-12: dashboard/work queue now sort chain attention by operational severity (`failed`, then `stale`, then `ready`) through a read-only queryset annotation, and registry filters `chain_ready`/`chain_stale`/`chain_failed` have regression coverage. No DB, model, ledger, payroll, or schedule mutation. Verification: focused ordering tests (`2 passed`), related view tests (`35 passed`), Ruff, `manage.py check`, and migration dry-run (`No changes detected`) passed.
- Graphify code-index was updated after the latest chain ordering/filter slice: `graphify update . --no-cluster` re-extracted `134/134` code files and wrote `3868` nodes / `13945` edges. Semantic docs extraction still needs `GEMINI_API_KEY`/`GOOGLE_API_KEY`; do not write the key to project files.
- Latest work queue chain next-action slice 2026-07-12: `#queue-reschedule-chains` cards now explain the next action per status (failed -> resolve error in plan, stale -> revalidate in plan, ready -> final check/apply in plan) without direct POST actions from the queue. Verification: focused pytest (`1 passed`), related view tests (`37 passed`), Ruff, `manage.py check`, migration dry-run, and Playwright desktop/mobile Browser QA passed; artifacts are in `%TEMP%\rmcodex-browser-qa-chain-next-actions`.
- Graphify code-index was updated after the work queue chain next-action slice: `graphify update . --no-cluster` re-extracted `134/134` code files and wrote `3869` nodes / `13947` edges. Semantic docs extraction still needs `GEMINI_API_KEY`/`GOOGLE_API_KEY`; do not write the key to project files.
- Latest chain detail diagnostics slice 2026-07-12: `/reschedule-plans/<id>/` now explains stale/blocked/failed chains in place from `validation_summary.apply_error`, `validation_summary.issues`, and step `validation_messages`; chain action messages and the apply confirm are Russian. No DB/model/migration/ledger/payroll/schedule mutation. Verification: focused detail tests (`5 passed`), full `ReschedulePlanViewTests` (`29 passed`), related service/view/work-queue tests (`66 passed`), Ruff, `manage.py check`, migration dry-run, full pytest (`417 passed`), and Playwright desktop/mobile Browser QA passed; artifacts are in `%TEMP%\rmcodex-browser-qa-chain-detail-diagnostics`.
- Graphify code-index was updated after the chain detail diagnostics slice: `graphify update . --no-cluster` re-extracted `134/134` code files and wrote `3877` nodes / `13998` edges. Semantic docs extraction still needs `GEMINI_API_KEY`/`GOOGLE_API_KEY`; do not write the key to project files.
- Recovery priority now: continue from the chain UX/operations layer with the next non-DB operations slice or open a fresh contract before DB/ledger/payroll work. A separate manager chain dashboard is deferred until real usage shows a concrete gap beyond registry metrics plus dashboard/work queue/detail diagnostics; do not add migrations or touch ledger/payroll unless a fresh contract is approved.
