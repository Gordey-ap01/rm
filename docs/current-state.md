# Текущее состояние проекта

Дата контрольной точки: 2026-07-28

## Активный этап

Этап: приемка полномочий и финансового контура. PostgreSQL-устойчивость
записей расписания и списаний, два доказательных среза полномочий, оба
подэтапа A+B, payroll до факта выплаты и сквозная service-приемка завершены.
Production preflight, health, проверяемый backup/restore, цельная
browser-приемка рабочих ролей, persisted transfer/conversion и приемка
грантового отчета завершены. Безопасный жизненный цикл грантового плана по
`docs/58-grant-plan-versioning-contract.md` реализован и локально принят.
Активный доменный этап описан принятым
`docs/59-grant-fixed-compensation-and-donor-report-snapshot-contract.md`.
59A-1/`0049` и 59A-2/`0050-0052` реализованы одним DB owner: payroll-бюджет,
фиксированный план и общее fixed-начисление прошли SQLite regression,
browser-приемку ролей и PostgreSQL 17 migration/trigger/concurrency gate;
59A локально принят. 59B не начат: следующим отдельным срезом следует снимок
закрытого донорского отчета; параллельно нужны решения по внешней эксплуатации.

Цель этапа: ожидающие согласования и финансовые решения разделены по ролям,
а руководитель имеет окончательный приоритет в управленческих областях без
подмены ответа адресата или неявного финансового утверждения.

Канонические контракты:

- `docs/01-prd.md`, разделы 2-4 и 6;
- `docs/decisions/ADR-003-approval-authority-and-manual-resolution.md`;
- `docs/07-updated-domain-model-after-interview.md`.
- `docs/53-operational-e2e-acceptance-contract.md`.
- `docs/56-persisted-balance-transfer-conversion-contract.md`.
- `docs/57-grant-management-report-acceptance-contract.md`.
- `docs/58-grant-plan-versioning-contract.md`.
- `docs/59-grant-fixed-compensation-and-donor-report-snapshot-contract.md`.
- `docs/decisions/ADR-007-fixed-grant-payroll-and-immutable-donor-report-snapshots.md`.

## Что сделано в текущем срезе

- Старый PRD сохранен в `docs/archive/prd/`; рабочий `docs/01-prd.md` заменен
  канонической версией с readiness matrix.
- Зафиксирована карта полномочий руководителя и администратора.
- Добавлен единый role policy:
  `operations/services/authority.py`.
- Добавлена append-only история решений по `AppointmentConfirmation`:
  `AppointmentConfirmationDecision` и миграция `operations.0044`.
- Администратор и руководитель могут вручную подтвердить или отклонить
  согласование с обязательным основанием.
- Руководитель может переопределить администратора; администратор не может
  переопределить текущее ручное решение руководителя.
- Источник решения и снимок роли согласованы DB-constraint; автор ручного
  решения защищен от физического удаления через `PROTECT`.
- Ответ по публичной ссылке сохраняет явный источник ответа.
- Карточка занятия показывает источник, автора, основание и ручные действия.
- Закрыта обнаруженная уязвимость: обычный специалист больше не может напрямую
  одобрять/отклонять заявки на отсутствие через admin endpoint.
- Добавлен `docs/48-time-off-decision-authority-contract.md`.
- Добавлены `TimeOffRequestDecision`, migration `0045` и доменный сервис
  `operations.services.time_off_decisions`.
- Однодневный отгул/больничный/другое администратор закрывает оперативно.
- Отпуск, изменение графика и период от двух дней после решения администратора
  действуют сразу, но остаются в очереди до контроля руководителя.
- Руководитель может подтвердить или изменить решение администратора;
  администратор не может изменить решение руководителя.
- Work queue, «Завтра» и мобильный кабинет показывают источник, основание,
  эффективный статус и ожидание руководителя.
- Старые `is_staff`-проверки dashboard/кабинета специалиста переведены на
  единый authority policy для групп руководителей и администраторов.
- `supersedes` обоих журналов использует `RESTRICT`: отдельное предыдущее
  решение удалить нельзя, вся история удаляется согласованно с родителем.
- ADR-004 сохраняет типизированные журналы решений; generic workflow отложен
  минимум до третьего доказательного процесса.
- Введен единый lock/recheck path для вместимости кабинета: он применяется в
  формах, calendar API, сервисах, materialization серий и program wizard.
- Два параллельных действия не могут занять последнее место кабинета: строки
  кабинетов блокируются в стабильном порядке, затем вместимость проверяется
  повторно по актуальным snapshot-данным.
- Решение о списании использует один service write-path для формы и сервисов:
  повторный или конкурентный `charge` не создает второй debit.
- `do_not_charge` возвращает остаток correction-проводкой и убирает активную
  связь с занятием, поэтому не возникает ложный факт для payroll/grant.
- Добавлен `.github/workflows/ci.yml`: чистая PostgreSQL 17, миграции, Django
  check, Ruff и полный pytest для каждого push/pull request.
- PostgreSQL-locking не применяется к nullable стороне `LEFT JOIN`: блокировщик
  расписания читает только строку занятия, а certificate/каскады явно
  ограничивают `FOR UPDATE` обязательными строками-владельцами. Миграций и
  изменения продуктовых правил для этого исправления не потребовалось.
- Добавлен `docs/51-payroll-director-approval-contract.md`: администратор
  готовит начисления и черновик листа, руководитель единолично меняет ставки
  и утверждает лист.
- Утверждение блокирует `PayrollSheet`, его строки и начисления в одной
  транзакции; прямой вызов сервиса обычным администратором не обходит роль.
- Авторизованный администратор получает `403` при прямом доступе к ставкам,
  а не цикл редиректов на login; UI показывает его следующий шаг без ложной
  команды утверждения.
- Добавлен `docs/52-payroll-payout-lifecycle-contract.md`: утвержденный лист
  руководитель передает во внутренний контур выплаты, затем фиксирует один
  полный фактический платеж.
- `PayrollPayout` не смешан с `Payment`/ledger получателей и `CenterExpense`;
  он хранит точную сумму, способ, дату, реквизит и автора. `PayrollSheet`
  связан с payout и типизированными lifecycle events через `PROTECT`.
- Переходы `approved -> sent -> paid` выполняются только service-слоем под
  блокировками листа, строк и начислений; второй конкурентный платеж не
  создается. Payout и lifecycle history неизменяемы, Django admin показывает
  их только для чтения.
- Экран листа различает права: администратор видит историю, но не финансовые
  команды; руководитель передает лист в выплату и фиксирует платеж с датой,
  способом и реквизитом.
- Добавлен `docs/53-operational-e2e-acceptance-contract.md` и два сквозных
  теста public service write-paths. Операционный сценарий доказывает путь
  `занятие -> отметка специалиста -> одно списание -> начисление -> табель ->
  полная выплата` без создания `Payment` получателя или `CenterExpense`;
  сценарий отпуска доказывает немедленную защиту расписания решением
  администратора и окончательный приоритет руководителя.
- Добавлен `docs/55-browser-role-acceptance-contract.md`: отдельная локальная
  browser-приемка администратора, руководителя и специалиста на временной
  обезличенной БД. Администратор не видит payroll-команды руководителя,
  руководитель проходит `approved -> sent -> paid` через UI, mobile-кабинет
  специалиста на `390px` не имеет horizontal overflow.
- Добавлены `BalanceTransfer`, migration `operations.0047` и связанная пара
  immutable ledger-проводок. Прямой перенос и конвертация `money -> sessions`
  сохраняют источник, основание, исторический курс и idempotency-key; прошлые
  unlinked transfer-проводки не переписаны.
- Сервис переноса сериализует остатки на PostgreSQL 17. Конвертация не меняет
  лечебный план `planned_sessions`, а увеличивает только доступное по оплате
  количество занятий каскада. Локальная browser-приемка прошла direct,
  конвертацию, 390px и запрет специалисту (`403`).
- Добавлен `docs/57-grant-management-report-acceptance-contract.md`.
  Грантовый отчет разделяет рубли и занятия, показывает opening/closing
  выбранного периода и отдельный текущий остаток; дата debit берется из
  snapshot занятия, а обычных движений — из времени проводки.
- Отчет и CSV доступны администратору и руководителю, но квоты,
  распределения специалистам и выделения получателям изменяет только
  руководитель. Архивный источник и его архивные счета доступны только для
  исторического чтения.
- Факт квоты теперь подтверждается debit-проводкой. Решение `Списать` без
  ledger не попадает в факт и выводится как нарушение целостности. Прямое
  распределение `специалист + количество` не скрывается при наличии квоты
  той же услуги.
- Ledger-балансы агрегируются в БД, статусы, льготы и выделения загружаются
  пакетно; число SQL-запросов не растет на каждый счет или выделение.
  CSV использует штатное quoting и защищен от spreadsheet formula injection.
- Добавлена migration `0048`: устойчивые корни квот и распределений получили
  `current_revision`, а append-only редакции сохраняют полный снимок, автора,
  роль, основание, `decided_at` и цепочку `supersedes`.
- Создание, редакция и закрытие грантового плана выполняются только
  руководителем через `operations.services.grant_plans`; администратор читает
  отчет и историю без управляющих команд.
- Устаревшая форма блокируется `expected_revision_id`; пересекающиеся ставки
  одного специалиста запрещены, количество и период не могут освободить уже
  списанный факт.
- `PayrollAccrual` сохраняет редакцию грантовой ставки. Начисление внутри
  активного листа не переоценивается, а несовпадение строки и начисления
  блокирует утверждение.
- Revision-строки защищены model guard и PostgreSQL-trigger от
  `UPDATE/DELETE`. Команда `check_grant_plan_integrity --strict` проверяет
  текущие проекции, превышения, пересечения и payroll.
- Legacy backfill намеренно необратим и проверяется повторяемым
  `scripts/verify_grant_plan_migration.py`; для production требуется окно
  запрета грантовых записей и совместимый rollback без `migrate 0047`.
- Для 59A-1 добавлены versioned `FundingPayrollBudget` и
  `GrantFixedCompensation`, append-only редакции, stale-token защита,
  director-only write-path и read-only история администратора.
- Migration `0049` добавляет PostgreSQL exclusion/guard/constraint triggers:
  пересечения, fixed/per-session XOR, неизменяемость истории, канонический
  ключ проектной роли и согласованную терминальную текущую проекцию.
- Грантовый отчет показывает бюджеты и фиксированные позиции. Руководитель
  создает, редактирует и закрывает их; администратор видит данные и историю
  без финансовых команд.

## Проверки

- Focused payroll payout tests: `8 passed, 1 skipped` на SQLite; пропущена
  только PostgreSQL-only гонка выплаты, отдельно пройденная на PostgreSQL 17.
- Focused operational acceptance: `2 passed` на SQLite и на свежей PostgreSQL
  базе после миграций `0001-0046`.
- Полный SQLite regression после production среза: `711 passed, 7 skipped`; пропуски — только
  PostgreSQL-only concurrency tests, отдельно пройденные на PostgreSQL 17.
- Ruff: пройден.
- Django system check: пройден.
- `makemigrations --check --dry-run`: изменений не обнаружено.
- PostgreSQL 17: миграции `0001-0047` с пустой базы и полный suite `730 passed`
  в disposable-контейнере; ручные решения, две гонки вместимости, гонка
  списания, гонка выплаты, nullable locking-path и сквозные сценарии проходят.
- Persisted transfer/conversion: migration `0047` применена к чистой локальной
  PostgreSQL 17 базе; focused PostgreSQL tests `10 passed`, SQLite form/view/service
  tests `13 passed, 2 skipped`, полный SQLite regression `721 passed, 9 skipped`,
  browser acceptance выполнена на отдельной обезличенной SQLite-базе.
- Grant/report acceptance: focused SQLite `100 passed`; условная ledger-
  агрегация и тот же целевой набор `100 passed` проверены на disposable
  PostgreSQL 17. Девять PostgreSQL-only конкурентных контрактов отдельно
  прошли; актуальный полный SQLite regression: `735 passed, 9 skipped`.
- Browser-приемка grant report выполнена на обезличенной БД: руководитель
  видит управление и раздельные итоги, администратор видит отчет/CSV без
  управляющих команд и получает `403` по прямому URL; desktop/mobile overflow
  и browser console errors отсутствуют.
- Grant plan lifecycle: `0047 -> 0048` legacy preflight прошел на отдельной
  SQLite-БД; чистая PostgreSQL 17 приняла migration `0048`, focused
  PostgreSQL tests `16 passed` и связанный payroll/report набор `31 passed`.
  Полный SQLite regression: `755 passed, 12 skipped`; пропуски только
  PostgreSQL-only и новые три проверки отдельно пройдены.
- Browser-приемка lifecycle выполнена на обезличенной временной БД:
  руководитель создал редакцию №2 и увидел полную историю; desktop/mobile
  overflow и console errors отсутствуют. Роль администратора подтверждена
  view-тестами и прямыми `403`; прямой browser logout был заблокирован
  политикой браузера и не обходился.
- GitHub Actions run `30154850160` для commit `81611d9` успешно прошел
  PostgreSQL 17 migrations, Django check, Ruff и полный pytest за `5m16s`.
- GitHub Actions run `30156267128` для commit `4c2e09e` успешно прошел чистую
  PostgreSQL 17, миграции, Django check, Ruff и полный pytest за `5m10s`.
- GitHub Actions run `30178291144` для commit `0a3c820` успешно прошел чистую
  PostgreSQL 17: dependency guard, Compose/preflight, Linux restore-drill и
  полный pytest (`718 passed`) за `6m36s`.
- Browser smoke локального сценария администратора: «Списать» создает один
  debit, «Не списывать» очищает ledger занятия и возвращает остаток; mobile
  `390px` без horizontal overflow, console errors отсутствуют.
- Первый GitHub CI run выявил недопустимый `FOR UPDATE` nullable `LEFT JOIN`;
  причина исправлена и воспроизведена полным локальным PostgreSQL suite.
  Повторный GitHub Actions run
  `30144312125` успешно прошел миграции, линтер и полный PostgreSQL suite.
- Playwright browser smoke: work queue администратора, «Завтра» руководителя и
  мобильный кабинет специалиста пройдены; horizontal overflow `0`.
- Playwright payroll smoke: администратор на `390px` не видит ставки или
  утверждение и получает `403` при прямом доступе; руководитель утверждает
  тот же лист. Unexpected console errors отсутствуют.
- Playwright payout smoke: оператор не видит команд передачи/выплаты;
  руководитель фиксирует банковскую выплату через UI, видит реквизит и два
  события истории. На `390px` horizontal overflow `0`, console errors нет.
- Локальный disposable production restore-drill прошел: проверены DB+media,
  отказ поврежденного архива и cleanup временного Compose-проекта. Внешние
  SMTP, monitoring и offsite backup остаются отдельными решениями владельца.
- Цельная browser-приемка ролей от 2026-07-26 прошла на локальной временной
  БД: все экраны ответили `200`, browser console/page errors и HTTP `4xx`/`5xx`
  отсутствовали, desktop и mobile screenshots проверены визуально. Временные
  данные, settings и процесс удалены после запуска.
- Graphify code index сохранен локально без LLM-вызова: `6102` nodes,
  `27116` edges, built from commit `4aeb5962`. Он не содержит последующие
  изменения, включая 59A-1; обновление остается отдельным chore после фиксации
  среза. Семантическое обновление документов не применено: внешний Gemini
  backend недоступен по региону/лимиту, но это не блокирует кодовый граф.
- 59A-1 focused SQLite: `152 passed, 15 skipped`; полный SQLite regression:
  `787 passed, 24 skipped`. Пропуски относятся к PostgreSQL-only проверкам.
  Ruff, Django system check, migration dry-run и `git diff --check` прошли.
  SQLite migration chain `0048 -> 0049 -> 0048` также прошла. После
  PostgreSQL gate полный regression повторно подтвердил тот же результат;
  финальный secret scan измененных/новых файлов не нашел совпадений.
- 59A-1 PostgreSQL 17 gate: чистая migration chain до `0049`, отдельный
  upgrade `0048 -> 0049` и round-trip `0049 -> 0048 -> 0049` прошли.
  Trigger/constraint/concurrency набор: `43 passed` без пропусков; связанный
  grant plan/payroll/report набор: `169 passed` без пропусков.
- GitHub Actions run `30257679071` для commit `19c0bd6` успешно прошел чистую
  PostgreSQL 17 migration chain до `0049`, Django/migration checks, Ruff,
  dependency manifests, Compose/preflight, Linux restore-drill и полный pytest
  (`811 passed`) за `7m35s`.
- Browser-приемка 59A-1 выполнена на временной обезличенной БД: руководитель
  видит write-команды, администратор — только данные и историю; desktop/mobile
  overflow и console errors отсутствуют. Временная БД и сервер удалены.
- 59A-2 разделен на migration `0050` expand, обратимый batched legacy-backfill
  `0051` и `0052` tighten с PostgreSQL-trigger неизменяемости approval event.
  Чистая migration chain до `0052` и populated round-trip
  `0049 -> 0052 -> 0049 -> 0052` прошли на PostgreSQL 17 без изменения сумм и
  статусов старых appointment-строк.
- Общее fixed-начисление создает одну idempotent строку без фиктивного занятия,
  услуги или минут; mixed-лист содержит appointment и fixed-строки, а
  `service_delivery` подавляет дублирующее сдельное начисление той же услуги.
  Draft-лист фиксирует budget commitment, а approval использует канонический
  порядок row locks и сохраняет budget revision/overage в неизменяемом событии.
- 59A-2 локальная приемка: focused PostgreSQL `10 passed`, связанный
  PostgreSQL gate `77 passed`, focused SQLite `7 passed, 3 skipped`; полный
  SQLite regression `794 passed, 27 skipped`. Ruff, Django system check,
  migration dry-run и browser-приемка mixed-листа для руководителя и
  администратора на desktop/mobile прошли; budget report показывает
  consumed/draft/available/forecast без browser console errors.

## Следующая работа

1. Выбрать владельца и поставщиков для offsite backup, monitoring/alerting и
   реального SMTP; хранить секреты только вне Git. Включить branch protection
   после согласования репозитория.
2. Следующим отдельным срезом выполнить 59B-1: закрытый донорский payload.
   59B-2 с фактом сдачи файла начинать только после готовности приватного
   storage.
3. Перед production migration chain `0048-0052` выполнить backup, `--strict`
   preflight, временно закрыть грантовые записи и подготовить совместимый
   rollback-релиз.
4. Отдельно согласовать policy возвратов и обратной конвертации до реализации.
5. Перед пилотом проверить mobile-кабинет на физическом телефоне и провести
   обезличенные рабочие сценарии центра.

## Глобальная готовность

Сильная реализованная основа:

- получатели и представители;
- индивидуальные и групповые занятия;
- несколько специалистов и кабинетные ограничения;
- переносы и цепочки плотного расписания;
- participant-level billing, ledger, financial-integrity и persisted transfer/conversion;
- принятый грантовый отчет, табели и payroll;
- расходы, договоры, шаблоны, акты и согласия;
- импорт сертификатов, связь с балансом и безопасный preflight.

Критические зоны до рабочего production-контура:

- завершенная матрица ролей во всех управленческих действиях;
- закрытый донорский отчет 59B;
- policy возвратов и обратной конвертации;
- offsite backup, monitoring/alerting и реальный SMTP;
- регулярный targeted browser smoke и приемка на физическом телефоне;
- приемка на реальных обезличенных сценариях центра;
- production cutover migration chain `0048-0052` после backup/preflight.

## Риски и запреты

- Не давать двум агентам одновременно менять модели и migration chain.
- Не запускать массовые backfill/import apply на production без preflight.
- Не откатывать `0048` командой `migrate 0047`: backfill и история намеренно
  необратимы.
- Не хранить API-ключи, пароли, production-конфиги и реальные ПДн в Git.
- Не считать SQLite test suite доказательством PostgreSQL concurrency.
- Не возвращаться к очередным микросрезам документов/сертификатов без
  продуктового приоритета из PRD.

Полный журнал до 2026-07-21 сохранен в
`docs/archive/recovery/current-state-through-2026-07-21.md`.
