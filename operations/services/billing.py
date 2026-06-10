"""Бизнес-логика балансов: решения по списанию, пополнения, переводы."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.db import transaction

from operations.models import (
    Appointment,
    BalanceAccount,
    Child,
    FundingSource,
    LedgerEntry,
    Payment,
)


@dataclass(frozen=True)
class DecisionResult:
    appointment: Appointment
    entry: LedgerEntry | None
    removed: int  # сколько старых ledger-записей было отвязано/удалено


def _default_amount_for(account: BalanceAccount, appointment: Appointment) -> Decimal:
    if account.unit == BalanceAccount.Unit.SESSIONS:
        return Decimal("-1")
    return -appointment.service.default_price


def apply_decision(
    appointment: Appointment,
    *,
    decision: str,
    account: BalanceAccount | None = None,
    amount: Decimal | None = None,
    reason: str = "Решение администратора по занятию.",
    actor: Any = None,
) -> DecisionResult:
    """Применяет решение по списанию: ``CHARGE`` / ``DO_NOT_CHARGE``.

    Атомарно (одна транзакция):
    - Меняет ``billing_decision`` и ``billing_account`` на занятии.
    - Для ``CHARGE``: создаёт/обновляет ``LedgerEntry`` списания.
    - Для ``DO_NOT_CHARGE``: отвязывает все ``LedgerEntry`` от занятия
      (записи сохраняются в журнале для аудита).
    """
    if decision not in (Appointment.BillingDecision.CHARGE, Appointment.BillingDecision.DO_NOT_CHARGE):
        raise ValueError(f"Неизвестное решение: {decision!r}")

    with transaction.atomic():
        appointment.billing_decision = decision
        appointment.billing_account = account if decision == Appointment.BillingDecision.CHARGE else None
        appointment.save(update_fields=["billing_decision", "billing_account", "updated_at"])

        if decision == Appointment.BillingDecision.DO_NOT_CHARGE:
            removed = LedgerEntry.objects.filter(appointment=appointment).update(appointment=None)
            return DecisionResult(appointment=appointment, entry=None, removed=removed)

        if account is None:
            raise ValueError("Для списания нужно передать счёт баланса.")
        if not account.can_pay_for(appointment.service):
            raise ValueError("Счёт не подходит для этой услуги.")
        if account.unit == BalanceAccount.Unit.MONEY and account.child_id != appointment.child_id:
            raise ValueError("Счёт не принадлежит получателю занятия.")

        signed = amount if amount is not None else _default_amount_for(account, appointment)
        if signed >= 0:
            raise ValueError("Сумма списания должна быть отрицательной.")

        # Снимем связь с любыми ledger-записями по ДРУГОМУ счёту.
        LedgerEntry.objects.filter(appointment=appointment).exclude(account=account).update(appointment=None)

        entry, _ = LedgerEntry.objects.update_or_create(
            appointment=appointment,
            account=account,
            defaults={
                "entry_type": LedgerEntry.EntryType.DEBIT,
                "amount": signed,
                "reason": reason,
                "created_by": actor,
            },
        )
        return DecisionResult(appointment=appointment, entry=entry, removed=0)


@transaction.atomic
def top_up_account(
    account: BalanceAccount,
    *,
    amount: Decimal,
    method: str = Payment.Method.GRANT_TRANSFER,
    paid_at: Any = None,
    reference: str = "",
    comment: str = "",
    actor: Any = None,
    create_ledger: bool = True,
) -> Payment:
    """Создаёт ``Payment`` и (опционально) ``LedgerEntry`` с пополнением."""
    if amount is None or amount <= 0:
        raise ValueError("Сумма пополнения должна быть положительной.")

    payment_kwargs: dict[str, Any] = {
        "balance_account": account,
        "amount": amount,
        "method": method,
        "reference": reference,
        "comment": comment,
        "created_by": actor,
    }
    if paid_at is not None:
        payment_kwargs["paid_at"] = paid_at
    payment = Payment.objects.create(**payment_kwargs)
    if create_ledger:
        LedgerEntry.objects.create(
            account=account,
            entry_type=LedgerEntry.EntryType.CREDIT,
            amount=amount,
            appointment=None,
            reason=f"Пополнение #{payment.pk} ({payment.get_method_display()})",
            created_by=actor,
        )
    return payment


@transaction.atomic
def transfer_between_accounts(
    *,
    from_account: BalanceAccount,
    to_account: BalanceAccount,
    amount: Decimal,
    reason: str,
    actor: Any = None,
) -> tuple[LedgerEntry, LedgerEntry]:
    """Переводит между счетами одного ребёнка или между детьми (если политика источника разрешает).

    Создаёт пару ``LedgerEntry``: списание с ``from_account`` и пополнение ``to_account``.
    """
    if amount is None or amount <= 0:
        raise ValueError("Сумма перевода должна быть положительной.")
    if from_account.pk == to_account.pk:
        raise ValueError("Нельзя переводить в тот же счёт.")

    source = from_account.funding_source
    if source.transfer_policy == FundingSource.TransferPolicy.NOT_TRANSFERABLE:
        raise ValueError("Источник финансирования не разрешает передачу остатков.")
    if (
        source.transfer_policy == FundingSource.TransferPolicy.WITHIN_CHILD
        and from_account.child_id != to_account.child_id
    ):
        raise ValueError("Этот источник можно передавать только в пределах одного получателя.")

    debit = LedgerEntry.objects.create(
        account=from_account,
        entry_type=LedgerEntry.EntryType.TRANSFER,
        amount=-amount,
        reason=reason,
        created_by=actor,
    )
    credit = LedgerEntry.objects.create(
        account=to_account,
        entry_type=LedgerEntry.EntryType.TRANSFER,
        amount=amount,
        reason=reason,
        created_by=actor,
    )
    return debit, credit


def summarize_ledger_by_account(child: Child) -> dict[int, Decimal]:
    """Возвращает текущие остатки по счетам получателя (используется в отчётах)."""
    totals: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    qs = LedgerEntry.objects.filter(account__child=child).values_list("account_id", "amount")
    for account_id, amount in qs:
        totals[account_id] += amount
    return dict(totals)
