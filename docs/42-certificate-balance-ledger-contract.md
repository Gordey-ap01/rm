# Certificate Balance Ledger Contract

Дата: 2026-07-19

Статус: first vertical slice implemented on 2026-07-19

Основание:
- `docs/38-certificate-payer-source-contract.md`
- `docs/39-recipient-certificate-crud-contract.md`
- `docs/41-certificate-import-write-path-contract.md`
- `docs/decisions/ADR-002-balance-accounts-ledger.md`

## Цель

Связать карточку сертификата с расходуемым балансом так, чтобы сертификаты могли реально использоваться при списании занятий, но остаток не расходился между двумя независимыми местами.

## Ключевое решение

`Certificate` не становится отдельной финансовой книгой. Источник правды по расходуемому остатку - `BalanceAccount.current_balance`, рассчитанный из `LedgerEntry`.

`Certificate` хранит юридические/исходные реквизиты:
- тип;
- номер;
- получатель;
- источник финансирования;
- плательщик;
- полная сумма;
- импортированный/начальный остаток;
- срок действия;
- комментарий.

Если сертификат связан со счетом баланса, текущий доступный остаток для UI и списаний берется со связанного `BalanceAccount`, а не из `Certificate.remaining_amount`.

## Почему так

В системе уже принят ADR-002: объяснимый финансовый факт живет в `BalanceAccount` + `LedgerEntry`. Если параллельно уменьшать `Certificate.remaining_amount`, появится риск двойного учета:

- ledger показывает один остаток;
- сертификат показывает другой остаток;
- непонятно, что считать правдой для руководителя и администратора.

Поэтому `Certificate.remaining_amount` в первом срезе рассматривается как стартовый/import snapshot. Дальше его можно переименовать в отдельной миграции, но не в первом финансовом срезе.

## Предлагаемая доменная модель

### Certificate.balance_account

Additive поле:

- `balance_account`: nullable one-to-one link to `BalanceAccount`.
- `on_delete=PROTECT`.
- `related_name="certificate"`.

Правила:

- linked account must belong to the same `child`;
- linked account must have `unit=money`;
- linked account should use the certificate `funding_source`;
- linked account may be `service_scope=any` or `specific_service`;
- one certificate has at most one balance account;
- one balance account belongs to at most one certificate.

`Certificate.clean()` must reject cross-child and non-money accounts. Funding source mismatch should be rejected when both certificate and account funding sources are set.

### Effective remaining amount

Add property:

- `Certificate.effective_remaining_amount`

Rules:

- if `balance_account_id` exists: return `balance_account.current_balance`;
- else return `remaining_amount`.

`Certificate.is_available` should use `effective_remaining_amount > 0`.

## Account creation service

Add service function, for example:

```python
ensure_certificate_balance_account(
    certificate,
    *,
    service_scope=BalanceAccount.ServiceScope.ANY,
    service=None,
    actor=None,
) -> BalanceAccount
```

Rules:

- atomic;
- lock certificate row;
- if certificate already has `balance_account`, return it unchanged;
- require `certificate.funding_source`;
- require `certificate.remaining_amount >= 0`;
- create `BalanceAccount`:
  - `child=certificate.child`;
  - `funding_source=certificate.funding_source`;
  - `unit=money`;
  - `service_scope` and `service` from request;
  - `initial_amount=0`;
  - validity dates copied from certificate where useful;
  - notes mention certificate type/number.
- create opening `LedgerEntry(CREDIT)` for `certificate.remaining_amount` when it is greater than zero;
- do not create `Payment`;
- link the account back to certificate;
- return account.

Rationale for `initial_amount=0` + opening ledger:
- audit trail is visible in ledger;
- future debits use the same mechanism as all other balances;
- `Payment` is not correct because certificate is entitlement/coverage, not necessarily a received bank payment.

## Appointment billing integration

No special certificate debit table is needed in the first slice.

Existing billing flow already debits a selected `BalanceAccount`:

- sessions account: default debit `-1`;
- money account: default debit `-appointment.service.default_price`;
- explicit amount can override when needed.

Once a certificate balance account exists, administrator can select that account for a paid appointment. The debit creates `LedgerEntry(DEBIT)` and reduces `BalanceAccount.current_balance`.

First implementation must not mutate `Certificate.remaining_amount` during appointment charge.

## UI/UX

Recipient certificate block:

- show imported/start amount and effective current balance;
- if no linked account and certificate has funding source, show hold-to-confirm action "Создать счет баланса";
- if linked account exists, show link to balances and current account status;
- explain briefly that the current остаток is calculated from ledger.

Balance account list/detail:

- identify accounts linked to certificates by certificate type/number;
- keep existing account display for non-certificate accounts.

Appointment billing UI:

- certificate accounts can appear in existing account selector as ordinary money accounts;
- account label should make certificate source recognizable.

## Financial boundaries

Creating a certificate balance account:

- creates `BalanceAccount`;
- creates opening `LedgerEntry(CREDIT)` if opening amount > 0;
- links `Certificate.balance_account`.

It must not create:

- `Payment`;
- appointment debit;
- payroll/accrual;
- grant allocation/fact;
- contract;
- schedule/status changes.

Charging an appointment from a certificate account:

- creates/updates the normal appointment debit `LedgerEntry`;
- updates appointment/participant billing decision as today;
- does not write to `Certificate.remaining_amount`;
- effective certificate balance changes because account ledger changed.

## Migration risks

Low-risk additive:

- nullable one-to-one `Certificate.balance_account`.

Potentially risky and not in first slice:

- renaming `Certificate.remaining_amount`;
- DB check constraints for total/remaining amount if production contains legacy invalid values;
- auto-linking existing certificates to accounts;
- backfilling accounts for all existing certificates;
- unique constraints on certificate number.

Before any backfill:

- count certificates with missing `funding_source`;
- count negative amounts;
- count `remaining_amount > total_amount`;
- count duplicate certificate numbers per child;
- review whether service scope can be inferred.

## Acceptance criteria for first vertical slice

- Add nullable link between `Certificate` and `BalanceAccount`.
- Add validation for same child, money unit and funding-source consistency.
- Add idempotent service for creating linked certificate balance accounts.
- Opening balance is represented by `LedgerEntry(CREDIT)`, not by `Payment`.
- Recipient certificate UI can create/link account through hold-to-confirm.
- Linked certificate shows effective current balance from `BalanceAccount.current_balance`.
- Appointment billing can debit the linked account through existing billing path.
- `Certificate.remaining_amount` is not mutated by account creation or appointment debit.
- Tests prove no `Payment`, payroll, grants, schedules, contracts or statuses are created by account creation.
- Checks: migration dry-run after migration, Ruff, Django check, focused service/view tests, full pytest, Browser QA recipient certificate -> create balance -> appointment debit display where practical.

## Implementation 2026-07-19

Implemented first DB-owner vertical slice:

- Migration `operations.0043_certificate_balance_account_and_more` adds nullable `Certificate.balance_account` one-to-one link to `BalanceAccount`.
- `Certificate.clean()` validates same child, money account unit, funding-source consistency, amount order and date order.
- `Certificate.effective_remaining_amount` returns linked `BalanceAccount.current_balance` when a balance account exists; `Certificate.is_available` now uses the effective amount.
- New service `operations.services.certificates.ensure_certificate_balance_account()` creates the linked money account atomically and idempotently.
- Opening certificate balance is stored as `LedgerEntry(CREDIT)` with `BalanceAccount.initial_amount=0`.
- The service does not create `Payment`, appointments, payroll accruals, grant allocations, contracts, schedules or status changes.
- Recipient detail has a POST-only hold-to-confirm action to create the linked balance account from a certificate with a funding source.
- Linked certificates show effective current balance and a link to the created balance account.
- The certificate panel spans the full recipient detail grid on desktop so the hold action is visible without horizontal scrolling.
- Appointment billing integration uses the existing billing service against the linked money account; tests prove debits reduce effective balance without mutating `Certificate.remaining_amount`.

Verification:

- Ruff touched Python.
- Django check.
- Migration dry-run `No changes detected`.
- Focused service/view/import/contract tests: `81 passed`.
- Full pytest: `648 passed`.
- Playwright Browser QA fallback desktop/mobile on recipient certificate -> hold create balance -> linked account display; synthetic `BQA-CERT-BALANCE-*` data was cleaned and local runserver `8113` stopped.

Still deferred:

- Auto-backfill existing certificates into balance accounts.
- Rename `Certificate.remaining_amount` to an explicit snapshot name.
- Add DB check constraints for amounts after production preflight.
- Add unique constraints for certificate numbers after duplicate analysis.
- Improve global balance list/detail labels for certificate-linked accounts if administrators need that outside recipient detail.

## Parallel-agent rule

- One DB-owner owns `operations/models.py`, migration chain, certificate-balance service and tests.
- UI-only agent may work on templates only after the model/service contract and function names are merged.
- No parallel edits to `operations/models.py`, `operations/services/billing.py`, certificate-balance service or migration files.
