"""Read-only financial facts derived from appointments."""

from __future__ import annotations

from dataclasses import dataclass

from operations.models import (
    Appointment,
    AppointmentParticipant,
    FundingSource,
    LedgerEntry,
)


@dataclass(frozen=True)
class AppointmentChargeFact:
    """Unified read model for the financial fact "appointment was charged"."""

    is_charged: bool
    funding_source: FundingSource | None = None
    ledger_entry: LedgerEntry | None = None
    appointment_participant: AppointmentParticipant | None = None
    note: str = ""
    billing_decision_label: str = ""
    charged_participants: tuple[AppointmentParticipant, ...] = ()
    ledger_entries: tuple[LedgerEntry, ...] = ()
    funding_source_ids: frozenset[int] = frozenset()
    has_mixed_funding: bool = False
    missing_debit_ledger: bool = False


def appointment_charge_fact(
    appointment: Appointment,
    *,
    include_ledger: bool = True,
) -> AppointmentChargeFact:
    """Return one source of truth for payroll/report charged appointment logic."""

    participants = tuple(appointment.participants.all())
    billing_label = appointment_billing_decision_label(appointment, participants=participants)
    charged_participants = tuple(
        sorted(
            (
                participant
                for participant in participants
                if participant.billing_decision == Appointment.BillingDecision.CHARGE
                and participant.billing_account_id
            ),
            key=lambda item: item.pk or 0,
        )
    )
    if charged_participants:
        ledger_entries = (
            _debit_ledger_entries(
                appointment,
                participants=charged_participants,
            )
            if include_ledger
            else ()
        )
        source_ids = frozenset(
            participant.billing_account.funding_source_id
            for participant in charged_participants
        )
        funding_source = (
            charged_participants[0].billing_account.funding_source
            if len(source_ids) == 1
            else None
        )
        note = f"списано участников: {len(charged_participants)}"
        if len(source_ids) > 1:
            note = f"{note}; смешанные источники финансирования"
        single_participant = (
            charged_participants[0] if len(charged_participants) == 1 else None
        )
        return AppointmentChargeFact(
            is_charged=True,
            funding_source=funding_source,
            ledger_entry=ledger_entries[0] if single_participant and ledger_entries else None,
            appointment_participant=single_participant,
            note=note,
            billing_decision_label=billing_label,
            charged_participants=charged_participants,
            ledger_entries=ledger_entries,
            funding_source_ids=source_ids,
            has_mixed_funding=len(source_ids) > 1,
            missing_debit_ledger=_has_missing_participant_debit(
                charged_participants,
                ledger_entries,
            )
            if include_ledger
            else False,
        )

    if participants:
        return AppointmentChargeFact(
            is_charged=False,
            note="нет решения «Списать» по участникам",
            billing_decision_label=billing_label,
        )

    if (
        appointment.billing_decision == Appointment.BillingDecision.CHARGE
        and appointment.billing_account_id
    ):
        ledger_entries = (
            _debit_ledger_entries(appointment, participants=None) if include_ledger else ()
        )
        funding_source = appointment.billing_account.funding_source
        return AppointmentChargeFact(
            is_charged=True,
            funding_source=funding_source,
            ledger_entry=ledger_entries[0] if ledger_entries else None,
            note="списано по занятию",
            billing_decision_label=billing_label,
            ledger_entries=ledger_entries,
            funding_source_ids=frozenset({funding_source.pk}),
            missing_debit_ledger=not ledger_entries if include_ledger else False,
        )

    return AppointmentChargeFact(
        is_charged=False,
        note="нет решения «Списать»",
        billing_decision_label=billing_label,
    )


def appointment_billing_decision_label(
    appointment: Appointment,
    *,
    participants: tuple[AppointmentParticipant, ...] | None = None,
) -> str:
    """Return the same billing decision label used by the specialist timesheet."""

    participant_rows = participants
    if participant_rows is None:
        participant_rows = tuple(appointment.participants.all())
    if not participant_rows:
        return appointment.get_billing_decision_display()
    if len(participant_rows) == 1:
        return participant_rows[0].get_billing_decision_display()

    counts: dict[str, int] = {}
    for participant in participant_rows:
        counts[participant.billing_decision] = counts.get(participant.billing_decision, 0) + 1
    parts = []
    if counts.get(Appointment.BillingDecision.CHARGE):
        parts.append(f"списать: {counts[Appointment.BillingDecision.CHARGE]}")
    if counts.get(Appointment.BillingDecision.DO_NOT_CHARGE):
        parts.append(f"не списывать: {counts[Appointment.BillingDecision.DO_NOT_CHARGE]}")
    if counts.get(Appointment.BillingDecision.UNDECIDED):
        parts.append(f"не решено: {counts[Appointment.BillingDecision.UNDECIDED]}")
    return ", ".join(parts) if parts else appointment.get_billing_decision_display()


def _debit_ledger_entries(
    appointment: Appointment,
    *,
    participants: tuple[AppointmentParticipant, ...] | None,
) -> tuple[LedgerEntry, ...]:
    ledger_qs = LedgerEntry.objects.filter(
        appointment=appointment,
        entry_type=LedgerEntry.EntryType.DEBIT,
    ).select_related("account", "account__funding_source")
    if participants is None:
        ledger_qs = ledger_qs.filter(appointment_participant__isnull=True)
    else:
        ledger_qs = ledger_qs.filter(appointment_participant__in=participants)
    return tuple(ledger_qs)


def _has_missing_participant_debit(
    charged_participants: tuple[AppointmentParticipant, ...],
    ledger_entries: tuple[LedgerEntry, ...],
) -> bool:
    ledger_participant_ids = {
        entry.appointment_participant_id
        for entry in ledger_entries
        if entry.appointment_participant_id
    }
    return any(
        participant.pk not in ledger_participant_ids for participant in charged_participants
    )
