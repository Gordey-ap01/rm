"""Начисления специалистам и расчетные листы."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentStaffAssignment,
    PayrollAccrual,
    PayrollPayout,
    PayrollSheet,
    PayrollSheetLifecycleEvent,
    PayrollSheetLine,
    StaffCompensationRule,
    StaffMember,
)
from operations.services.authority import is_director_user
from operations.services.compensation import calculate_staff_compensation
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
        minutes = max(int((ends_at - starts_at).total_seconds() // 60), 0)
        compensation = calculate_staff_compensation(
            staff=staff,
            appointment=appointment,
            charge_fact=context,
            rules=rules,
            work_date=work_date,
            duration_minutes=minutes,
        )
        if not compensation.has_rate:
            counters["skipped_no_rule"] += 1
            return
        defaults = {
            "staff_assignment": staff_assignment,
            "appointment": appointment,
            "appointment_participant": context.appointment_participant,
            "ledger_entry": context.ledger_entry,
            "staff_member": staff,
            "service": appointment.service,
            "funding_source": compensation.funding_source,
            "pay_rule": compensation.rule,
            "grant_allocation_revision": compensation.allocation_revision,
            "work_date": work_date,
            "starts_at_snapshot": starts_at,
            "ends_at_snapshot": ends_at,
            "duration_minutes": minutes,
            "rate_type_snapshot": compensation.rate_type,
            "rate_amount_snapshot": compensation.rate_amount,
            "session_scope_snapshot": compensation.session_scope,
            "group_pay_policy_snapshot": compensation.group_pay_policy,
            "charged_participants_count_snapshot": compensation.charged_participants_count,
            "pay_units_snapshot": compensation.pay_units,
            "amount": compensation.amount,
            "note": compensation.note,
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
        if accrual.sheet_lines.exclude(
            payroll_sheet__status=PayrollSheet.Status.CANCELLED
        ).exists():
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


def _lock_sheet_lines_and_accruals(
    sheet: PayrollSheet,
) -> tuple[list[PayrollSheetLine], list[PayrollAccrual], Decimal]:
    """Lock the payroll fact rows in the same order for every financial transition."""

    lines = list(
        PayrollSheetLine.objects.select_for_update()
        .filter(payroll_sheet=sheet)
        .order_by("pk")
    )
    if not lines:
        raise ValueError("В расчетном листе нет строк начислений.")

    accrual_ids = [line.payroll_accrual_id for line in lines]
    accruals = list(
        PayrollAccrual.objects.select_for_update().filter(pk__in=accrual_ids).order_by("pk")
    )
    if len(accruals) != len(accrual_ids):
        raise ValueError("В расчетном листе есть отсутствующие начисления.")

    total = sum((line.amount for line in lines), Decimal("0"))
    if sheet.total_amount != total:
        raise ValueError("Итог расчетного листа не совпадает с его строками.")
    return lines, accruals, total


def _require_director(actor: Any, action: str) -> None:
    if not is_director_user(actor):
        raise PermissionDenied(f"{action} может только руководитель.")


@transaction.atomic
def send_payroll_sheet(
    sheet: PayrollSheet,
    *,
    note: str,
    actor: Any = None,
) -> PayrollSheet:
    _require_director(actor, "Передать расчетный лист в выплату")
    note = note.strip()
    if len(note) < 5:
        raise ValueError("Укажите основание передачи в выплату не короче 5 символов.")

    sheet = PayrollSheet.objects.select_for_update().get(pk=sheet.pk)
    if sheet.status != PayrollSheet.Status.APPROVED:
        raise ValueError("Передать в выплату можно только утвержденный расчетный лист.")

    _, accruals, _ = _lock_sheet_lines_and_accruals(sheet)
    if any(accrual.status != PayrollAccrual.Status.APPROVED for accrual in accruals):
        raise ValueError("В листе есть начисления, не готовые к выплате.")

    now = timezone.now()
    sheet.status = PayrollSheet.Status.SENT
    sheet.save(update_fields=["status", "updated_at"])
    PayrollSheetLifecycleEvent.objects.create(
        payroll_sheet=sheet,
        event_type=PayrollSheetLifecycleEvent.EventType.SENT,
        status_from=PayrollSheet.Status.APPROVED,
        status_to=PayrollSheet.Status.SENT,
        actor=actor,
        actor_role_snapshot=PayrollSheetLifecycleEvent.ActorRole.DIRECTOR,
        note=note,
        occurred_at=now,
    )
    return sheet


@transaction.atomic
def record_payroll_payout(
    sheet: PayrollSheet,
    *,
    amount: Decimal,
    method: str,
    paid_at: date,
    reference: str = "",
    note: str = "",
    actor: Any = None,
) -> PayrollPayout:
    _require_director(actor, "Зафиксировать выплату")
    if amount is None or amount <= 0:
        raise ValueError("Сумма выплаты должна быть положительной.")
    if method not in PayrollPayout.Method.values:
        raise ValueError("Выберите допустимый способ выплаты.")
    if paid_at is None:
        raise ValueError("Укажите дату выплаты.")

    sheet = PayrollSheet.objects.select_for_update().get(pk=sheet.pk)
    if sheet.status != PayrollSheet.Status.SENT:
        raise ValueError("Зафиксировать выплату можно только после передачи в выплату.")

    _, accruals, total = _lock_sheet_lines_and_accruals(sheet)
    if any(accrual.status != PayrollAccrual.Status.APPROVED for accrual in accruals):
        raise ValueError("В листе есть начисления, не готовые к выплате.")
    if amount != total:
        raise ValueError("Сумма выплаты должна точно совпадать с итогом расчетного листа.")
    if PayrollPayout.objects.select_for_update().filter(payroll_sheet=sheet).exists():
        raise ValueError("Для этого расчетного листа уже зафиксирована выплата.")

    payout = PayrollPayout.objects.create(
        payroll_sheet=sheet,
        amount=amount,
        method=method,
        paid_at=paid_at,
        reference=reference.strip(),
        note=note.strip(),
        recorded_by=actor,
    )
    now = timezone.now()
    sheet.status = PayrollSheet.Status.PAID
    sheet.save(update_fields=["status", "updated_at"])
    PayrollAccrual.objects.filter(pk__in=[accrual.pk for accrual in accruals]).update(
        status=PayrollAccrual.Status.PAID,
        updated_at=now,
    )
    PayrollSheetLifecycleEvent.objects.create(
        payroll_sheet=sheet,
        event_type=PayrollSheetLifecycleEvent.EventType.PAID,
        status_from=PayrollSheet.Status.SENT,
        status_to=PayrollSheet.Status.PAID,
        actor=actor,
        actor_role_snapshot=PayrollSheetLifecycleEvent.ActorRole.DIRECTOR,
        note=note.strip(),
        occurred_at=now,
    )
    return payout


@transaction.atomic
def approve_payroll_sheet(sheet: PayrollSheet, *, actor: Any = None) -> PayrollSheet:
    _require_director(actor, "Утвердить расчетный лист")

    sheet = PayrollSheet.objects.select_for_update().get(pk=sheet.pk)
    if sheet.status != PayrollSheet.Status.DRAFT:
        raise ValueError("Утвердить можно только черновик расчетного листа.")

    lines = list(
        PayrollSheetLine.objects.select_for_update()
        .filter(payroll_sheet=sheet)
        .order_by("pk")
    )
    accrual_ids = [line.payroll_accrual_id for line in lines]
    accruals = list(
        PayrollAccrual.objects.select_for_update().filter(pk__in=accrual_ids).order_by("pk")
    )
    if len(accruals) != len(accrual_ids) or any(
        accrual.status != PayrollAccrual.Status.DRAFT for accrual in accruals
    ):
        raise ValueError("В расчетном листе есть начисления, которые уже нельзя утвердить.")
    accruals_by_id = {accrual.pk: accrual for accrual in accruals}
    if any(
        line.amount != accruals_by_id[line.payroll_accrual_id].amount
        for line in lines
    ):
        raise ValueError(
            "Сумма строки расчетного листа не совпадает с начислением. "
            "Пересоздайте черновик перед утверждением."
        )

    now = timezone.now()
    total = sum((line.amount for line in lines), Decimal("0"))
    sheet.total_amount = total
    sheet.status = PayrollSheet.Status.APPROVED
    sheet.approved_by = actor
    sheet.approved_at = now
    sheet.save(update_fields=["total_amount", "status", "approved_by", "approved_at", "updated_at"])
    PayrollAccrual.objects.filter(pk__in=accrual_ids).update(
        status=PayrollAccrual.Status.APPROVED,
        approved_by=actor,
        approved_at=now,
        updated_at=now,
    )
    return sheet
