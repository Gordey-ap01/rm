# Манифест восстановления проекта

Дата актуализации: 2026-08-29

Назначение: минимальная точка входа после потери сессии. Манифест не
пересказывает проект, а указывает источники истины и порядок чтения.

## 1. Порядок восстановления

1. Прочитать этот файл.
2. Прочитать только верхний актуальный раздел `docs/current-state.md`.
3. Прочитать `docs/01-prd.md` и ADR, относящиеся к текущему срезу.
4. Прочитать профильный контракт из индекса ниже.
5. Проверить `git status`, текущую ветку, последние коммиты и тесты.
6. Использовать Graphify для навигации по связям, затем подтверждать выводы
   чтением исходных файлов.

Архивные журналы и PRD не читать по умолчанию. Они нужны только для выяснения
истории решения.

## 2. Приоритет источников

1. `docs/01-prd.md` - канонические продуктовые требования и матрица готовности.
2. `docs/decisions/ADR-*.md` - принятые архитектурные решения.
3. `docs/07-updated-domain-model-after-interview.md` - доменный контракт после
   интервью.
4. Профильный контракт активного вертикального среза.
5. Модели, миграции, сервисы и тесты - фактически реализованное поведение.
6. `docs/current-state.md` - статус выполнения и ближайшая работа.
7. Архивы и MVP-документы - только исторический контекст.

При противоречии между PRD и кодом нельзя молча считать код правильным:
расхождение фиксируется как gap, после чего меняется либо контракт, либо код.

## 3. Индекс документации

| Документы | Назначение |
| --- | --- |
| `docs/00-input-summary.md` | Исходный обзор входных материалов. |
| `docs/01-prd.md` | Канонический PRD, роли, сценарии и readiness matrix. |
| `docs/02-tech-stack-research.md` | Исследование стека. |
| `docs/03-ux-ui-and-implementation-plan.md` | Базовая UX/UI карта; сверять с PRD. |
| `docs/04-clarifying-questions.md` | Исторический список вопросов. |
| `docs/05-domain-rules-mvp.md`, `docs/06-mvp-technical-model.md` | Старый MVP baseline, не текущий контракт. |
| `docs/07-updated-domain-model-after-interview.md` | Доменная модель после интервью. |
| `docs/08-parallel-agent-execution-plan.md` | Владение зонами и правила параллельной работы. |
| `docs/09-*.md` ... `docs/13-*.md` | Переносы, цепочки и единая проверка вместимости расписания. |
| `docs/14-*.md` ... `docs/19-*.md` | Финансовый факт и financial-integrity контур. |
| `docs/20-group-payroll-policy-contract.md` | Групповая зарплатная политика. |
| `docs/21-*.md`, `docs/22-*.md` | Расходы, активы, категории и контрагенты. |
| `docs/23-*.md` ... `docs/39-*.md` | Договоры, шаблоны, документы, акты, согласия и сертификаты. |
| `docs/40-*.md` ... `docs/47-*.md` | Импорт и баланс сертификатов, preflight и readiness UI. |
| `docs/48-time-off-decision-authority-contract.md` | Решения по отсутствиям и обязательный контроль руководителя. |
| `docs/49-postgresql-schedule-billing-write-serialization-contract.md` | Контракт конкурентной записи: A вместимость и B идемпотентное списание завершены. |
| `docs/50-postgresql-ci-contract.md` | Обязательная CI-проверка PostgreSQL 17, миграций, линтера и полного pytest. |
| `docs/51-payroll-director-approval-contract.md` | Реализованный срез: ставки и утверждение payroll только руководителем. |
| `docs/52-payroll-payout-lifecycle-contract.md` | Реализованный срез: передача листа в выплату, полный факт выплаты и неизменяемый журнал. |
| `docs/53-operational-e2e-acceptance-contract.md` | Реализованный срез: сквозная service-приемка записи, списания, payroll, выплаты и приоритета руководителя по отпуску. |
| `docs/54-production-preflight-contract.md` | Реализованный срез: health, проверяемые backup/restore, CI restore-drill и границы внешнего monitoring/SMTP. |
| `docs/55-browser-role-acceptance-contract.md` | Реализованный срез: browser-приемка администратора, руководителя и mobile-кабинета специалиста. |
| `docs/56-persisted-balance-transfer-conversion-contract.md` | Реализованный срез: immutable transfer, `money -> sessions`, PostgreSQL-сериализация и browser-приемка. |
| `docs/57-grant-management-report-acceptance-contract.md` | Реализованный отчетный срез: роли, периодные ledger-балансы, раздельные единицы, квоты, архив и безопасный CSV; также границы будущих опасных миграций грантов. |
| `docs/58-grant-plan-versioning-contract.md` | Реализованный срез: типизированные редакции квот и распределений, director-only write-path, legacy backfill и payroll provenance. |
| `docs/59-grant-fixed-compensation-and-donor-report-snapshot-contract.md` | Реализованный эпик 59: payroll-бюджет, fixed/per-session policy, фиксированная оплата проекта и закрытый донорский снимок. |
| `docs/60-private-artifact-storage-and-donor-submission-contract.md` | Реализованный срез 59B-2: private storage, append-only сдачи/выдачи, integrity и backup/restore v2; там же production-блокеры. |
| `docs/61-group-program-series-lifecycle-contract.md` | Активный доменный контракт: 61A-61B, 61C-1/61C-2/61C-3, `0060`, `missing_only`, frozen targets `0061` и сервис `retry_skipped` реализованы; следующее cancel/withdraw и 61D UI. |
| `docs/decisions/ADR-001-*.md` | Django/PostgreSQL/local-first. |
| `docs/decisions/ADR-002-*.md` | Балансовые счета и ledger. |
| `docs/decisions/ADR-003-*.md` | Полномочия и ручные решения. |
| `docs/decisions/ADR-004-*.md` | Типизированные журналы вместо generic workflow. |
| `docs/decisions/ADR-005-*.md` | Блокировка строки кабинета для настраиваемой вместимости. |
| `docs/decisions/ADR-006-*.md` | Типизированные редакции грантового плана с устойчивой текущей проекцией. |
| `docs/decisions/ADR-007-*.md` | Принятое расширение общего payroll, budget provenance и неизменяемые снимки внутренней сверки проекта. |
| `docs/decisions/ADR-008-*.md` | Отдельное private content-addressed/write-once хранилище вне Caddy/media и server-mediated access. |
| `docs/decisions/ADR-009-*.md` | Устойчивый корень серии, типизированные редакции и append-only запуски materialization. |
| `docs/interviews/*.md` | Первичные интервью; читать при спорном требовании. |
| `docs/PRODUCTION_DEPLOYMENT.md` | Развертывание и эксплуатация production. |
| `docs/current-state.md` | Компактная текущая контрольная точка. |
| `docs/archive/prd/` | Сохраненные старые PRD, не источники текущих требований. |
| `docs/archive/recovery/` | Полные старые журналы и промпты, не читать по умолчанию. |
| `docshablon/` | Приватные исходные образцы документов; не коммитить. |
| `docs/24-document-template-source-inventory.md` | Обезличенный индекс `docshablon/`. |

## 4. Архитектурные владельцы

- Один ведущий агент владеет PRD, доменной моделью и порядком срезов.
- Только один DB owner одновременно меняет `operations/models.py` и цепочку
  миграций.
- Параллельные агенты допустимы после фиксации контракта для read-only аудита,
  UI, тестов и документации в непересекающихся файлах.
- Финансы, расписание, статусы и полномочия меняются только вертикальным срезом:
  контракт, БД, сервис, UI, аудит и тесты.

## 5. Skills и инструменты

| Skill/инструмент | Использование |
| --- | --- |
| `database-schema-designer` | Схема, constraints, indexes и безопасные миграции. |
| `graphify` | Индекс связей и поиск затронутых зон, не копия документации. |
| `documentation-and-adrs` | PRD, ADR и контрольные точки. |
| `planning-and-task-breakdown` | Вертикальные срезы и acceptance criteria. |
| `code-review-and-quality` | Ревью перед merge. |
| `security-and-hardening` | Роли, персональные данные, uploads и внешние ссылки. |
| `frontend-ui-engineering` | Рабочие интерфейсы администратора/руководителя. |
| `browser-testing-with-devtools` | Browser smoke после UI-изменений, когда доступен. |
| `git-workflow-and-versioning` | Малые проверенные коммиты и удаленная страховка. |
| `source-driven-development` | Проверка Django и PostgreSQL-команд по официальной документации. |
| `shipping-and-launch` | Production preflight, rollback и границы запуска. |

Graphify может работать локально для индекса кода. Semantic extraction требует
внешнюю LLM-модель и ее ключ, но не является обязательной для продолжения.
Ключи нельзя хранить в tracked-файлах проекта.

## 6. Контрольная точка

При завершении среза обновлять только:

1. один актуальный раздел в `docs/current-state.md`;
2. изменившуюся строку readiness matrix в `docs/01-prd.md`;
3. ADR, только если принято новое архитектурное решение;
4. этот манифест, только если изменились источники, skills или порядок чтения;
5. Graphify один раз после завершенного эпика, если он доступен.

Перед остановкой: `git status`, secret scan, focused tests, полный регресс при
общем изменении, commit и push. Незакоммиченные изменения перечислять явно.
