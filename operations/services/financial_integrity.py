"""Read-only audit for charge, participant and ledger consistency."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from operations.models import (
    Appointment,
    AppointmentParticipant,
    BalanceAccount,
    FundingSource,
    LedgerEntry,
)
from operations.services.financial_facts import appointment_charge_fact


class FinancialIssueCode:
    PARTICIPANT_CHARGE_WITHOUT_ACCOUNT = "participant_charge_without_account"
    APPOINTMENT_CHARGE_WITHOUT_ACCOUNT = "appointment_charge_without_account"
    MISSING_DEBIT_LEDGER = "missing_debit_ledger"
    STALE_LEGACY_CHARGE_WITH_PARTICIPANTS = "stale_legacy_charge_with_participants"
    STALE_DEBIT_LEDGER_WITHOUT_CHARGE_FACT = "stale_debit_ledger_without_charge_fact"
    LEDGER_ACCOUNT_PARTICIPANT_MISMATCH = "ledger_account_participant_mismatch"
    LEDGER_APPOINTMENT_PARTICIPANT_MISMATCH = "ledger_appointment_participant_mismatch"
    MIXED_FUNDING_GROUP = "mixed_funding_group"


class FinancialIssueSeverity:
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class FinancialIntegrityIssue:
    code: str
    severity: str
    message: str
    appointment: Appointment | None = None
    participant: AppointmentParticipant | None = None
    ledger_entry: LedgerEntry | None = None
    account: BalanceAccount | None = None
    funding_source: FundingSource | None = None


def audit_appointments(
    appointments: Iterable[Appointment],
) -> list[FinancialIntegrityIssue]:
    """Return financial integrity issues without mutating appointments or ledger."""

    issues: list[FinancialIntegrityIssue] = []
    for appointment in appointments:
        issues.extend(_audit_appointment(appointment))
    return issues


def _audit_appointment(appointment: Appointment) -> list[FinancialIntegrityIssue]:
    participants = tuple(
        appointment.participants.select_related(
            "billing_account",
            "billing_account__funding_source",
        ).order_by("pk")
    )
    ledgers = tuple(
        appointment.ledger_entries.filter(entry_type=LedgerEntry.EntryType.DEBIT)
        .select_related(
            "account",
            "account__funding_source",
            "appointment_participant",
            "appointment_participant__billing_account",
        )
        .order_by("pk")
    )
    fact = appointment_charge_fact(appointment)
    issues: list[FinancialIntegrityIssue] = []

    issues.extend(_participant_charge_issues(appointment, participants))
    legacy_issue = _legacy_charge_issue(appointment, participants)
    if legacy_issue is not None:
        issues.append(legacy_issue)
    if fact.missing_debit_ledger:
        issues.append(
            FinancialIntegrityIssue(
                code=FinancialIssueCode.MISSING_DEBIT_LEDGER,
                severity=FinancialIssueSeverity.ERROR,
                message="Списание имеет счет, но не имеет связанной debit ledger-проводки.",
                appointment=appointment,
                participant=fact.appointment_participant,
                account=_fact_account(appointment, fact.appointment_participant),
                funding_source=fact.funding_source,
            )
        )
    if fact.has_mixed_funding:
        issues.append(
            FinancialIntegrityIssue(
                code=FinancialIssueCode.MIXED_FUNDING_GROUP,
                severity=FinancialIssueSeverity.INFO,
                message="Групповое списание использует несколько источников финансирования.",
                appointment=appointment,
            )
        )

    for ledger in ledgers:
        issues.extend(_ledger_issues(appointment, participants, ledger))
    return issues


def _participant_charge_issues(
    appointment: Appointment,
    participants: tuple[AppointmentParticipant, ...],
) -> list[FinancialIntegrityIssue]:
    issues = []
    for participant in participants:
        if (
            participant.billing_decision == Appointment.BillingDecision.CHARGE
            and not participant.billing_account_id
        ):
            issues.append(
                FinancialIntegrityIssue(
                    code=FinancialIssueCode.PARTICIPANT_CHARGE_WITHOUT_ACCOUNT,
                    severity=FinancialIssueSeverity.ERROR,
                    message="Участник помечен как списанный, но счет списания не выбран.",
                    appointment=appointment,
                    participant=participant,
                )
            )
    if (
        not participants
        and appointment.billing_decision == Appointment.BillingDecision.CHARGE
        and not appointment.billing_account_id
    ):
        issues.append(
            FinancialIntegrityIssue(
                code=FinancialIssueCode.APPOINTMENT_CHARGE_WITHOUT_ACCOUNT,
                severity=FinancialIssueSeverity.ERROR,
                message="Занятие помечено как списанное, но счет списания не выбран.",
                appointment=appointment,
            )
        )
    return issues


def _legacy_charge_issue(
    appointment: Appointment,
    participants: tuple[AppointmentParticipant, ...],
) -> FinancialIntegrityIssue | None:
    if not participants or appointment.billing_decision != Appointment.BillingDecision.CHARGE:
        return None
    if _legacy_charge_matches_single_participant(appointment, participants):
        return None
    return FinancialIntegrityIssue(
        code=FinancialIssueCode.STALE_LEGACY_CHARGE_WITH_PARTICIPANTS,
        severity=FinancialIssueSeverity.WARNING,
        message=(
            "Legacy-поле занятия содержит списание, но у занятия есть participant "
            "snapshot, поэтому финансовый факт берется из участников."
        ),
        appointment=appointment,
        account=appointment.billing_account,
        funding_source=appointment.billing_account.funding_source
        if appointment.billing_account_id
        else None,
    )


def _legacy_charge_matches_single_participant(
    appointment: Appointment,
    participants: tuple[AppointmentParticipant, ...],
) -> bool:
    if len(participants) != 1:
        return False
    participant = participants[0]
    return (
        participant.billing_decision == Appointment.BillingDecision.CHARGE
        and participant.billing_account_id
        and participant.billing_account_id == appointment.billing_account_id
    )


def _ledger_issues(
    appointment: Appointment,
    participants: tuple[AppointmentParticipant, ...],
    ledger: LedgerEntry,
) -> list[FinancialIntegrityIssue]:
    issues: list[FinancialIntegrityIssue] = []
    participant = ledger.appointment_participant
    if participant is not None and participant.appointment_id != appointment.pk:
        issues.append(
            FinancialIntegrityIssue(
                code=FinancialIssueCode.LEDGER_APPOINTMENT_PARTICIPANT_MISMATCH,
                severity=FinancialIssueSeverity.ERROR,
                message="Ledger-проводка ссылается на участника другого занятия.",
                appointment=appointment,
                participant=participant,
                ledger_entry=ledger,
                account=ledger.account,
                funding_source=ledger.account.funding_source,
            )
        )
    if participant is not None and participant.child_id != ledger.account.child_id:
        issues.append(
            FinancialIntegrityIssue(
                code=FinancialIssueCode.LEDGER_ACCOUNT_PARTICIPANT_MISMATCH,
                severity=FinancialIssueSeverity.ERROR,
                message="Ledger-проводка ссылается на счет другого получателя.",
                appointment=appointment,
                participant=participant,
                ledger_entry=ledger,
                account=ledger.account,
                funding_source=ledger.account.funding_source,
            )
        )
    if not _ledger_matches_charge_fact(appointment, participants, ledger):
        issues.append(
            FinancialIntegrityIssue(
                code=FinancialIssueCode.STALE_DEBIT_LEDGER_WITHOUT_CHARGE_FACT,
                severity=FinancialIssueSeverity.WARNING,
                message="Debit ledger-проводка привязана к занятию, которое не считается списанным фактом.",
                appointment=appointment,
                participant=participant,
                ledger_entry=ledger,
                account=ledger.account,
                funding_source=ledger.account.funding_source,
            )
        )
    return issues


def _ledger_matches_charge_fact(
    appointment: Appointment,
    participants: tuple[AppointmentParticipant, ...],
    ledger: LedgerEntry,
) -> bool:
    participant = ledger.appointment_participant
    if participant is not None:
        return (
            participant.pk in {item.pk for item in participants}
            and participant.billing_decision == Appointment.BillingDecision.CHARGE
            and participant.billing_account_id == ledger.account_id
        )
    if participants:
        return False
    return (
        appointment.billing_decision == Appointment.BillingDecision.CHARGE
        and appointment.billing_account_id == ledger.account_id
    )


def _fact_account(
    appointment: Appointment,
    participant: AppointmentParticipant | None,
) -> BalanceAccount | None:
    if participant is not None:
        return participant.billing_account
    if appointment.billing_account_id:
        return appointment.billing_account
    return None
