Update 2026-07-14: `apply_chain(chain)`, registry-level chain UX/metrics, dashboard/work queue chain signals, chain attention ordering (`failed` -> `stale` -> `ready`), work queue next-action guidance, detail-page chain diagnostics for stale/blocked/failed chains, direct work-queue-to-detail chain deep links (`#chain-<id>`), registry row priority chain deep links, registry row priority step deep links (`#step-<id>`), chain POST anchor redirects, terminal-chain read-only actions, step-level POST anchor redirects, work queue non-chain step attention cards, dashboard non-chain step signals, work queue confirmation-card links to exact reschedule steps, visible `:target` highlighting for `#chain-*`/`#step-*`/`#queue-*`, terminal-plan revalidation guard/read-only action visibility, terminal-plan action lock for chain/step actions, terminal-plan confirmation queue filtering, terminal-plan registry attention filtering, centralized terminal plan status constants, terminal-plan registry confirmation archive labels, terminal-plan detail confirmation archive labels, terminal-plan detail archive copy, terminal-plan chain archive copy, chain-to-step detail links, chain dependency-to-step links, and terminal-plan header move-link hiding are implemented and tested. Browser QA passed for work queue guidance, chain detail diagnostics, dashboard/work queue chain/step deep links, registry chain/step deep links, chain/step POST redirect anchor preservation, terminal chain action visibility, reschedule-step confirmation links, target highlighting, terminal plan action visibility, terminal plan action lock, terminal-plan confirmation filtering, terminal-plan registry attention filtering, terminal-plan registry confirmation archive labels, terminal-plan detail confirmation archive labels, terminal-plan detail archive copy, terminal-plan chain archive copy, chain-to-step detail links, chain dependency-to-step links, and terminal-plan header move-link hiding. A separate manager chain dashboard is deferred until real usage shows a concrete gap; next safe step is another non-DB operations slice or a fresh contract before DB/ledger/payroll/status work.

# Промпт для следующего чата по проекту "Радость моя"

Скопировать этот текст в новый чат, если работа продолжится отдельно.

```text
Продолжаем проект "Радость моя" в репозитории D:\РадостьМояАвтоматизация\RMcodex.

Цель: не срочный MVP, а серьезный рабочий продукт для реабилитационного центра: расписание, финансы, программы занятий, гранты, табели, кабинет специалиста и отчеты руководителя.

Текущий этап: Stage 5 UX/UI табличная стабилизация закрыта. Доменный срез persisted-планов переноса расписания реализован по docs/09-cascade-reschedule-domain-slice.md: есть модели плана/шагов, сервис, минимальный UI, тесты, миграция 0018, связь AppointmentConfirmation.reschedule_step и согласования по валидному шагу плана через 0019, итог согласований шага `confirmation_status/confirmation_summary` через 0020, read-only реестр `/reschedule-plans/` с фильтрами статуса, согласований и управленческим фильтром `focus` (`manual_review`, `stale`, `failed`, `waiting`, `declined`, `ready_to_apply`). Если согласования отправлены, `waiting` и `declined` блокируют применение шага; `approved`-шаг применяется вручную администратором через detail-кнопку "Применить согласованный перенос" с browser-confirm. Массовое отсутствие специалиста уже может создаваться как `staff_absence` persisted-план без отмены занятий; detail плана умеет ручной разбор `review_conflict` через быстрые действия "Открыть занятие", "Перенести вручную", "Отменить" и "Отметить разобранным". Закрыть `review_conflict` можно только после фактического переноса/отмены исходного занятия. Одиночный шаг с `requires_room_override=True` теперь применяется только через отдельную кнопку "Применить с разрешением кабинета" и сохраняет `AppointmentRoomOverride` на новом занятии с причиной и автором. Реестр `/reschedule-plans/` также показывает read-only метрики "Текущий контроль" и "Динамика" за 7/30/90 дней или все время. Важно: текущие несколько `move` шагов для одного `source_appointment` являются альтернативами, а не цепочкой; после применения одного варианта остальные нетерминальные шаги того же занятия автоматически становятся `skipped`, шаги других исходных занятий остаются рабочими. Контракт цепочек находится в docs/10-reschedule-chain-dependencies-contract.md; срез 1 схемы реализован миграцией 0021 (`AppointmentRescheduleChain`, `AppointmentRescheduleStepDependency`, nullable `chain/chain_position/chain_required` на step), срез 2 реализован сервисом `create_chain_for_steps()` и read-only блоком цепочек в detail плана. `revalidate_chain(chain)`, atomic `apply_chain(chain)`, registry-level chain UX/metrics, chain/step deep links, POST anchor preservation, terminal-chain read-only actions, dashboard/work queue attention cards for chains and non-chain steps, work queue confirmation cards linked to exact reschedule steps, visible target highlighting for chain/step/queue deep links, plan-level terminal revalidation guard/read-only actions, terminal-plan action lock for chain/step actions, terminal-plan confirmation queue filtering, terminal-plan registry attention filtering, centralized terminal plan status constants, terminal-plan registry confirmation archive labels, terminal-plan detail confirmation archive labels, terminal-plan detail archive copy, terminal-plan chain archive copy, chain-to-step detail links, chain dependency-to-step links, and terminal-plan header move-link hiding are complete. Terminal plan status contract is in docs/11-plan-terminal-status-contract.md. Next task: continue with the next non-DB operations slice or create a fresh approved contract before DB/ledger/payroll/status changes. A separate manager chain dashboard is deferred until real usage shows a concrete gap; do not change ledger/payroll or add DB migrations unless a new contract is approved.

Сначала обязательно прочитай:
1. docs/project-recovery-manifest.md
2. последние датированные разделы docs/current-state.md

Дальше читай только то, что нужно для текущей задачи:
- БД, расписание, финансы, гранты, табели: docs/07-updated-domain-model-after-interview.md и docs/decisions/ADR-002-balance-accounts-ledger.md
- параллельная работа агентов: docs/08-parallel-agent-execution-plan.md
- переносы, занятые окна, отсутствие специалиста и каскадные сдвиги расписания: docs/09-cascade-reschedule-domain-slice.md
- атомарные цепочки переноса нескольких занятий: docs/10-reschedule-chain-dependencies-contract.md
- терминальные статусы и перепроверка планов переноса: docs/11-plan-terminal-status-contract.md
- UX/UI: docs/03-ux-ui-and-implementation-plan.md и релевантные templates/static файлы
- стек/deploy: docs/decisions/ADR-001-django-postgresql-local-first.md и docs/PRODUCTION_DEPLOYMENT.md
- первичные требования интервью: docs/interviews/interview-director-2026-06-23.md

Если доступен Graphify и есть graphify-out/graph.json, сначала используй graphify query как индекс проекта. Graphify semantic extraction был обновлен 2026-07-12 через Gemini после установки недостающего Python-пакета `openai`; граф снова видит свежие `apply_chain()`, `revalidate_chain()` и `create_chain_for_steps()`. При расхождениях свежие docs/current-state.md, docs/07-updated-domain-model-after-interview.md и код остаются источником правды.

Критические правила:
- Не править БД, финансы, расписание или статусы без сверки с актуальной доменной моделью.
- Для следующего среза держаться docs/09-cascade-reschedule-domain-slice.md: план переноса сохраняется как данные, автоматический каскад без действия администратора не запускается; не менять `operations/models.py` и migration chain параллельно несколькими агентами.
- Один агент владеет operations/models.py и migration chain.
- Несколько агентов подключать только по контрактам из docs/08-parallel-agent-execution-plan.md.
- Делать изменения вертикальными срезами: БД/валидация/UI/тесты.
- Не коммитить секреты, production-конфиги, реальные персональные данные и реальные Excel-выгрузки.
- При контрольной точке обновлять только изменившиеся разделы документации и docs/project-recovery-manifest.md, если изменились sources/skills/порядок восстановления.
```

## Как использовать

1. Начать новый чат в этом же проекте.
2. Вставить текст из блока выше.
3. Не прикладывать полную копию проекта: модель должна читать манифест и только нужные документы.
