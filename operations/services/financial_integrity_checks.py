"""Persisted financial integrity check runs and findings."""

from __future__ import annotations

from collections.abc import Iterable

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from operations.models import (
    Appointment,
    FinancialIntegrityCheckRun,
    FinancialIntegrityFinding,
    LedgerEntry,
)
from operations.services import financial_integrity


def financial_integrity_candidate_queryset():
    return (
        Appointment.objects.filter(
            Q(billing_decision=Appointment.BillingDecision.CHARGE)
            | Q(participants__billing_decision=Appointment.BillingDecision.CHARGE)
            | Q(ledger_entries__entry_type=LedgerEntry.EntryType.DEBIT)
        )
        .select_related(
            "child",
            "staff_member",
            "service",
            "room",
            "billing_account",
            "billing_account__funding_source",
        )
        .prefetch_related(
            "participants__child",
            "participants__billing_account",
            "participants__billing_account__funding_source",
            "ledger_entries__account",
            "ledger_entries__account__funding_source",
            "ledger_entries__appointment_participant",
            "ledger_entries__appointment_participant__child",
            "ledger_entries__appointment_participant__billing_account",
        )
        .distinct()
        .order_by("pk")
    )


def run_financial_integrity_check(
    *,
    appointments: Iterable[Appointment] | None = None,
    requested_by=None,
    run_type: str = FinancialIntegrityCheckRun.RunType.MANUAL,
) -> FinancialIntegrityCheckRun:
    run = FinancialIntegrityCheckRun.objects.create(
        run_type=run_type,
        requested_by=requested_by,
        status=FinancialIntegrityCheckRun.Status.RUNNING,
    )
    try:
        is_scoped_run = appointments is not None
        candidates = list(appointments if is_scoped_run else financial_integrity_candidate_queryset())
        candidate_ids = {appointment.pk for appointment in candidates if appointment.pk}
        issues = financial_integrity.audit_appointments(candidates)
        now = timezone.now()
        seen_keys = set()
        severity_counts = {
            FinancialIntegrityFinding.Severity.ERROR: 0,
            FinancialIntegrityFinding.Severity.WARNING: 0,
            FinancialIntegrityFinding.Severity.INFO: 0,
        }

        with transaction.atomic():
            for issue in issues:
                issue_key = financial_integrity.financial_integrity_issue_key(issue)
                seen_keys.add(issue_key)
                severity_counts[issue.severity] = severity_counts.get(issue.severity, 0) + 1
                _upsert_finding(issue, issue_key=issue_key, run=run, seen_at=now)
            _resolve_unseen_findings(
                seen_keys=seen_keys,
                run=run,
                resolved_at=now,
                appointment_ids=candidate_ids if is_scoped_run else None,
            )
            run.status = FinancialIntegrityCheckRun.Status.COMPLETED
            run.finished_at = now
            run.candidate_count = len(candidates)
            run.issue_count = len(issues)
            run.error_count = severity_counts[FinancialIntegrityFinding.Severity.ERROR]
            run.warning_count = severity_counts[FinancialIntegrityFinding.Severity.WARNING]
            run.info_count = severity_counts[FinancialIntegrityFinding.Severity.INFO]
            run.error_message = ""
            run.save(
                update_fields=[
                    "status",
                    "finished_at",
                    "candidate_count",
                    "issue_count",
                    "error_count",
                    "warning_count",
                    "info_count",
                    "error_message",
                    "updated_at",
                ]
            )
    except Exception as exc:
        run.status = FinancialIntegrityCheckRun.Status.FAILED
        run.finished_at = timezone.now()
        run.error_message = str(exc)
        run.save(update_fields=["status", "finished_at", "error_message", "updated_at"])
        raise
    return run


def _upsert_finding(
    issue: financial_integrity.FinancialIntegrityIssue,
    *,
    issue_key: str,
    run: FinancialIntegrityCheckRun,
    seen_at,
) -> FinancialIntegrityFinding:
    defaults = _finding_values(issue, issue_key=issue_key, seen_at=seen_at)
    finding, created = FinancialIntegrityFinding.objects.get_or_create(
        issue_key=issue_key,
        defaults={
            **defaults,
            "status": FinancialIntegrityFinding.Status.OPEN,
            "first_seen_at": seen_at,
            "last_seen_at": seen_at,
            "first_seen_run": run,
            "last_seen_run": run,
        },
    )
    if created:
        return finding

    update_fields = [
        "code",
        "severity",
        "appointment",
        "appointment_participant",
        "ledger_entry",
        "account",
        "funding_source",
        "last_seen_at",
        "last_seen_run",
        "message",
        "appointment_starts_at",
        "appointment_service_name",
        "participant_name",
        "account_label",
        "funding_source_name",
        "ledger_entry_type",
        "ledger_amount",
        "payload",
        "updated_at",
    ]
    for field, value in defaults.items():
        setattr(finding, field, value)
    finding.last_seen_at = seen_at
    finding.last_seen_run = run
    if finding.status == FinancialIntegrityFinding.Status.RESOLVED:
        finding.status = FinancialIntegrityFinding.Status.OPEN
        finding.resolved_at = None
        finding.resolved_run = None
        update_fields.extend(["status", "resolved_at", "resolved_run"])
    finding.save(update_fields=update_fields)
    return finding


def _resolve_unseen_findings(
    *,
    seen_keys: set[str],
    run: FinancialIntegrityCheckRun,
    resolved_at,
    appointment_ids: set[int] | None,
) -> int:
    queryset = FinancialIntegrityFinding.objects.filter(
        status__in=[
            FinancialIntegrityFinding.Status.OPEN,
            FinancialIntegrityFinding.Status.ACKNOWLEDGED,
        ]
    )
    if appointment_ids is not None:
        if not appointment_ids:
            return 0
        queryset = queryset.filter(appointment_id__in=appointment_ids)
    if seen_keys:
        queryset = queryset.exclude(issue_key__in=seen_keys)
    return queryset.update(
        status=FinancialIntegrityFinding.Status.RESOLVED,
        resolved_at=resolved_at,
        resolved_run=run,
        updated_at=timezone.now(),
    )


def _finding_values(
    issue: financial_integrity.FinancialIntegrityIssue,
    *,
    issue_key: str,
    seen_at,
) -> dict[str, object]:
    appointment = issue.appointment
    participant = issue.participant
    ledger_entry = issue.ledger_entry
    account = issue.account
    funding_source = issue.funding_source
    return {
        "code": issue.code,
        "severity": issue.severity,
        "appointment": appointment,
        "appointment_participant": participant,
        "ledger_entry": ledger_entry,
        "account": account,
        "funding_source": funding_source,
        "last_seen_at": seen_at,
        "message": issue.message,
        "appointment_starts_at": appointment.starts_at if appointment else None,
        "appointment_service_name": str(appointment.service) if appointment else "",
        "participant_name": str(participant.child) if participant else "",
        "account_label": str(account) if account else "",
        "funding_source_name": str(funding_source) if funding_source else "",
        "ledger_entry_type": ledger_entry.get_entry_type_display() if ledger_entry else "",
        "ledger_amount": ledger_entry.amount if ledger_entry else None,
        "payload": {
            "issue_key": issue_key,
            "code": issue.code,
            "severity": issue.severity,
            "appointment_id": appointment.pk if appointment else None,
            "participant_id": participant.pk if participant else None,
            "ledger_entry_id": ledger_entry.pk if ledger_entry else None,
            "account_id": account.pk if account else None,
            "funding_source_id": funding_source.pk if funding_source else None,
        },
    }
