# Template Placeholder Expansion v2

Дата: 2026-07-18

Статус: контракт на безопасный кодовый срез без миграций

Основание: `docs/24-document-template-source-inventory.md`

## Цель

Расширить контур Word-шаблонов договоров так, чтобы администратор мог готовить `.docx` на основе реальных локальных образцов и видеть, какие `{{ placeholder }}` поддерживаются системой сейчас или зарезервированы под ближайшие DB-срезы.

## Входит в срез

- Единый список плейсхолдеров для шаблонов договоров в `operations.services.contract_documents`.
- Подстановка новых плейсхолдеров, которые уже можно получить из текущей БД: телефон/email представителя, связь с получателем, телефон/email получателя, тип/период источника финансирования, расширенные поля контрагента.
- Blank fallback для юридических полей, которых в БД еще нет: реквизиты центра, паспорт/адрес представителя, адрес получателя, спецификация услуг, сертификат, структурированные банковские реквизиты.
- Справка по плейсхолдерам на форме `ContractTemplate`.
- Валидация загрузки нового файла шаблона: поддерживается `.docx`; legacy `.doc` сначала конвертировать вручную.

## Не входит в срез

- Новые модели и миграции.
- Сохранение donation contract Word-файла как `Document`.
- Реквизиты центра, паспортные данные, адреса, спецификации услуг и сертификаты как структурированные данные.
- Генерация согласий, B2B-договоров и актов.
- Любые изменения в `BalanceAccount`, `LedgerEntry`, `Payment`, billing, payroll, grants, appointment statuses или расписании.
- Конвертация `.doc` в `.docx` внутри приложения.

## Acceptance Criteria

- Существующие service/donation Word downloads продолжают работать.
- `.docx`-шаблон с v2-плейсхолдерами не оставляет поддержанные `{{ key }}` в итоговом документе.
- Плейсхолдеры будущих DB-срезов заменяются на `_______________`, а не падают и не остаются в тексте.
- Новая загрузка `.doc`/`.txt` как файла шаблона отклоняется формой с понятным сообщением.
- Форма шаблона показывает группы плейсхолдеров и предупреждение про `.docx`.
- Проверки: Ruff по измененным Python-файлам, Django check, migration dry-run `No changes detected`, focused contract tests, related view tests, full pytest при возможности, Browser QA для формы шаблона desktop/mobile.

## Implementation 2026-07-18

Status: implemented as a no-migration vertical slice.

Implemented:
- `operations.services.contract_documents` now has a single grouped placeholder catalog for contract Word templates.
- Service and donation contract `.docx` generation fills all supported v2 placeholders and replaces future-DB placeholders with `_______________` instead of leaving raw `{{ key }}` tokens.
- `ContractTemplateForm` rejects newly uploaded non-`.docx` template files and shows an explicit `.docx` conversion note.
- Contract template create/edit pages show the grouped placeholder reference in the sidebar.
- Existing service/donation Word downloads continue to work; donation contracts still download without creating `Document` because `Document` currently requires `Child`.

Verified:
- Ruff format/check on touched Python files.
- `manage.py check --settings=rehab_center.settings_test`.
- `makemigrations --check --dry-run --settings=rehab_center.settings_test` returned `No changes detected`.
- Focused/related contract tests: `24 passed`.
- Full test suite: `561 passed`, with the existing django-tasks warning.
- Python Playwright QA for `/contracts/templates/new/` on desktop 1365x900 and mobile 390x844: placeholder groups/tokens visible after expansion, `.docx` help visible, no console warnings/errors, no horizontal overflow.

Still out of scope:
- Donation-contract `Document` storage without mandatory `Child`.
- Center legal profile, representative passport/address, child address, structured service specs, certificate links, B2B contracts, consents, acts and signed legal snapshots.
- Any ledger/balance/payment/payroll/billing/grant/schedule/status semantic changes.
