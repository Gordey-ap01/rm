"""Бизнес-логика балансов: решения по списанию, пополнения, переводы."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from django.db import IntegrityError, transaction
from django.db.models import Q

from operations.models import (
    Appointment,
    AppointmentParticipant,
    AppointmentSeriesCancellationResult,
    BalanceAccount,
    BalanceTransfer,
    Child,
    FundingSource,
    LedgerEntry,
    Payment,
    ProgramBlock,
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


def _ledger_entries(
    appointment: Appointment,
    participant: AppointmentParticipant | None,
):
    entries = LedgerEntry.objects.filter(appointment=appointment)
    if participant is not None:
        entries = entries.filter(
            Q(appointment_participant=participant) | Q(appointment_participant__isnull=True)
        )
    else:
        entries = entries.filter(appointment_participant__isnull=True)
    return entries


def _locked_ledger_entries(
    appointment: Appointment,
    participant: AppointmentParticipant | None,
) -> list[LedgerEntry]:
    return list(
        _ledger_entries(appointment, participant).select_for_update().order_by("pk")
    )


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
        if (
            decision == Appointment.BillingDecision.CHARGE
            and AppointmentSeriesCancellationResult.objects.filter(
                appointment=appointment,
                outcome=AppointmentSeriesCancellationResult.Outcome.CANCELLED,
            ).exists()
        ):
            raise ValueError(
                "Нельзя списать оплату после массовой отмены будущего занятия серией."
            )
        participant, participants = _locked_participant(appointment, participant)
        block_ids = {
            block_id
            for block_id in [
                appointment.program_block_id,
                participant.program_block_id if participant is not None else None,
            ]
            if block_id is not None
        }
        list(
            ProgramBlock.objects.select_for_update()
            .filter(pk__in=block_ids)
            .order_by("pk")
        )

        if decision == Appointment.BillingDecision.CHARGE and account is None:
            raise ValueError("Для списания нужно передать счёт баланса.")
        account_ids = {
            account_id
            for account_id in [
                appointment.billing_account_id,
                participant.billing_account_id if participant is not None else None,
                account.pk if account is not None else None,
            ]
            if account_id is not None
        }
        account_ids.update(
            _ledger_entries(appointment, participant).values_list(
                "account_id", flat=True
            )
        )
        locked_accounts = {
            locked.pk: locked
            for locked in BalanceAccount.all_objects.select_for_update()
            .filter(pk__in=account_ids)
            .order_by("pk")
        }

        locked_account = None
        if decision == Appointment.BillingDecision.CHARGE:
            locked_account = locked_accounts.get(account.pk)
            if locked_account is None:
                raise ValueError("Счёт баланса больше не существует.")
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


def _as_idempotency_key(value: UUID | str | None) -> UUID:
    if value is None:
        return uuid4()
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("Ключ идемпотентности имеет неверный формат.") from exc


def _locked_transfer_accounts(
    from_account: BalanceAccount,
    to_account: BalanceAccount,
) -> tuple[BalanceAccount, BalanceAccount]:
    if from_account.pk == to_account.pk:
        raise ValueError("Нельзя переводить в тот же счёт.")
    account_ids = sorted((from_account.pk, to_account.pk))
    locked = {
        account.pk: account
        for account in BalanceAccount.objects.select_for_update(of=("self",))
        .select_related("funding_source", "service")
        .filter(pk__in=account_ids)
        .order_by("pk")
    }
    if len(locked) != 2:
        raise ValueError("Один из счетов больше не существует.")
    return locked[from_account.pk], locked[to_account.pk]


def _validate_transfer_accounts(
    from_account: BalanceAccount,
    to_account: BalanceAccount,
) -> None:
    if from_account.status != BalanceAccount.Status.ACTIVE:
        raise ValueError("Исходный счёт должен быть активен.")
    if to_account.status != BalanceAccount.Status.ACTIVE:
        raise ValueError("Целевой счёт должен быть активен.")
    if from_account.funding_source_id != to_account.funding_source_id:
        raise ValueError("Перенос возможен только внутри одного источника финансирования.")

    source = from_account.funding_source
    if source.transfer_policy == FundingSource.TransferPolicy.NOT_TRANSFERABLE:
        raise ValueError("Источник финансирования не разрешает передачу остатков.")
    if (
        source.transfer_policy == FundingSource.TransferPolicy.WITHIN_CHILD
        and from_account.child_id != to_account.child_id
    ):
        raise ValueError("Этот источник можно передавать только в пределах одного получателя.")


def _ensure_matching_idempotent_transfer(
    transfer: BalanceTransfer,
    *,
    from_account: BalanceAccount,
    to_account: BalanceAccount,
    program_block: ProgramBlock | None,
    operation_kind: str,
    amount_from: Decimal,
    amount_to: Decimal,
    conversion_rate: Decimal | None,
    reason: str,
    actor: Any,
) -> BalanceTransfer:
    has_different_intent = (
        transfer.from_account_id != from_account.pk
        or transfer.to_account_id != to_account.pk
        or transfer.program_block_id != (program_block.pk if program_block else None)
        or transfer.operation_kind != operation_kind
        or transfer.amount_to != amount_to
        or transfer.reason != reason
        or transfer.created_by_id != (actor.pk if actor else None)
    )
    has_same_financial_values = (
        operation_kind == BalanceTransfer.OperationKind.MONEY_TO_SESSIONS
        or (
            transfer.amount_from == amount_from
            and transfer.conversion_rate == conversion_rate
        )
    )
    if has_different_intent or not has_same_financial_values:
        raise ValueError("Этот ключ идемпотентности уже использован другой операцией.")
    return transfer


def _record_balance_transfer(
    *,
    from_account: BalanceAccount,
    to_account: BalanceAccount,
    operation_kind: str,
    amount_from: Decimal,
    amount_to: Decimal,
    conversion_rate: Decimal | None,
    reason: str,
    actor: Any = None,
    program_block: ProgramBlock | None = None,
    idempotency_key: UUID | str | None = None,
) -> BalanceTransfer:
    if amount_from is None or amount_from <= 0 or amount_to is None or amount_to <= 0:
        raise ValueError("Сумма и количество переноса должны быть положительными.")
    normalized_reason = reason.strip() if reason else ""
    if not normalized_reason:
        raise ValueError("Укажите основание финансовой операции.")
    key = _as_idempotency_key(idempotency_key)

    with transaction.atomic():
        if program_block is not None:
            program_block = (
                ProgramBlock.objects.select_related("program", "service")
                .select_for_update()
                .get(pk=program_block.pk)
            )
        locked_from, locked_to = _locked_transfer_accounts(from_account, to_account)
        existing = BalanceTransfer.objects.select_related(
            "from_account", "to_account", "program_block"
        ).filter(idempotency_key=key).first()
        if existing is not None:
            return _ensure_matching_idempotent_transfer(
                existing,
                from_account=locked_from,
                to_account=locked_to,
                program_block=program_block,
                operation_kind=operation_kind,
                amount_from=amount_from,
                amount_to=amount_to,
                conversion_rate=conversion_rate,
                reason=normalized_reason,
                actor=actor,
            )

        _validate_transfer_accounts(locked_from, locked_to)
        if amount_from > locked_from.current_balance:
            raise ValueError("Недостаточно средств на исходном счете.")

        if program_block is not None:
            if program_block.program.child_id != locked_to.child_id:
                raise ValueError("Каскад и целевой счёт должны относиться к одному получателю.")
            if (
                program_block.balance_account_id
                and program_block.balance_account_id != locked_to.pk
            ):
                raise ValueError("Целевой счёт должен совпадать со счётом выбранного каскада.")
            if not locked_to.can_pay_for(program_block.service):
                raise ValueError("Целевой счёт не подходит для услуги каскада.")

        try:
            with transaction.atomic():
                transfer = BalanceTransfer.objects.create(
                    from_account=locked_from,
                    to_account=locked_to,
                    program_block=program_block,
                    operation_kind=operation_kind,
                    amount_from=amount_from,
                    amount_to=amount_to,
                    from_unit_snapshot=locked_from.unit,
                    to_unit_snapshot=locked_to.unit,
                    conversion_rate=conversion_rate,
                    reason=normalized_reason,
                    idempotency_key=key,
                    created_by=actor,
                )
        except IntegrityError:
            existing = BalanceTransfer.objects.select_related(
                "from_account", "to_account", "program_block"
            ).get(idempotency_key=key)
            return _ensure_matching_idempotent_transfer(
                existing,
                from_account=locked_from,
                to_account=locked_to,
                program_block=program_block,
                operation_kind=operation_kind,
                amount_from=amount_from,
                amount_to=amount_to,
                conversion_rate=conversion_rate,
                reason=normalized_reason,
                actor=actor,
            )

        LedgerEntry.objects.create(
            account=locked_from,
            entry_type=LedgerEntry.EntryType.TRANSFER,
            amount=-amount_from,
            balance_transfer=transfer,
            transfer_side=LedgerEntry.TransferSide.DEBIT,
            reason=normalized_reason,
            created_by=actor,
        )
        LedgerEntry.objects.create(
            account=locked_to,
            entry_type=LedgerEntry.EntryType.TRANSFER,
            amount=amount_to,
            balance_transfer=transfer,
            transfer_side=LedgerEntry.TransferSide.CREDIT,
            reason=normalized_reason,
            created_by=actor,
        )
        if program_block is not None and not program_block.balance_account_id:
            program_block.balance_account = locked_to
            program_block.save(update_fields=["balance_account", "updated_at"])
        return transfer


def record_balance_transfer(
    *,
    from_account: BalanceAccount,
    to_account: BalanceAccount,
    amount: Decimal,
    reason: str,
    actor: Any = None,
    program_block: ProgramBlock | None = None,
    idempotency_key: UUID | str | None = None,
) -> BalanceTransfer:
    """Persist one direct same-unit transfer and its linked pair of ledger entries."""
    if from_account.unit != to_account.unit:
        raise ValueError("Для разных единиц используйте конвертацию рублей в занятия.")
    return _record_balance_transfer(
        from_account=from_account,
        to_account=to_account,
        program_block=program_block,
        operation_kind=BalanceTransfer.OperationKind.DIRECT,
        amount_from=amount,
        amount_to=amount,
        conversion_rate=None,
        reason=reason,
        actor=actor,
        idempotency_key=idempotency_key,
    )


def convert_money_to_sessions(
    *,
    from_account: BalanceAccount,
    to_account: BalanceAccount,
    program_block: ProgramBlock,
    sessions: Decimal,
    reason: str,
    actor: Any = None,
    idempotency_key: UUID | str | None = None,
) -> BalanceTransfer:
    """Convert a whole number of rubles-backed sessions using the block service price snapshot."""
    if sessions is None or sessions <= 0 or sessions != sessions.to_integral_value():
        raise ValueError("Для конвертации укажите целое положительное количество занятий.")
    with transaction.atomic():
        # Program operations use one global lock order: cascade, then accounts.
        locked_block = (
            ProgramBlock.objects.select_related("program", "service")
            .select_for_update()
            .get(pk=program_block.pk)
        )
        rate = locked_block.service.default_price
        if rate is None or rate <= 0:
            raise ValueError("Для конвертации у услуги каскада должна быть положительная цена.")
        if from_account.unit != BalanceAccount.Unit.MONEY:
            raise ValueError("Исходный счёт конвертации должен быть в рублях.")
        if to_account.unit != BalanceAccount.Unit.SESSIONS:
            raise ValueError("Целевой счёт конвертации должен быть в занятиях.")
        return _record_balance_transfer(
            from_account=from_account,
            to_account=to_account,
            program_block=locked_block,
            operation_kind=BalanceTransfer.OperationKind.MONEY_TO_SESSIONS,
            amount_from=sessions * rate,
            amount_to=sessions,
            conversion_rate=rate,
            reason=reason,
            actor=actor,
            idempotency_key=idempotency_key,
        )


def transfer_between_accounts(
    *,
    from_account: BalanceAccount,
    to_account: BalanceAccount,
    amount: Decimal,
    reason: str,
    actor: Any = None,
) -> tuple[LedgerEntry, LedgerEntry]:
    """Backward-compatible direct transfer wrapper for existing callers."""
    transfer = record_balance_transfer(
        from_account=from_account,
        to_account=to_account,
        amount=amount,
        reason=reason,
        actor=actor,
    )
    entries = {
        entry.transfer_side: entry
        for entry in transfer.ledger_entries.select_related("account").all()
    }
    return entries[LedgerEntry.TransferSide.DEBIT], entries[LedgerEntry.TransferSide.CREDIT]


def summarize_ledger_by_account(child: Child) -> dict[int, Decimal]:
    """Возвращает текущие остатки по счетам получателя (используется в отчётах)."""
    totals: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    qs = LedgerEntry.objects.filter(account__child=child).values_list("account_id", "amount")
    for account_id, amount in qs:
        totals[account_id] += amount
    return dict(totals)
