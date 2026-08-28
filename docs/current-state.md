# Текущее состояние проекта

Дата контрольной точки: 2026-08-29

## Активный этап

Этап: групповые программы, каскады и серии по контракту 61.
Срезы 61A и 61B реализованы и подтверждены GitHub Actions run `33169757672`.
61C-1 и 61C-2 реализованы и подтверждены GitHub Actions run `33182319309`:
устойчивый корень, immutable-редакции,
append-only run/result, сверяемый legacy backfill, PostgreSQL guards и единый
materializer индивидуальных/групповых/create/join серий. 61C-3 реализован и
локально принят: атомарная редакция только будущего состава. Следующий кодовый
подсрез - 61C-4: expand-gate `0060` и `missing_only` реализованы и локально
приняты; expand `0061` и сервисное исполнение `retry_skipped` реализованы и
локально приняты. Следующий подсрез - stop/cancel/withdraw, затем рабочий 61D UI.
Production preflight, health, проверяемый backup/restore, цельная
browser-приемка рабочих ролей, persisted transfer/conversion и приемка
грантового отчета завершены. Безопасный жизненный цикл грантового плана по
`docs/58-grant-plan-versioning-contract.md` реализован и локально принят.
Активный доменный этап описан принятым
`docs/59-grant-fixed-compensation-and-donor-report-snapshot-contract.md`.
59A-1/`0049` и 59A-2/`0050-0052` реализованы одним DB owner: payroll-бюджет,
фиксированный план и общее fixed-начисление прошли SQLite regression,
browser-приемку ролей и PostgreSQL 17 migration/trigger/concurrency gate;
59A принят. 59B-1/`0053` реализован и принят локально/CI: закрытая внутренняя
сверка сохраняется неизменяемым обезличенным снимком с проверяемыми hash,
MVCC-временем данных и цепочкой исправлений. 59B-2/`0054` реализован и
локально/CI принят: фактически переданный файл хранится в отдельном
приватном write-once контуре, а его версии и выдачи фиксируются append-only.
Перед production остаются эксплуатационные решения раздела 10 контракта 60.

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
- `docs/61-group-program-series-lifecycle-contract.md`.
- `docs/decisions/ADR-007-fixed-grant-payroll-and-immutable-donor-report-snapshots.md`.

## Что сделано в текущем срезе

- `0057-0059` добавляют revision/run/result-модель, backfill со смешанным
  preflight и PostgreSQL-защиту immutable/cross-root/temporal контрактов.
- Каждая дата атомарно записывает appointment/participant, compatibility
  occurrence и canonical result; аварийный повтор продолжает тот же run без
  дублей и не выдает частичную запись за завершенную.
- Materializer исполняет immutable revision, выбирает основного специалиста по
  роли, повторно проверяет кабинет, каскады, программы, счета, доступность и
  вместимость под установленным lock-order.
- Legacy occurrence нельзя ошибочно перемаркировать как native между expand и
  backfill. Полностью покрытый legacy-run переигрывается идемпотентно; для
  непокрытых дат реализован явный `missing_only` режим 61C-4a.
- Историческая группа с одним участником сохраняется и читается без создания
  новых занятий; новая native-группа по-прежнему требует минимум двух.
- Runtime DB-role получила только `SELECT/INSERT` на восемь append-only таблиц
  истории серий; реальная проверка ограниченной роли на PostgreSQL прошла.
- Команда 61C-3 под блокировкой корня и `expected_revision_id` атомарно создает
  новую immutable-редакцию состава и переключает текущую проекцию. Она доступна
  только администратору/руководителю, не изменяет уже созданные занятия и
  запрещена для исторической `join_existing`-операции.
- Будущий состав проверяется по типу занятия, ролям специалистов, вместимости
  кабинета, активным программам/каскадам, полному будущему периоду программ,
  счетам и услуге. Выход вне графика фиксируется отдельно по каждому
  специалисту с основанием; строки специалистов блокируются и при редакции,
  и при materialization, а неактивный статус нельзя обойти override-флагом.
- До явных режимов 61C-4 прежняя `initial`-обертка fail-closed отклоняет запуск
  будущей редакции: она не переиспользует operation key и диапазон первой
  редакции для семантически неверного run.
- Expand-gate 61C-4a1 `0060` разрешает цепочке попыток одной даты переходить
  только к той же или более новой редакции. Reverse блокируется после первого
  cross-revision result; чистая PostgreSQL-цепочка `0001-0060` и весь migration
  class серий (`7 passed`) прошли.
- `missing_only` создает новый append-only run по текущей редакции, записывает
  отсутствующие даты единым индивидуальным/групповым materializer, а даты с
  историей фиксирует новой попыткой `unchanged` без изменения Appointment.
  Повтор того же ключа возвращает сохраненный run даже после смены статуса или
  текущей редакции; принятый незавершенный диапазон продолжается по сохраненному
  immutable-снимку, а пересекающийся run новой редакции до этого не принимается.
  После `interrupted` SQLite и PostgreSQL запрещают новый result до `resumed`.
- Expand `0061` добавляет `AppointmentSeriesRetryTarget`: run атомарно фиксирует
  точную вершину цепочки и эффективный `skipped`, число целей сверяется с
  `expected_result_count`. PostgreSQL блокирует изменение/удаление цели, чужое
  продолжение зарезервированной вершины и reverse после появления данных;
  ORM-проверки сохраняют цепочку и reservation на SQLite.
- `materialize_retry_skipped_series` в одной транзакции принимает run и полный
  набор frozen targets, а затем атомарно исполняет каждую дату общим
  индивидуальным/групповым materializer. Старый compatibility occurrence не
  меняется: результатом становится новая попытка `created` либо `skipped` со
  ссылкой `supersedes`. Новый run выбирает immutable-редакцию, применимую ко
  всему диапазону, и не пересекает следующую границу редакции. Повтор того же
  ключа воспроизводит или продолжает run; новый ключ не ветвит зарезервированную
  цепочку, а принятый retry имеет приоритет над новым `missing_only`.
- Аварийное возобновление использует сохраненные targets и revision даже после
  новой текущей редакции, смены роли того же actor и остановки серии. Успешный
  result принимает только Appointment той же серии, даты, услуги и типа;
  чужой Appointment отклоняется до записи immutable-истории.
- Приемка исполнения `retry_skipped`: весь `test_program_series.py` прошел на
  SQLite `62 passed, 29 skipped` и PostgreSQL 17 `91 passed`; включая
  `skipped -> unchanged -> created`, повторный `skipped`, individual/group,
  применимую историческую редакцию, fault recovery, same-key/different-key
  PostgreSQL races и приоритет над `missing_only`. Миграция для сервисного
  подсреза не требуется. Полный актуальный SQLite regression:
  `906 passed, 72 skipped`; пропуски относятся к PostgreSQL-only контрактам.
- Приемка frozen retry targets `0061`: весь `test_program_series.py` прошел на
  SQLite `54 passed, 27 skipped` и PostgreSQL 17 `81 passed`. Отдельно проверены
  fail-closed preflight старых retry runs, deferred count, immutable/reverse
  guards и двухпоточная гонка target-vs-successor; повторный независимый review
  не нашел оставшихся Critical/High. Полный SQLite regression после expand:
  `898 passed, 70 skipped`; пропуски относятся к PostgreSQL-only контрактам.
- Приемка 61C-4a `missing_only`: весь `test_program_series.py` на SQLite
  `53 passed, 23 skipped`, PostgreSQL 17 `76 passed`; включая fault recovery,
  смену редакции/роли/статуса и race-тесты same-key, different-key и
  interrupt-vs-writer. Полный SQLite regression: `897 passed, 66 skipped`.
  Django check, Ruff и `makemigrations --check` прошли.
- Приемка 61C-3: весь `test_program_series.py` на SQLite `48 passed, 19 skipped`,
  PostgreSQL 17 `67 passed`; Django check, Ruff, `makemigrations --check` и
  `git diff --check` прошли. Миграция для этого подсреза не требуется.
- Приемка текущего среза: focused PostgreSQL `59 passed`, focused SQLite
  `41 passed, 19 skipped`; полный regression PostgreSQL `946 passed`, SQLite
  `884 passed, 62 skipped`. Чистая цепочка `0001-0059`, реальная ограниченная
  runtime-role, Django check, Ruff и `makemigrations --check` прошли.
  PostgreSQL-only пропуски на SQLite ожидаемы; единственная warning относится к
  внешней `django_tasks`.
- Миграция `0056` добавляет режим `join_existing`, fingerprint операции и
  защищенную связь occurrence с фактическим участником занятия.
- PostgreSQL trigger проверяет соответствие participant выбранному занятию;
  `PROTECT`, форма и append-only журнал не позволяют тихо удалить joined-факт.
- Администратор видит совместимые будущие группы той же услуги, вместимость,
  остаток плана/оплаты, конфликты и причины недоступности.
- Join работает paid-only, атомарно создает participant с каскадом, счетом и
  монотонным номером; `LedgerEntry` при планировании не создается.
- Повтор POST идемпотентен, а fingerprint `(appointment_id, starts_at)` не дает
  повторно обработать перенесенную цель тем же ключом.
- Порядок блокировок финансового переноса исправлен на
  `ProgramBlock -> BalanceAccount`; join и transfer не образуют deadlock.
- PostgreSQL concurrency покрывает последнее место кабинета, общий последний
  оплаченный слот и один operation key; опасный reverse `0056` блокируется после
  появления join-истории.
- Финальный regression: SQLite `872 passed, 56 skipped` (PostgreSQL-only),
  PostgreSQL 17 `928 passed` без пропусков. Единственная warning относится к
  `django_tasks`, не к коду проекта.
- Desktop и `390x844` Browser QA прошли реальный join без horizontal overflow
  и console warnings/errors; синтетические данные и runserver удалены.
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
- Для 59B-1 добавлены `DonorReport` и append-only `DonorReportSnapshot`,
  migration `0053`, model guards и PostgreSQL triggers против изменения или
  удаления закрытого снимка, истории и текущего указателя в обход сервиса.
  DB-trigger сам пересчитывает canonical SHA-256 и применяет фиксированный
  key/value allowlist схемы v1, поэтому `bulk_create`/raw insert не может
  закрепить fake hash или ПДн в разрешенном JSON-поле.
- Payload и evidence manifest имеют точные allowlist-схемы, стабильный порядок
  строк и канонический SHA-256. В передаваемый JSON входят только псевдонимы
  `SRC-*`, `DON-*`, `SVC-*`, `SPC-*`, `RCP-*` и служебные refs планов;
  evidence с внутренними PK остается только во внутренней записи и не
  экспортируется.
- Preview и закрытие строятся в едином PostgreSQL `REPEATABLE READ` MVCC-срезе.
  Закрывает только руководитель с основанием, review-token и optimistic
  pointer; администратор может строить preview, читать историю и скачивать
  обезличенный JSON, но получает `403` на закрытие.
- Исправление создает следующую версию и сохраняет `supersedes`; повторное
  закрытие неизменившегося payload запрещено. Найденный browser-приемкой
  stale-preview после первой версии закрыт серверным разрешением актуального
  указателя и отдельным регрессионным тестом без ослабления close-lock.

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
- Graphify code index структурно обновлен: `6933` nodes,
  `32917` edges, `366` communities; модели, сервисы и UI 61A-61B
  находятся запросом. Семантический слой не является
  источником истины и не блокирует кодовый граф.
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
- GitHub Actions run `30360743429` для commit `cbb5294` успешно прошел чистую
  PostgreSQL 17 migration chain до `0052`, Django/migration checks, Ruff,
  dependency manifests, Compose/preflight, Linux restore-drill и полный pytest
  (`821 passed`) за `7m42s`.
- 59B-1 migration round-trip `0053 -> 0052 -> 0053` прошел на PostgreSQL 17.
  Полный PostgreSQL baseline до финального hardening дал `854 passed`; после
  hardening весь donor-report набор повторно прошел: PostgreSQL `38 passed`,
  SQLite `30 passed, 8 skipped`. Финальный полный SQLite regression:
  `824 passed, 35 skipped`; пропуски только PostgreSQL-only.
- Browser-приемка 59B-1 выполнена на отдельной обезличенной PostgreSQL-БД:
  руководитель закрыл первый снимок, администратор построил повторный preview,
  прочитал текущую версию и не получил close-команд. Desktop/mobile проверены,
  browser console errors отсутствуют; JSON-download дополнительно покрыт
  view-тестами, так как browser блокирует автоматическую загрузку файла.
- GitHub Actions run `30413567385` для commit `66db14d` успешно прошел чистую
  PostgreSQL 17 migration chain до `0053`, Django/migration checks, Ruff,
  dependency manifests, Compose/preflight, Linux restore-drill и полный pytest
  (`859 passed`) за `8m27s`.
- 59B-2 добавил `DonorReportSubmission` и
  `DonorReportSubmissionAccess`, migration `0054`, private content-addressed
  storage вне `MEDIA_ROOT`, director-only write-path, отдельное
  download-permission администратора и проверяемые exact bytes.
- Append-only цепочка сериализуется через изменяемый `DonorReport`: runtime
  сохраняет `SELECT/INSERT` истории без `UPDATE/DELETE/TRUNCATE`, `TEMPORARY`, записи в
  `django_migrations`, DDL/ownership или role memberships. Service и trigger
  проверены реальным подключением ограниченной роли. `EXECUTE` на функции схемы
  отозван у `PUBLIC` и runtime, кроме трех явно перечисленных validation helpers,
  необходимых trigger-контракту.
- Audit-события дают неизменяемую прикладную атрибуцию, но не
  криптографическую non-repudiation: компрометация runtime SQL может дописать
  событие с существующим actor. До отдельного подписанного writer-контура
  runtime credentials и web-процесс являются явно зафиксированной
  доверительной границей.
- Backup format v2 архивирует DB, media и private artifacts в одном
  окне остановки writers, хранит размер БД и включает metadata в
  checksum. Backup/restore/deploy/migration защищены общим host `flock`, а
  backup публикуется после `fsync` и отклоняет незавершенный restore. Durable
  backup marker и `backup_prod.sh --recover` возвращают исходное состояние web,
  удаляют только неопубликованный partial и требуют health-check. Restore до
  остановки web проверяет tar safety, размер, bytes/inodes томов и
  PostgreSQL, а затем валидирует staged-БД/private root. Recovery валидирует и
  принимает fsynced `.tmp` marker после обрыва между записью и atomic rename.
- Production Compose разделяет migration/restore owner (`POSTGRES_USER`) и
  ограниченную runtime-роль (`POSTGRES_RUNTIME_USER`). Миграции и grants
  выполняет одноразовый `migration` service; web не владеет DB/schema,
  не имеет DDL/role membership и технически проходит role guard. Root-доступ к
  operator-owned `0700` backup вынесен в сетево изолированный
  `archive-maintenance` без production secrets и live volumes. Изолированный
  `volume-init` имеет только media/private volumes для подготовки и recovery;
  web остается non-root.
- Полный локальный Docker restore drill v2 повторно прошел 2026-08-28: exact private
  bytes, поврежденный backup отклонен, abrupt backup/recovery и fault injection
  после DB/file cutover с повторным обрывом
  самого recovery не помешали вернуть исходные DB/media/private; опасный v1 не
  изменил live-данные. Durable `fsync` markers, temp-only recovery и fail-closed
  отказ на неизвестном `.restore-*` не удаляют rollback при обрыве. Обрыв между
  root-распаковкой и передачей владельца оставляет проверяемый root-owned partial,
  который штатный recovery удаляет; private staged/live modes нормализуются до
  `0700/0600`. Неудачный
  deploy после запуска новой web-версии оставляет web/Caddy закрытыми. Новая генерация
  запускается только через restore Compose override, проходит `candidate` web
  health-check и durable `validated` до открытия Caddy и удаления старой.
  Docker sentinel подтвердил, что private-данные не попадают в image; web работает
  не от root.
- Focused 59B-2: SQLite `20 passed, 8 skipped`, PostgreSQL 17 `28 passed`
  без пропусков. Полный SQLite regression после финального hardening:
  `844 passed, 43 skipped`; пропуски относятся к PostgreSQL-only контрактам.
- GitHub Actions run `33139833208` для commit `cdfca64` успешно прошел чистую
  PostgreSQL 17 migration chain, Django/Ruff/dependency/Compose checks,
  Linux restore drill с operator-owned `0700` backup и полный regression:
  `887 passed` за `326.28s`.
- Browser-приемка на обезличенной PostgreSQL-БД прошла замену
  PDF, историю №1/№2, private download и access event. Desktop и
  `390px` не имеют page overflow, browser console чиста; матрица ролей
  дополнительно закреплена view-тестами.

## Следующая работа

1. Реализовать 61C-4b-d stop/cancel/withdraw без изменения фактов прошлого и
   подключить retry/cancel к рабочему интерфейсу администратора.
2. Закрыть 61D: паузу/завершение, переходы программы/каскада, registry,
   руководительские отчеты и полную ролевую приемку серий.
3. Перед production migration chain `0048-0061` выполнить backup v2, `--strict`
   preflight, временно закрыть грантовые записи и подготовить совместимый
   rollback-релиз. После появления истории `0053`/`0054` и series history `0055-0061`
   эти миграции не откатываются:
   rollback сохраняет additive schema, private volume и immutable-историю.
4. Закрыть production-допуск 59B-2: offsite backup, monitoring/alerting, реальный SMTP,
   retention/legal hold, шифрование, malware policy и раздельные DB credentials вне Git.
5. Отдельно согласовать policy возвратов/обратной конвертации; перед пилотом проверить
   mobile-кабинет на физическом телефоне и провести
   обезличенные рабочие сценарии центра.

## Глобальная готовность

Сильная реализованная основа:

- получатели и представители;
- индивидуальные и групповые занятия;
- несколько специалистов и кабинетные ограничения;
- переносы и цепочки плотного расписания;
- participant-level billing, ledger, financial-integrity и persisted transfer/conversion;
- принятый грантовый отчет, табели и payroll;
- неизменяемые донорские снимки, фактические сдачи и приватные выдачи;
- расходы, договоры, шаблоны, акты и согласия;
- импорт сертификатов, связь с балансом и безопасный preflight.

Критические зоны до рабочего production-контура:

- завершенная матрица ролей во всех управленческих действиях;
- остаток 61C-4 и 61D: cancel/withdraw, UI retry и полный жизненный цикл серий;
- production policy и внешняя защита приватных артефактов 59B-2;
- policy возвратов и обратной конвертации;
- offsite backup, monitoring/alerting и реальный SMTP;
- регулярный targeted browser smoke и приемка на физическом телефоне;
- приемка на реальных обезличенных сценариях центра;
- production cutover migration chain `0048-0061` после backup v2/preflight.

## Риски и запреты

- Не давать двум агентам одновременно менять модели и migration chain.
- Не запускать массовые backfill/import apply на production без preflight.
- Не откатывать `0048` командой `migrate 0047`: backfill и история намеренно
  необратимы.
- Не откатывать `0053` после появления снимков: reverse намеренно блокируется,
  production rollback выполняется совместимым приложением без удаления
  immutable-истории.
- Не откатывать `0054` после первой сдачи; совместимый rollback обязан
  сохранить таблицы, private volume и backup format v2.
- Не откатывать `0056` после появления join-серии или outcome `joined`;
  совместимый rollback сохраняет participant и occurrence-историю.
- Не откатывать `0058`/`0059` после native revision/run/result; совместимый
  rollback сохраняет append-only таблицы и использует предыдущий read-path.
- Не откатывать `0060` после cross-revision result: reverse намеренно
  блокируется, а совместимый rollback сохраняет forward-only цепочку попыток.
- Не откатывать `0061` после появления frozen retry target: reverse намеренно
  блокируется, а совместимый rollback сохраняет цель и продолжает принятый run.
- Не объединять разделенные runtime/migration DB-роли: runtime не имеет
  DDL, role membership, право отключать triggers или заменять защитные
  функции `0053`/`0054`; live preflight технически это проверяет.
- Не хранить API-ключи, пароли, production-конфиги и реальные ПДн в Git.
- Не считать SQLite test suite доказательством PostgreSQL concurrency.
- Не возвращаться к очередным микросрезам документов/сертификатов без
  продуктового приоритета из PRD.

Полный журнал до 2026-07-21 сохранен в
`docs/archive/recovery/current-state-through-2026-07-21.md`.
