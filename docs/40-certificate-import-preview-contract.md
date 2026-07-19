# Certificate Import Preview Contract

Дата: 2026-07-19

Статус: no-migration read-only срез

Основание:
- `docs/38-certificate-payer-source-contract.md`
- `docs/39-recipient-certificate-crud-contract.md`
- `docs/project-recovery-manifest.md`

## Цель

Добавить безопасный предпросмотр Excel/CSV для сертификатов после стабилизации модели плательщика и источника. Администратор должен заранее увидеть ошибки строк и справочников, не создавая сертификаты и не меняя финансовые факты.

## Решение среза

- Расширить общий экран `/contracts/import-preview/` типом `Сертификаты`.
- Поддержать `.xlsx`, `.csv`, `.tsv` через существующий read-only parser.
- Проверять строки сертификатов по существующим получателям, источникам финансирования и представителям-плательщикам.
- Показывать результат в общей таблице распознанных колонок и строк.

## Проверки строк

- Получатель ищется по фамилии и имени; отчество и дата рождения уточняют поиск, если заполнены.
- Тип сертификата должен соответствовать `Certificate.CertificateType`.
- Полная сумма и остаток обязательны, не могут быть отрицательными.
- Остаток не может быть больше полной суммы.
- Дата окончания не может быть раньше даты начала.
- Источник финансирования должен уже существовать, если указан.
- Представитель-плательщик ищется только среди представителей найденного получателя.
- Дубликат номера сертификата у получателя показывается предупреждением.

## Не входит

- Реальное создание `Certificate`.
- Изменение остатков сертификатов.
- Создание или изменение `BalanceAccount`, `Payment`, `LedgerEntry`, payroll и списаний занятий.
- Автоматическое создание получателей, представителей или источников финансирования.
- Импорт файлов из `docshablon/` в БД.

## Acceptance criteria

- GET `/contracts/import-preview/` показывает тип `Сертификаты`.
- POST с типом `certificates` разбирает CSV/XLSX и показывает строки.
- Валидная строка проходит preview.
- Ошибочная строка показывает ошибки по суммам, датам, справочникам или плательщику.
- Preview не создает записей `Certificate`.
- `makemigrations --check --dry-run` reports no changes.
- Checks: Ruff, Django check, focused import-preview tests, full pytest, Browser QA upload preview.

## Реализация 2026-07-19

- `operations.services.import_preview` расширен типом `CERTIFICATE_IMPORT`.
- Общий `/contracts/import-preview/` теперь показывает и принимает тип `Сертификаты`.
- `ContractImportPreviewForm` получил вариант `certificates`.
- Поддержаны русские заголовки и English aliases для автоматического/технического CSV.
- Валидация проверяет получателя, тип сертификата, суммы, даты, источник и представителя-плательщика.
- Preview остается read-only: сервисные и view-тесты проверяют, что `Certificate` не создается.
- Проверки: Ruff touched Python и полный `operations`, Django check, migration dry-run `No changes detected`, focused import-preview tests `9 passed`, full pytest `632 passed`, Playwright desktop/mobile QA upload preview.
- Browser QA synthetic `BQA-CERTIMPORT*` data cleaned; local runserver `8108` stopped.
