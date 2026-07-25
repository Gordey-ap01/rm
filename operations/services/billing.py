"""Бизнес-логика балансов: решения по списанию, пополнения, переводы."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Q

from operations.models import (
    Appointment,
    AppointmentParticipant,
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


def _sync_appointment_billing_summary(
    appointment: Appointment,
    *,
    participants: list[AppointmentParticipant] | None = None,
) -> None:
    if participants is None:
        participants = list(appointment.participants.select_related("billing_account").order_by("pk"))
    if not participants:
        return
    billing_decision = Appointment.BillingDecision.UNDECIDED
    billing_account = None
    if all(
        participant.billing_decision == Appointment.BillingDecision.DO_NOT_CHARGE
        for participant in participants
    ):
        billing_decision = Appointment.BillingDecision.DO_NOT_CHARGE
    elif all(
        participant.billing_decision == Appointment.BillingDecision.CHARGE
        for participant in participants
    ):
        charged_participants = list(participants)
        if (
            len(charged_participants) == 1
            and charged_participants[0].child_id == appointment.child_id
        ):
            billing_decision = Appointment.BillingDecision.CHARGE
            billing_account = charged_participants[0].billing_account

    Appointment.objects.filter(pk=appointment.pk).update(
        billing_decision=billing_decision,
        billing_account=billing_account,
    )
    appointment.billing_decision = billing_decision
    appointment.billing_account = billing_account


def _single_participant_or_none(
    participants: list[AppointmentParticipant],
) -> AppointmentParticipant | None:
    if len(participants) > 1:
        raise ValueError("Для группового занятия нужно выбрать конкретного участника.")
    return participants[0] if participants else None


def _locked_participant(
    appointment: Appointment,
    participant: AppointmentParticipant | None,
) -> tuple[AppointmentParticipant | None, list[AppointmentParticipant]]:
    participants = list(
        AppointmentParticipant.objects.select_for_update()
        .filter(appointment=appointment)
        .order_by("pk")
    )
    if participant is None:
        return _single_participant_or_none(participants), participants

    for locked_participant in participants:
        if locked_participant.pk == participant.pk:
            return locked_participant, participants
    raise ValueError("Участник не относится к выбранному занятию или был удален.")


def _locked_ledger_entries(
    appointment: Appointment,
    participant: AppointmentParticipant | None,
) -> list[LedgerEntry]:
    entries = LedgerEntry.objects.select_for_update().filter(appointment=appointment)
    if participant is not None:
        entries = entries.filter(
            Q(appointment_participant=participant) | Q(appointment_participant__isnull=True)
        )
    else:
        entries = entries.filter(appointment_participant__isnull=True)
    return list(entries.order_by("pk"))


def _unlink_entries(entries: list[LedgerEntry]) -> int:
    for entry in entries:
        entry.appointment = None
        entry.save(update_fields=["appointment", "updated_at"])
    return len(entries)


def _reverse_and_unlink_entries(
    entries: list[LedgerEntry],
    *,
    appointment: Appointment,
    participant: AppointmentParticipant | None,
    reason: str,
    actor: Any,
) -> int:
    """Keep an append-only audit trail while returning the linked account balance to zero."""
    totals_by_account: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    accounts_by_id: dict[int, BalanceAccount] = {}
    for entry in entries:
        totals_by_account[entry.account_id] += entry.amount
        accounts_by_id[entry.account_id] = entry.account

    for account_id, total in totals_by_account.items():
        if total >= 0:
            continue
        LedgerEntry.objects.create(
            account=accounts_by_id[account_id],
            entry_type=LedgerEntry.EntryType.CORRECTION,
            amount=-total,
            appointment_participant=participant,
            price_snapshot=appointment.service.default_price,
            reason=f"Отмена списания занятия #{appointment.pk}: {reason}",
            created_by=actor,
        )
    return _unlink_entries(entries)


def apply_decision(
    appointment: Appointment,
    *,
    decision: str,
    account: BalanceAccount | None = None,
    amount: Decimal | None = None,
    reason: str = "Решение администратора по занятию.",
    actor: Any = None,
    participant: AppointmentParticipant | None = None,
) -> DecisionResult:
    """Применяет решение по списанию: ``CHARGE`` / ``DO_NOT_CHARGE``.

    Атомарно (одна транзакция):
    - Меняет ``billing_decision`` и ``billing_account`` на занятии.
    - Для ``CHARGE``: создаёт/обновляет ``LedgerEntry`` списания.
    - Для ``DO_NOT_CHARGE``: создает correction-проводку для возврата остатка,
      затем отвязывает исторические ``LedgerEntry`` от занятия.
    """
    if decision not in (Appointment.BillingDecision.CHARGE, Appointment.BillingDecision.DO_NOT_CHARGE):
        raise ValueError(f"Неизвестное решение: {decision!r}")

    with transaction.atomic():
        # Every standard decision locks in this order to serialize one appointment's fact.
        appointment = (
            Appointment.objects.select_for_update().select_related("service").get(pk=appointment.pk)
        )
        participant, participants = _locked_participant(appointment, participant)

        locked_account = None
        if decision == Appointment.BillingDecision.CHARGE:
            if account is None:
                raise ValueError("Для списания нужно передать счёт баланса.")
            locked_account = BalanceAccount.objects.select_for_update().get(pk=account.pk)
            if not locked_account.can_pay_for(appointment.service):
                raise ValueError("Счёт не подходит для этой услуги.")
            expected_child_id = participant.child_id if participant is not None else appointment.child_id
            if locked_account.child_id != expected_child_id:
                raise ValueError("Счёт не принадлежит получателю занятия.")

        ledger_entries = _locked_ledger_entries(appointment, participant)
        if decision == Appointment.BillingDecision.DO_NOT_CHARGE:
            removed = _reverse_and_unlink_entries(
                ledger_entries,
                appointment=appointment,
                participant=participant,
                reason=reason,
                actor=actor,
            )
            if participant is not None:
                participant.billing_decision = Appointment.BillingDecision.DO_NOT_CHARGE
                participant.billing_account = None
                participant.price_snapshot = None
                participant.save(
                    update_fields=[
                        "billing_decision",
                        "billing_account",
                        "price_snapshot",
                        "updated_at",
                    ]
                )
                _sync_appointment_billing_summary(appointment, participants=participants)
            else:
                appointment.billing_decision = Appointment.BillingDecision.DO_NOT_CHARGE
                appointment.billing_account = None
                appointment.save(update_fields=["billing_decision", "billing_account", "updated_at"])
            return DecisionResult(appointment=appointment, entry=None, removed=removed)

        signed = amount if amount is not None else _default_amount_for(locked_account, appointment)
        if signed >= 0:
            raise ValueError("Сумма списания должна быть отрицательной.")

        matching_entries = [entry for entry in ledger_entries if entry.account_id == locked_account.pk]
        entry = matching_entries[0] if matching_entries else None
        removed = _unlink_entries(
            [candidate for candidate in ledger_entries if candidate is not entry]
        )
        if entry is None:
            entry = LedgerEntry.objects.create(
                appointment=appointment,
                account=locked_account,
                entry_type=LedgerEntry.EntryType.DEBIT,
                amount=signed,
                appointment_participant=participant,
                price_snapshot=appointment.service.default_price,
                reason=reason,
                created_by=actor,
            )
        else:
            entry.entry_type = LedgerEntry.EntryType.DEBIT
            entry.amount = signed
            entry.appointment_participant = participant
            entry.price_snapshot = appointment.service.default_price
            entry.reason = reason
            entry.created_by = actor
            entry.save(
                update_fields=[
                    "entry_type",
                    "amount",
                    "appointment_participant",
                    "price_snapshot",
                    "reason",
                    "created_by",
                    "updated_at",
                ]
            )

        if participant is not None:
            participant.billing_decision = Appointment.BillingDecision.CHARGE
            participant.billing_account = locked_account
            participant.price_snapshot = appointment.service.default_price
            participant.save(
                update_fields=[
                    "billing_decision",
                    "billing_account",
                    "price_snapshot",
                    "updated_at",
                ]
            )
            _sync_appointment_billing_summary(appointment, participants=participants)
        else:
            appointment.billing_decision = Appointment.BillingDecision.CHARGE
            appointment.billing_account = locked_account
            appointment.save(update_fields=["billing_decision", "billing_account", "updated_at"])
        return DecisionResult(appointment=appointment, entry=entry, removed=removed)


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
    if from_account.unit != to_account.unit:
        raise ValueError("Нельзя переводить между счетами с разными единицами учета.")
    if amount > from_account.current_balance:
        raise ValueError("Недостаточно средств на исходном счете.")

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
