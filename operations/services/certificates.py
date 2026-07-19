"""Business actions for recipient certificates."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Count, F, Q

from operations.models import BalanceAccount, Certificate, LedgerEntry, Service

CERTIFICATE_BALANCE_PREFLIGHT_LABELS: dict[str, str] = {
    "missing_funding_source": "нет источника финансирования",
    "negative_total_amount": "полная сумма меньше нуля",
    "negative_remaining_amount": "остаток меньше нуля",
    "remaining_exceeds_total": "остаток больше полной суммы",
    "invalid_dates": "дата окончания раньше даты начала",
    "linked_account_wrong_child": "связанный счет другого получателя",
    "linked_account_non_money": "связанный счет не в рублях",
    "linked_account_funding_mismatch": "источник сертификата и счета различается",
}


@dataclass(frozen=True)
class CertificateBalancePreflightReport:
    """Read-only summary for future certificate balance backfill decisions."""

    total_certificates: int
    linked_certificates: int
    unlinked_certificates: int
    backfill_candidates: int
    zero_balance_without_account: int
    issue_counts: dict[str, int]
    sample_certificate_ids: dict[str, tuple[int, ...]]
    duplicate_number_groups: int
    duplicate_number_certificate_count: int
    duplicate_number_samples: tuple[dict[str, Any], ...]

    @property
    def has_issues(self) -> bool:
        return any(self.issue_counts.values()) or self.duplicate_number_certificate_count > 0

    def issue_rows(self) -> list[tuple[str, str, int, tuple[int, ...]]]:
        rows: list[tuple[str, str, int, tuple[int, ...]]] = []
        for code, label in CERTIFICATE_BALANCE_PREFLIGHT_LABELS.items():
            count = self.issue_counts.get(code, 0)
            if count:
                rows.append((code, label, count, self.sample_certificate_ids.get(code, ())))
        return rows


def _actor_or_none(actor: Any):
    return actor if getattr(actor, "is_authenticated", False) else None


def certificate_balance_preflight_report(
    *,
    sample_limit: int = 20,
) -> CertificateBalancePreflightReport:
    """Audit certificate data before any automatic balance-account backfill.

    This function is intentionally read-only. It does not create accounts, ledger entries,
    payments, appointments, payroll facts, grant allocations or status changes.
    """
    sample_limit = max(sample_limit, 0)
    certificates = Certificate.objects.all()
    unlinked = certificates.filter(balance_account__isnull=True)
    valid_date_filter = (
        Q(valid_from__isnull=True)
        | Q(valid_until__isnull=True)
        | Q(valid_until__gte=F("valid_from"))
    )
    issue_querysets = {
        "missing_funding_source": unlinked.filter(funding_source__isnull=True),
        "negative_total_amount": certificates.filter(total_amount__lt=0),
        "negative_remaining_amount": certificates.filter(remaining_amount__lt=0),
        "remaining_exceeds_total": certificates.filter(remaining_amount__gt=F("total_amount")),
        "invalid_dates": certificates.filter(
            valid_from__isnull=False,
            valid_until__isnull=False,
            valid_until__lt=F("valid_from"),
        ),
        "linked_account_wrong_child": certificates.filter(
            balance_account__isnull=False
        ).exclude(balance_account__child_id=F("child_id")),
        "linked_account_non_money": certificates.filter(
            balance_account__isnull=False
        ).exclude(balance_account__unit=BalanceAccount.Unit.MONEY),
        "linked_account_funding_mismatch": certificates.filter(
            balance_account__isnull=False,
            funding_source__isnull=False,
        ).exclude(balance_account__funding_source_id=F("funding_source_id")),
    }
    issue_counts: dict[str, int] = {}
    sample_certificate_ids: dict[str, tuple[int, ...]] = {}
    for code, queryset in issue_querysets.items():
        issue_counts[code] = queryset.count()
        if sample_limit:
            sample_certificate_ids[code] = tuple(
                queryset.order_by("pk").values_list("pk", flat=True)[:sample_limit]
            )
        else:
            sample_certificate_ids[code] = ()

    duplicate_groups = list(
        certificates.exclude(number="")
        .values("child_id", "number")
        .annotate(count=Count("id"))
        .filter(count__gt=1)
        .order_by("child_id", "number")
    )
    duplicate_samples = tuple(
        {
            "child_id": group["child_id"],
            "number": group["number"],
            "count": group["count"],
        }
        for group in duplicate_groups[:sample_limit]
    )
    duplicate_certificate_count = sum(group["count"] for group in duplicate_groups)

    safe_unlinked = unlinked.filter(
        funding_source__isnull=False,
        total_amount__gte=0,
        remaining_amount__lte=F("total_amount"),
    ).filter(valid_date_filter)
    return CertificateBalancePreflightReport(
        total_certificates=certificates.count(),
        linked_certificates=certificates.filter(balance_account__isnull=False).count(),
        unlinked_certificates=unlinked.count(),
        backfill_candidates=safe_unlinked.filter(remaining_amount__gt=0).count(),
        zero_balance_without_account=safe_unlinked.filter(remaining_amount=0).count(),
        issue_counts=issue_counts,
        sample_certificate_ids=sample_certificate_ids,
        duplicate_number_groups=len(duplicate_groups),
        duplicate_number_certificate_count=duplicate_certificate_count,
        duplicate_number_samples=duplicate_samples,
    )


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
