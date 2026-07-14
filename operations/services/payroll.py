"""Начисления специалистам и расчетные листы."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentStaffAssignment,
    FundingStaffAllocation,
    PayrollAccrual,
    PayrollSheet,
    PayrollSheetLine,
    StaffCompensationRule,
    StaffMember,
)
from operations.services.financial_facts import appointment_charge_fact


@dataclass(frozen=True)
class PayrollGenerationResult:
    created: int = 0
    updated: int = 0
    existing_locked: int = 0
    skipped_no_charge: int = 0
    skipped_no_rule: int = 0

    @property
    def touched(self) -> int:
        return self.created + self.updated


def _matching_compensation_rule(
    rules: list[StaffCompensationRule],
    *,
    service_id: int,
    funding_source_id: int | None,
    work_date: date,
    duration_minutes: int,
) -> StaffCompensationRule | None:
    matches: list[StaffCompensationRule] = []
    for rule in rules:
        if rule.service_id and rule.service_id != service_id:
            continue
        if rule.funding_source_id and rule.funding_source_id != funding_source_id:
            continue
        if rule.min_duration_minutes and duration_minutes < rule.min_duration_minutes:
            continue
        if rule.max_duration_minutes and duration_minutes > rule.max_duration_minutes:
            continue
        if rule.starts_on and rule.starts_on > work_date:
            continue
        if rule.ends_on and rule.ends_on < work_date:
            continue
        matches.append(rule)

    if not matches:
        return None

    return sorted(
        matches,
        key=lambda rule: (
            (1 if rule.service_id else 0) + (1 if rule.funding_source_id else 0),
            1 if rule.funding_source_id else 0,
            1 if rule.service_id else 0,
            (1 if rule.min_duration_minutes else 0) + (1 if rule.max_duration_minutes else 0),
            rule.starts_on or date.min,
            rule.pk or 0,
        ),
        reverse=True,
    )[0]


def _matching_grant_allocation(
    *,
    staff: StaffMember,
    service_id: int,
    funding_source_id: int | None,
    work_date: date,
) -> FundingStaffAllocation | None:
    if not funding_source_id:
        return None
    return (
        FundingStaffAllocation.objects.filter(
            staff_member=staff,
            service_id=service_id,
            funding_source_id=funding_source_id,
            session_pay_amount__isnull=False,
        )
        .filter(Q(starts_on__isnull=True) | Q(starts_on__lte=work_date))
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=work_date))
        .select_related("service_quota", "funding_source", "service", "staff_member")
        .order_by("-starts_on", "-service_quota_id", "-pk")
        .first()
    )


def _compensation_amount(rule: StaffCompensationRule, minutes: int) -> Decimal:
    if rule.rate_type == StaffCompensationRule.RateType.HOURLY:
        return (rule.amount * Decimal(minutes) / Decimal(60)).quantize(Decimal("0.01"))
    return rule.amount


@transaction.atomic
def generate_accruals_for_staff(
    staff: StaffMember,
    *,
    date_from: date,
    date_to: date,
    actor: Any = None,
) -> PayrollGenerationResult:
    if date_to < date_from:
        raise ValueError("Дата окончания не может быть раньше даты начала.")

    tz = timezone.get_current_timezone()
    assignments = list(
        AppointmentStaffAssignment.objects.filter(
            staff_member=staff,
            starts_at_snapshot__date__gte=date_from,
            starts_at_snapshot__date__lte=date_to,
        )
        .select_related(
            "appointment",
            "appointment__service",
            "appointment__billing_account__funding_source",
            "staff_member",
        )
        .prefetch_related("appointment__participants__billing_account__funding_source")
        .order_by("starts_at_snapshot", "appointment_id")
    )
    rules = list(
        StaffCompensationRule.objects.filter(staff_member=staff, is_active=True).select_related(
            "service",
            "funding_source",
        )
    )

    result = PayrollGenerationResult()
    counters = result.__dict__.copy()

    def upsert_accrual(
        *,
        appointment: Appointment,
        starts_at: Any,
        ends_at: Any,
        staff_assignment: AppointmentStaffAssignment | None,
        dedupe_key: str,
    ) -> None:
        context = appointment_charge_fact(appointment)
        if not context.is_charged:
            counters["skipped_no_charge"] += 1
            return

        work_date = timezone.localtime(starts_at, tz).date()
        allocation = _matching_grant_allocation(
            staff=staff,
            service_id=appointment.service_id,
            funding_source_id=context.funding_source.pk if context.funding_source else None,
            work_date=work_date,
        )
        minutes = max(int((ends_at - starts_at).total_seconds() // 60), 0)
        if allocation is not None:
            rule = None
            rate_type = StaffCompensationRule.RateType.PER_SESSION
            rate_amount = allocation.session_pay_amount
            amount = allocation.session_pay_amount
            note = f"{context.note}; ставка из распределения грантовой квоты"
        else:
            rule = _matching_compensation_rule(
                rules,
                service_id=appointment.service_id,
                funding_source_id=context.funding_source.pk if context.funding_source else None,
                work_date=work_date,
                duration_minutes=minutes,
            )
            if rule is None:
                counters["skipped_no_rule"] += 1
                return
            rate_type = rule.rate_type
            rate_amount = rule.amount
            amount = _compensation_amount(rule, minutes)
            note = context.note
        defaults = {
            "staff_assignment": staff_assignment,
            "appointment": appointment,
            "appointment_participant": context.appointment_participant,
            "ledger_entry": context.ledger_entry,
            "staff_member": staff,
            "service": appointment.service,
            "funding_source": context.funding_source,
            "pay_rule": rule,
            "work_date": work_date,
            "starts_at_snapshot": starts_at,
            "ends_at_snapshot": ends_at,
            "duration_minutes": minutes,
            "rate_type_snapshot": rate_type,
            "rate_amount_snapshot": rate_amount,
            "amount": amount,
            "note": note,
            "created_by": actor,
        }
        accrual = PayrollAccrual.objects.filter(dedupe_key=dedupe_key).first()
        if accrual is None:
            PayrollAccrual.objects.create(dedupe_key=dedupe_key, **defaults)
            counters["created"] += 1
            return
        if accrual.status != PayrollAccrual.Status.DRAFT:
            counters["existing_locked"] += 1
            return
        for field, value in defaults.items():
            setattr(accrual, field, value)
        accrual.save(update_fields=[*defaults.keys(), "updated_at"])
        counters["updated"] += 1

    for assignment in assignments:
        appointment = assignment.appointment
        upsert_accrual(
            appointment=appointment,
            starts_at=assignment.starts_at_snapshot,
            ends_at=assignment.ends_at_snapshot,
            staff_assignment=assignment,
            dedupe_key=f"staff-assignment:{assignment.pk}:appointment:{appointment.pk}",
        )

    legacy_appointments = (
        Appointment.objects.filter(
            staff_member=staff,
            starts_at__date__gte=date_from,
            starts_at__date__lte=date_to,
        )
        .exclude(staff_assignments__staff_member=staff)
        .select_related("service", "billing_account__funding_source")
        .prefetch_related("participants__billing_account__funding_source")
        .distinct()
    )
    for appointment in legacy_appointments:
        upsert_accrual(
            appointment=appointment,
            starts_at=appointment.starts_at,
            ends_at=appointment.ends_at,
            staff_assignment=None,
            dedupe_key=f"legacy-appointment:{appointment.pk}:staff:{staff.pk}",
        )

    return PayrollGenerationResult(**counters)


@transaction.atomic
def create_payroll_sheet_for_staff(
    staff: StaffMember,
    *,
    date_from: date,
    date_to: date,
    actor: Any = None,
    generate_missing: bool = True,
) -> PayrollSheet:
    if generate_missing:
        generate_accruals_for_staff(staff, date_from=date_from, date_to=date_to, actor=actor)

    accruals = list(
        PayrollAccrual.objects.filter(
            staff_member=staff,
            work_date__gte=date_from,
            work_date__lte=date_to,
            status=PayrollAccrual.Status.DRAFT,
        )
        .exclude(
            sheet_lines__payroll_sheet__status__in=[
                PayrollSheet.Status.DRAFT,
                PayrollSheet.Status.APPROVED,
                PayrollSheet.Status.SENT,
                PayrollSheet.Status.PAID,
            ]
        )
        .select_related("appointment", "service")
        .order_by("work_date", "starts_at_snapshot", "service__name")
    )
    if not accruals:
        raise ValueError("Нет черновых начислений для расчетного листа.")

    total = sum((accrual.amount for accrual in accruals), Decimal("0"))
    sheet = PayrollSheet.objects.create(
        staff_member=staff,
        date_from=date_from,
        date_to=date_to,
        total_amount=total,
        created_by=actor,
    )
    PayrollSheetLine.objects.bulk_create(
        [
            PayrollSheetLine(
                payroll_sheet=sheet,
                payroll_accrual=accrual,
                appointment=accrual.appointment,
                service=accrual.service,
                work_date=accrual.work_date,
                duration_minutes=accrual.duration_minutes,
                amount=accrual.amount,
                note=accrual.note,
            )
            for accrual in accruals
        ]
    )
    return sheet


@transaction.atomic
def approve_payroll_sheet(sheet: PayrollSheet, *, actor: Any = None) -> PayrollSheet:
    if sheet.status != PayrollSheet.Status.DRAFT:
        raise ValueError("Утвердить можно только черновик расчетного листа.")

    now = timezone.now()
    total = sum((line.amount for line in sheet.lines.all()), Decimal("0"))
    sheet.total_amount = total
    sheet.status = PayrollSheet.Status.APPROVED
    sheet.approved_by = actor
    sheet.approved_at = now
    sheet.save(update_fields=["total_amount", "status", "approved_by", "approved_at", "updated_at"])
    PayrollAccrual.objects.filter(
        sheet_lines__payroll_sheet=sheet, status=PayrollAccrual.Status.DRAFT
    ).update(
        status=PayrollAccrual.Status.APPROVED,
        approved_by=actor,
        approved_at=now,
        updated_at=now,
    )
    return sheet
