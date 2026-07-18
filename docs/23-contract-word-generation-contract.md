# Контракт: Word-генерация договоров

Дата: 2026-07-18

Статус: выполнен 2026-07-18

## Контекст

В `docs/21-expenses-assets-contracts-contract.md` закрыты реестр договоров, PDF-download и Excel/CSV/TSV preview без записи в БД. В `docs/22-category-counterparty-directory-contract.md` добавлен рабочий UI для категорий расходов и контрагентов. Следующий безопасный шаг: дать администратору генерацию редактируемого Word-файла договора из структурной карточки и шаблона, не меняя финансы, статусы занятий и доменную модель БД.

## Цель

Администратор должен уметь:

- сформировать `.docx` для договора с получателем или договора пожертвования;
- использовать загруженный `ContractTemplate.file`, если он есть;
- получить системный базовый `.docx`, если файл шаблона не загружен;
- сохранить сгенерированный файл как `Document` и привязать его к договору, когда это безопасно;
- скачать сгенерированный файл для ручной проверки и юридической правки.

## Границы

Входит:

- runtime-зависимость `python-docx`;
- сервис генерации `.docx` для `ServiceContract` и `DonationContract`;
- placeholder replacement для загруженных `.docx` шаблонов;
- системный fallback-шаблон без загруженного файла;
- POST-действия "Сформировать Word" в реестре договоров;
- создание `Document(category=contract)` и привязка к договору только для договора с получателем;
- download сформированного `.docx`;
- tests и Browser QA desktop/mobile.

Не входит:

- новые модели или миграции;
- изменение юридических реквизитов центра и контрагентов;
- real Excel import write-path;
- approve/pay расходов;
- создание платежей, проводок, балансов, списаний, payroll или grant фактов;
- автоматическая отправка договора представителям.

## Правила

- Генерация Word не меняет `status` договора.
- Генерация Word не создает финансовые факты.
- Если договор с получателем уже имеет `document`, повторная генерация заменяет файл этого `Document`, а не создает дубль.
- Для договора пожертвования в текущей модели `Document` обязателен `child`, поэтому Word-файл скачивается без сохранения в `Document`; привязку пожертвований к файлам нужно решить отдельной модельной правкой.
- Placeholder names должны быть стабильными и документированными в UI/контракте.
- Ошибки чтения шаблона показываются администратору и не портят существующий связанный документ.

## Placeholder v1

- `{{ contract.number }}`
- `{{ contract.signed_on }}`
- `{{ contract.valid_from }}`
- `{{ contract.valid_until }}`
- `{{ contract.status }}`
- `{{ contract.type }}`
- `{{ contract.template }}`
- `{{ child.full_name }}`
- `{{ child.birth_date }}`
- `{{ representative.full_name }}`
- `{{ counterparty.name }}`
- `{{ counterparty.inn }}`
- `{{ counterparty.kpp }}`
- `{{ counterparty.ogrn }}`
- `{{ counterparty.legal_address }}`
- `{{ counterparty.postal_address }}`
- `{{ counterparty.bank_details }}`
- `{{ funding_source.name }}`
- `{{ donation.amount_limit }}`

## Acceptance Criteria

- Администратор видит действие "Word" у договоров с получателями и договоров пожертвования.
- Для договора с получателем POST создает или обновляет связанный `Document` с `.docx` и затем позволяет скачать файл.
- Для договора пожертвования POST возвращает `.docx` download без создания `Document`, пока модель документов не поддерживает договоры без получателя.
- Генерация из `.docx` шаблона заменяет поддержанные placeholders.
- Fallback-шаблон формирует валидный `.docx`, когда `ContractTemplate.file` пустой.
- Tests доказывают отсутствие `LedgerEntry`, `BalanceAccount`, `Payment`, payroll и grant-фактов.
- Browser QA desktop/mobile проходит без горизонтального overflow и console/page errors.

## Следующие срезы после выполнения

- модельное расширение документов для договоров пожертвования без `child`;
- юридические реквизиты центра и контрагентов;
- approve/pay расходов центра;
- import write-path с записью в БД.
