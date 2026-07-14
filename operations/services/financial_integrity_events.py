"""Typed event history for persisted financial integrity findings."""

from __future__ import annotations

import hashlib

from django.utils import timezone

from operations.models import (
    FinancialIntegrityCheckRun,
    FinancialIntegrityFinding,
    FinancialIntegrityFindingEvent,
)


def record_finding_event(
    finding: FinancialIntegrityFinding,
    *,
    event_type: str,
    run: FinancialIntegrityCheckRun | None = None,
    actor=None,
    status_from: str = "",
    status_to: str = "",
    note: str = "",
    event_at=None,
) -> FinancialIntegrityFindingEvent:
    if finding.pk is None:
        raise ValueError("Financial integrity finding must be saved before recording events.")
    event_at = event_at or timezone.now()
    event_key = financial_integrity_event_key(
        finding=finding,
        event_type=event_type,
        run=run,
        actor=actor,
        status_from=status_from,
        status_to=status_to,
        event_at=event_at,
    )
    event, _ = FinancialIntegrityFindingEvent.objects.get_or_create(
        event_key=event_key,
        defaults={
            "finding": finding,
            "event_type": event_type,
            "event_at": event_at,
            "run": run,
            "actor": actor,
            "status_from": status_from,
            "status_to": status_to,
            "severity": finding.severity,
            "code": finding.code,
            "issue_key": finding.issue_key,
            "message": finding.message,
            "note": (note or "").strip(),
            "source_snapshot": financial_integrity_source_snapshot(finding),
        },
    )
    return event


def financial_integrity_event_key(
    *,
    finding: FinancialIntegrityFinding,
    event_type: str,
    run: FinancialIntegrityCheckRun | None,
    actor,
    status_from: str,
    status_to: str,
    event_at,
) -> str:
    actor_id = getattr(actor, "pk", None)
    parts = [
        str(finding.pk or ""),
        finding.issue_key or "",
        event_type or "",
        str(run.pk if run else ""),
        str(actor_id or ""),
        status_from or "",
        status_to or "",
        event_at.isoformat() if event_at else "",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def financial_integrity_source_snapshot(finding: FinancialIntegrityFinding) -> dict[str, object]:
    return {
        "appointment_id": finding.appointment_id,
        "appointment_starts_at": (
            finding.appointment_starts_at.isoformat() if finding.appointment_starts_at else None
        ),
        "appointment_service_name": finding.appointment_service_name,
        "appointment_participant_id": finding.appointment_participant_id,
        "participant_name": finding.participant_name,
        "ledger_entry_id": finding.ledger_entry_id,
        "ledger_entry_type": finding.ledger_entry_type,
        "ledger_amount": str(finding.ledger_amount) if finding.ledger_amount is not None else None,
        "account_id": finding.account_id,
        "account_label": finding.account_label,
        "funding_source_id": finding.funding_source_id,
        "funding_source_name": finding.funding_source_name,
        "payload": finding.payload,
    }
