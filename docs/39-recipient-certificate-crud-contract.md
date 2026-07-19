# Recipient Certificate CRUD Contract

Дата: 2026-07-19

Статус: no-migration application/UI срез выполнен

Основание:
- `docs/38-certificate-payer-source-contract.md`
- `docs/30-certificate-contract-link-contract.md`
- `docs/03-ux-ui-and-implementation-plan.md`

## Цель

Сделать сертификаты управляемыми из рабочей карточки получателя, а не только через Django admin. Администратор должен завести сертификат, источник, плательщика, суммы и срок, после чего договор по сертификату сможет использовать эти реквизиты в шаблоне.

## Решение среза

- Добавить `CertificateForm`, где получатель фиксируется маршрутом карточки.
- Фильтровать `payer_representative` только представителями выбранного получателя.
- Добавить маршруты:
  - `/recipients/<child_id>/certificates/new/`
  - `/certificates/<pk>/edit/`
- Показать сертификаты в карточке получателя с типом, номером, источником, плательщиком, остатком и сроком.
- Добавить кнопку создания сертификата в карточке получателя.
- Валидировать в форме неотрицательные суммы, остаток не больше полной суммы и порядок дат.

## Не входит

- Новые поля БД или миграции.
- Списание сертификата занятиями.
- Автоматическое создание `BalanceAccount`, `Payment`, `LedgerEntry`.
- Excel-import сертификатов.
- Отдельный общий реестр сертификатов.
- Архивирование/удаление сертификатов.

## Acceptance criteria

- Recipient detail shows certificate table and create/edit actions.
- Create form saves certificate for the route recipient only.
- Payer representative choices are limited to the recipient's representatives.
- POST with a payer representative from another recipient is rejected.
- Editing certificate source/payer/amounts does not create financial facts.
- `makemigrations --check --dry-run` reports no changes.
- Checks: Ruff, Django check, focused recipient tests, full pytest, Browser QA for recipient detail/create/edit.

## Реализация 2026-07-19

- `CertificateForm` добавлен в `operations/forms.py`; получатель фиксируется через `child` из route/view.
- `payer_representative` фильтруется по представителям выбранного получателя; чужой представитель отклоняется queryset/form validation.
- Форма валидирует неотрицательные суммы, остаток не больше полной суммы и порядок дат.
- Добавлены routes/views `recipient_certificate_create` и `recipient_certificate_edit`.
- Карточка получателя показывает кнопку создания сертификата и таблицу `recipient-certificates-table` с типом, номером, источником, плательщиком, остатком, сроком и edit action.
- Проверки: Ruff touched Python и `operations`, Django check, migration dry-run `No changes detected`, focused recipient tests `6 passed`, full pytest `630 passed`, Playwright desktop/mobile QA для detail/create/edit.
- Browser QA synthetic `BQA-CERTCRUD*` data cleaned; local runserver `8107` stopped.
- Graphify code-index после среза: `5229` nodes / `22978` edges. Semantic extraction не запускалась; raw `docshablon/` остается ignored/private.
