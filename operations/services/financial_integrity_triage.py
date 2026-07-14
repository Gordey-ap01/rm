"""Triage transitions for persisted financial integrity findings."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from operations.models import FinancialIntegrityFinding, FinancialIntegrityFindingEvent
from operations.services import financial_integrity_events as financial_integrity_events_svc


class FinancialIntegrityTriageError(ValueError):
    """Raised when a requested finding triage transition is not allowed."""


def acknowledge_finding(
    finding: FinancialIntegrityFinding,
    *,
    actor,
    note: str = "",
) -> FinancialIntegrityFinding:
    return _transition_finding(
        finding,
        actor=actor,
        note=note,
        allowed_statuses={FinancialIntegrityFinding.Status.OPEN},
        next_status=FinancialIntegrityFinding.Status.ACKNOWLEDGED,
        event_type=FinancialIntegrityFindingEvent.EventType.ACKNOWLEDGED,
    )


def return_finding_to_open(
    finding: FinancialIntegrityFinding,
    *,
    actor,
    note: str = "",
) -> FinancialIntegrityFinding:
    return _transition_finding(
        finding,
        actor=actor,
        note=note,
        allowed_statuses={FinancialIntegrityFinding.Status.ACKNOWLEDGED},
        next_status=FinancialIntegrityFinding.Status.OPEN,
        event_type=FinancialIntegrityFindingEvent.EventType.RETURNED_TO_OPEN,
    )


def ignore_finding(
    finding: FinancialIntegrityFinding,
    *,
    actor,
    note: str,
) -> FinancialIntegrityFinding:
    normalized_note = _normalize_note(note)
    if not normalized_note:
        raise FinancialIntegrityTriageError("Ignore requires a triage note.")
    return _transition_finding(
        finding,
        actor=actor,
        note=normalized_note,
        allowed_statuses={
            FinancialIntegrityFinding.Status.OPEN,
            FinancialIntegrityFinding.Status.ACKNOWLEDGED,
        },
        next_status=FinancialIntegrityFinding.Status.IGNORED,
        event_type=FinancialIntegrityFindingEvent.EventType.IGNORED,
    )


def reopen_finding(
    finding: FinancialIntegrityFinding,
    *,
    actor,
    note: str = "",
) -> FinancialIntegrityFinding:
    return _transition_finding(
        finding,
        actor=actor,
        note=note,
        allowed_statuses={
            FinancialIntegrityFinding.Status.IGNORED,
            FinancialIntegrityFinding.Status.RESOLVED,
        },
        next_status=FinancialIntegrityFinding.Status.OPEN,
        event_type=FinancialIntegrityFindingEvent.EventType.REOPENED,
        clear_resolution=True,
    )


def _transition_finding(
    finding: FinancialIntegrityFinding,
    *,
    actor,
    note: str,
    allowed_statuses: set[str],
    next_status: str,
    event_type: str,
    clear_resolution: bool = False,
) -> FinancialIntegrityFinding:
    if actor is None:
        raise FinancialIntegrityTriageError("Triage actor is required.")
    if finding.status not in allowed_statuses:
        raise FinancialIntegrityTriageError(
            f"Cannot transition financial finding from {finding.status} to {next_status}."
        )

    with transaction.atomic():
        previous_status = finding.status
        event_at = timezone.now()
        finding.status = next_status
        finding.triage_note = _normalize_note(note)
        finding.triaged_by = actor
        finding.triaged_at = event_at
        update_fields = ["status", "triage_note", "triaged_by", "triaged_at", "updated_at"]
        if clear_resolution:
            finding.resolved_at = None
            finding.resolved_run = None
            update_fields.extend(["resolved_at", "resolved_run"])
        finding.save(update_fields=update_fields)
        financial_integrity_events_svc.record_finding_event(
            finding,
            event_type=event_type,
            actor=actor,
            status_from=previous_status,
            status_to=next_status,
            note=finding.triage_note,
            event_at=event_at,
        )
    return finding


def _normalize_note(note: str | None) -> str:
    return (note or "").strip()
