# Immutable Contract Signed File Archive

Дата: 2026-07-18

Статус: DB-owner срез выполнен

Основание:
- `docs/24-document-template-source-inventory.md`
- `docs/26-legal-document-targets-and-center-profile-contract.md`
- `docs/27-legal-template-families-contract.md`
- `docs/30-certificate-contract-link-contract.md`

## Проблема

`ContractLegalSnapshot` уже фиксирует реквизиты договора при Word-генерации, но текущий `Document` остается рабочим файлом: повторная генерация обновляет тот же файл и snapshot. Для юридического контура нужен отдельный архив подписанных файлов, где каждая сохраненная подписанная версия имеет неизменяемую запись, checksum и копию snapshot на момент архивации.

## Решение первого среза

- Добавить отдельную модель `ContractSignedFile`.
- Один архивный файл относится ровно к одному договору: `ServiceContract` или `DonationContract`.
- Архивная запись хранит:
  - тип договора;
  - ссылку на service/donation договор;
  - исходный generated `Document`, если есть;
  - файл подписанной версии;
  - исходное имя файла;
  - размер;
  - SHA-256;
  - дату подписания;
  - кто загрузил;
  - статус `active` или `void`;
  - frozen JSON snapshots договора, центра, получателя, представителя, контрагента, источника финансирования и шаблона.
- UI первого среза: в реестре договоров администратор может заархивировать текущий сгенерированный Word-файл как подписанную версию и скачать последнюю архивную версию.
- Для donation и service договоров используется текущий `Document.file`; если файл договора еще не сгенерирован, действие показывает ошибку и не создает архив.

## Не входит

- Электронная подпись, ЭДО и криптографическая подпись.
- Загрузка скана с компьютера вместо текущего generated `Document.file`.
- Акты выполненных услуг.
- B2B-договоры и согласия.
- Автоматическое изменение статусов договора при архивации.
- Любые `LedgerEntry`, `BalanceAccount`, `Payment`, billing, payroll, grant, certificate-balance, schedule или appointment-status semantics.
- Удаление или перезапись ранее созданных архивных записей через product UI.

## Инварианты

- `ContractSignedFile` имеет ровно одну contract-ссылку по `contract_kind`.
- Файл обязателен.
- SHA-256 и размер считаются из фактически сохраненного файла.
- При архивации копируется snapshot из текущего `ContractLegalSnapshot`, если он есть; если snapshot отсутствует, действие запрещено, потому что архив без реквизитов юридически слабый.
- Повторная Word-генерация договора не меняет уже созданные `ContractSignedFile`.
- Void-статус только помечает запись, но не удаляет файл и не создает финансовых фактов.

## Acceptance criteria

- В service/donation строке реестра видна последняя активная подписанная версия, если она есть.
- Кнопка архивации доступна только как POST и требует существующий generated `Document` с `ContractLegalSnapshot`.
- Архивация service договора создает `ContractSignedFile` с `contract_kind=service`, файлом, checksum, размером, snapshot-copy и uploaded_by.
- Архивация donation договора создает аналогичную запись `contract_kind=donation`.
- Архивный файл скачивается отдельным read-only маршрутом.
- Повторная генерация Word после архивации не меняет checksum старой архивной записи.
- Полный pytest проходит.
- Browser QA проверяет реестр desktop/mobile, archive action и download link.

## Агентские правила

- Срез делает один DB-owner агент: меняются `operations/models.py` и migration chain.
- Параллельные агенты могут только read-only review или подготовку `.docx`-шаблонов вне git.
- `docshablon/` не коммитить и не отправлять в Graphify semantic extraction.

## Реализация 2026-07-18

- Миграция `operations.0035_contractsignedfile` добавляет `ContractSignedFile`.
- Архивная запись связывается ровно с одним service/donation договором, хранит исходный `Document`, файл, исходное имя, content type, размер, SHA-256, дату подписания, автора загрузки, статус и frozen snapshot-copy.
- `ContractSignedFile.save()` запрещает менять архивные поля после создания; разрешено только аннулирование через статус/причину.
- Сервис `archive_service_contract_signed_file()` / `archive_donation_contract_signed_file()` требует существующий generated `Document` с `ContractLegalSnapshot`, копирует файл и не трогает финансы.
- `/contracts/` показывает последнюю активную архивную версию и POST-кнопку фиксации для service/donation договоров со snapshot.
- Добавлены маршруты архивации и read-only download архивного файла.
- Проверки: Ruff, Django check, migration dry-run `No changes detected`, focused contract tests `49 passed`, full pytest `597 passed`, in-app Browser QA desktop/mobile для service/donation archive action и архивных ссылок; download event в in-app Browser не поддерживается, download route покрыт Django test.
