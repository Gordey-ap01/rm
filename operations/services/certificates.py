"""Business actions for recipient certificates."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db import transaction

from operations.models import BalanceAccount, Certificate, LedgerEntry, Service


def _actor_or_none(actor: Any):
    return actor if getattr(actor, "is_authenticated", False) else None


@transaction.atomic
def ensure_certificate_balance_account(
    certificate: Certificate,
    *,
    service_scope: str = BalanceAccount.ServiceScope.ANY,
    service: Service | None = None,
    actor: Any = None,
) -> BalanceAccount:
    """Create and link the spendable money account for a certificate.

    The certificate remains the legal/source record. Spendable balance is represented by
    the linked account ledger and initialized by an opening credit entry.
    """
    locked = (
        Certificate.objects.select_for_update()
        .select_related("child", "funding_source", "balance_account")
        .get(pk=certificate.pk)
    )
    if locked.balance_account_id:
        certificate.balance_account = locked.balance_account
        return locked.balance_account
    if not locked.funding_source_id:
        raise ValueError("Для счета сертификата нужен источник финансирования.")
    if locked.remaining_amount is None or locked.remaining_amount < 0:
        raise ValueError("Остаток сертификата не может быть отрицательным.")

    account = BalanceAccount(
        child=locked.child,
        funding_source=locked.funding_source,
        unit=BalanceAccount.Unit.MONEY,
        service_scope=service_scope,
        service=service,
        initial_amount=Decimal("0"),
        valid_from=locked.valid_from,
        valid_until=locked.valid_until,
        notes=(
            f"Счет сертификата {locked.get_certificate_type_display()} "
            f"№{locked.number or 'б/н'}"
        ),
    )
    account.full_clean()
    account.save()

    if locked.remaining_amount > 0:
        LedgerEntry.objects.create(
            account=account,
            entry_type=LedgerEntry.EntryType.CREDIT,
            amount=locked.remaining_amount,
            reason=(
                f"Открытие баланса сертификата {locked.get_certificate_type_display()} "
                f"№{locked.number or 'б/н'}"
            ),
            created_by=_actor_or_none(actor),
        )

    locked.balance_account = account
    locked.full_clean()
    locked.save(update_fields=["balance_account", "updated_at"])
    certificate.balance_account = account
    return account
