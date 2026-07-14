"""Triage transitions for persisted financial integrity findings."""

from __future__ import annotations

from django.utils import timezone

from operations.models import FinancialIntegrityFinding


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
        clear_resolution=True,
    )


def _transition_finding(
    finding: FinancialIntegrityFinding,
    *,
    actor,
    note: str,
    allowed_statuses: set[str],
    next_status: str,
    clear_resolution: bool = False,
) -> FinancialIntegrityFinding:
    if actor is None:
        raise FinancialIntegrityTriageError("Triage actor is required.")
    if finding.status not in allowed_statuses:
        raise FinancialIntegrityTriageError(
            f"Cannot transition financial finding from {finding.status} to {next_status}."
        )

    finding.status = next_status
    finding.triage_note = _normalize_note(note)
    finding.triaged_by = actor
    finding.triaged_at = timezone.now()
    update_fields = ["status", "triage_note", "triaged_by", "triaged_at", "updated_at"]
    if clear_resolution:
        finding.resolved_at = None
        finding.resolved_run = None
        update_fields.extend(["resolved_at", "resolved_run"])
    finding.save(update_fields=update_fields)
    return finding


def _normalize_note(note: str | None) -> str:
    return (note or "").strip()
