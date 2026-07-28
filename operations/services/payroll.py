"""Начисления специалистам и расчетные листы."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentStaffAssignment,
    FundingPayrollBudget,
    FundingSource,
    GrantFixedCompensation,
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
    skipped_fixed_service: int = 0

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
    fixed_position_queryset = GrantFixedCompensation.objects.filter(
        staff_member=staff,
        lifecycle_status=GrantFixedCompensation.LifecycleStatus.ACTIVE,
        period_to__gte=date_from,
        period_from__lte=date_to,
    )
    fixed_source_ids = sorted(
        set(
            fixed_position_queryset.values_list(
                "payroll_budget__funding_source_id",
                flat=True,
            )
        )
    )
    if fixed_source_ids:
        list(
            FundingSource.all_objects.select_for_update()
            .filter(pk__in=fixed_source_ids)
            .order_by("pk")
        )
    fixed_positions = list(
        fixed_position_queryset.select_for_update(of=("self",))
        .select_related(
            "service",
            "current_revision",
            "current_revision__service",
            "payroll_budget",
            "payroll_budget__funding_source",
            "payroll_budget__current_revision",
        )
        .order_by("pk")
    )
    for fixed in fixed_positions:
        revision = fixed.current_revision
        budget_revision = fixed.payroll_budget.current_revision
        if (
            revision is None
            or revision.fixed_compensation_id != fixed.pk
            or revision.superseded_by.exists()
            or budget_revision is None
            or budget_revision.payroll_budget_id != fixed.payroll_budget_id
            or budget_revision.superseded_by.exists()
        ):
            raise ValueError(
                "Нарушена целостность фиксированной грантовой оплаты. "
                "Запустите проверку грантового плана."
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
        fixed_matches = [
            fixed
            for fixed in fixed_positions
            if fixed.compensation_scope
            == GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
            and fixed.service_id == appointment.service_id
            and fixed.payroll_budget.funding_source_id
            == getattr(compensation.funding_source, "pk", None)
            and fixed.period_from <= work_date <= fixed.period_to
        ]
        if len(fixed_matches) > 1:
            raise ValueError(
                "Для услуги найдено несколько фиксированных грантовых позиций. "
                "Запустите проверку грантового плана."
            )
        if fixed_matches:
            counters["skipped_fixed_service"] += 1
            return
        defaults = {
            "accrual_kind": PayrollAccrual.AccrualKind.APPOINTMENT,
            "staff_assignment": staff_assignment,
            "appointment": appointment,
            "appointment_participant": context.appointment_participant,
            "ledger_entry": context.ledger_entry,
            "staff_member": staff,
            "service": appointment.service,
            "funding_source": compensation.funding_source,
            "pay_rule": compensation.rule,
            "grant_allocation_revision": compensation.allocation_revision,
            "grant_fixed_compensation_revision": None,
            "payroll_budget_revision": None,
            "work_date": work_date,
            "period_from_snapshot": None,
            "period_to_snapshot": None,
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

    for fixed in fixed_positions:
        if not (date_from <= fixed.accrual_on <= date_to):
            continue
        revision = fixed.current_revision
        budget_revision = fixed.payroll_budget.current_revision
        if revision is None or budget_revision is None:
            raise ValueError("У фиксированной грантовой позиции отсутствует текущая редакция.")
        subject = (
            revision.service.name
            if revision.compensation_scope
            == GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
            else revision.assignment_label
        )
        defaults = {
            "accrual_kind": PayrollAccrual.AccrualKind.GRANT_FIXED,
            "staff_assignment": None,
            "appointment": None,
            "appointment_participant": None,
            "ledger_entry": None,
            "staff_member": staff,
            "service": None,
            "funding_source": fixed.payroll_budget.funding_source,
            "pay_rule": None,
            "grant_allocation_revision": None,
            "grant_fixed_compensation_revision": revision,
            "payroll_budget_revision": budget_revision,
            "work_date": revision.accrual_on,
            "period_from_snapshot": revision.period_from,
            "period_to_snapshot": revision.period_to,
            "starts_at_snapshot": None,
            "ends_at_snapshot": None,
            "duration_minutes": None,
            "rate_type_snapshot": None,
            "rate_amount_snapshot": None,
            "session_scope_snapshot": None,
            "group_pay_policy_snapshot": None,
            "charged_participants_count_snapshot": None,
            "pay_units_snapshot": None,
            "amount": revision.amount,
            "note": f"Фиксированная грантовая оплата: {subject}",
            "created_by": actor,
        }
        dedupe_key = f"grant-fixed:{fixed.pk}"
        accrual = PayrollAccrual.objects.filter(dedupe_key=dedupe_key).first()
        if accrual is None:
            PayrollAccrual.objects.create(dedupe_key=dedupe_key, **defaults)
            counters["created"] += 1
            continue
        if accrual.status != PayrollAccrual.Status.DRAFT:
            counters["existing_locked"] += 1
            continue
        if accrual.sheet_lines.exclude(
            payroll_sheet__status=PayrollSheet.Status.CANCELLED
        ).exists():
            counters["existing_locked"] += 1
            continue
        for field, value in defaults.items():
            setattr(accrual, field, value)
        accrual.save(update_fields=[*defaults.keys(), "updated_at"])
        counters["updated"] += 1

    return PayrollGenerationResult(**counters)


def _payroll_line_label(accrual: PayrollAccrual) -> str:
    if accrual.accrual_kind == PayrollAccrual.AccrualKind.APPOINTMENT:
        if accrual.service is None:
            raise ValueError("У начисления за занятие отсутствует услуга.")
        return accrual.service.name
    revision = accrual.grant_fixed_compensation_revision
    if revision is None:
        raise ValueError("У фиксированного начисления отсутствует редакция позиции.")
    if (
        revision.compensation_scope
        == GrantFixedCompensation.CompensationScope.SERVICE_DELIVERY
    ):
        if revision.service is None:
            raise ValueError("У фиксированной оплаты услуги отсутствует услуга.")
        return f"Фиксировано: {revision.service.name}"
    return revision.assignment_label


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
        .select_related(
            "appointment",
            "service",
            "funding_source",
            "payroll_budget_revision",
            "grant_fixed_compensation_revision",
            "grant_fixed_compensation_revision__service",
            "grant_fixed_compensation_revision__fixed_compensation",
        )
        .order_by("work_date", "accrual_kind", "pk")
    )
    if not accruals:
        raise ValueError("Нет черновых начислений для расчетного листа.")

    accrual_budgets, _budgets = _lock_payroll_budgets_for_accruals(accruals)
    now = timezone.now()
    for accrual in accruals:
        budget = accrual_budgets.get(accrual.pk)
        accrual.payroll_budget_revision = (
            budget.current_revision if budget is not None else None
        )
        accrual.updated_at = now
    PayrollAccrual.objects.bulk_update(
        accruals,
        ["payroll_budget_revision", "updated_at"],
    )

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
                accrual_kind_snapshot=accrual.accrual_kind,
                appointment=accrual.appointment,
                service=accrual.service,
                funding_source=accrual.funding_source,
                payroll_budget_revision=accrual.payroll_budget_revision,
                work_date=accrual.work_date,
                period_from_snapshot=accrual.period_from_snapshot,
                period_to_snapshot=accrual.period_to_snapshot,
                line_label=_payroll_line_label(accrual),
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


def _lock_payroll_budgets_for_accruals(
    accruals: list[PayrollAccrual],
) -> tuple[dict[int, FundingPayrollBudget], list[FundingPayrollBudget]]:
    source_ids = sorted(
        {
            accrual.funding_source_id
            for accrual in accruals
            if accrual.funding_source_id is not None
        }
    )
    if source_ids:
        list(
            FundingSource.all_objects.select_for_update()
            .filter(pk__in=source_ids)
            .order_by("pk")
        )

    budget_ids: set[int] = set()
    appointment_accruals = [
        accrual
        for accrual in accruals
        if accrual.accrual_kind == PayrollAccrual.AccrualKind.APPOINTMENT
        and accrual.funding_source_id is not None
    ]
    if appointment_accruals:
        earliest = min(accrual.work_date for accrual in appointment_accruals)
        latest = max(accrual.work_date for accrual in appointment_accruals)
        budget_ids.update(
            FundingPayrollBudget.objects.filter(
                funding_source_id__in=source_ids,
                starts_on__lte=latest,
                ends_on__gte=earliest,
            ).values_list("pk", flat=True)
        )

    for accrual in accruals:
        if accrual.accrual_kind != PayrollAccrual.AccrualKind.GRANT_FIXED:
            continue
        revision = accrual.grant_fixed_compensation_revision
        if revision is None:
            raise ValueError(
                "У фиксированного начисления отсутствует редакция позиции."
            )
        budget_ids.add(revision.fixed_compensation.payroll_budget_id)

    budgets = list(
        FundingPayrollBudget.objects.select_for_update(of=("self",))
        .filter(pk__in=budget_ids)
        .select_related("current_revision")
        .order_by("pk")
    )
    for budget in budgets:
        if (
            budget.current_revision is None
            or budget.current_revision.payroll_budget_id != budget.pk
            or budget.current_revision.superseded_by.exists()
        ):
            raise ValueError(
                "Нарушена цепочка редакций бюджета оплаты труда. "
                "Запустите проверку грантового плана."
            )

    accrual_budgets = _match_accruals_to_locked_budgets(accruals, budgets)
    used_budget_ids = {budget.pk for budget in accrual_budgets.values()}
    return (
        accrual_budgets,
        [budget for budget in budgets if budget.pk in used_budget_ids],
    )


def _match_accruals_to_locked_budgets(
    accruals: list[PayrollAccrual],
    budgets: list[FundingPayrollBudget],
) -> dict[int, FundingPayrollBudget]:
    budgets_by_id = {budget.pk: budget for budget in budgets}
    accrual_budgets: dict[int, FundingPayrollBudget] = {}
    for accrual in accruals:
        if accrual.accrual_kind == PayrollAccrual.AccrualKind.GRANT_FIXED:
            revision = accrual.grant_fixed_compensation_revision
            if revision is None:
                raise ValueError(
                    "У фиксированного начисления отсутствует редакция позиции."
                )
            budget_id = revision.fixed_compensation.payroll_budget_id
            budget = budgets_by_id.get(budget_id)
            if budget is None:
                raise ValueError("Не найден бюджет фиксированного начисления.")
            if budget.funding_source_id != accrual.funding_source_id:
                raise ValueError(
                    "Источник фиксированного начисления не совпадает с бюджетом."
                )
            accrual_budgets[accrual.pk] = budget
            continue

        matches = [
            budget
            for budget in budgets
            if budget.funding_source_id == accrual.funding_source_id
            and budget.starts_on <= accrual.work_date <= budget.ends_on
        ]
        if len(matches) > 1:
            raise ValueError(
                "Для начисления найдено несколько бюджетов оплаты труда. "
                "Запустите проверку грантового плана."
            )
        if matches:
            accrual_budgets[accrual.pk] = matches[0]
    return accrual_budgets


def _payroll_budget_lock_signature(
    accruals: list[PayrollAccrual],
) -> list[tuple[int, str, int | None, date, int | None]]:
    signature = []
    for accrual in accruals:
        fixed_budget_id = None
        if accrual.accrual_kind == PayrollAccrual.AccrualKind.GRANT_FIXED:
            revision = accrual.grant_fixed_compensation_revision
            if revision is None:
                raise ValueError(
                    "У фиксированного начисления отсутствует редакция позиции."
                )
            fixed_budget_id = revision.fixed_compensation.payroll_budget_id
        signature.append(
            (
                accrual.pk,
                accrual.accrual_kind,
                accrual.funding_source_id,
                accrual.work_date,
                fixed_budget_id,
            )
        )
    return signature


@transaction.atomic
def approve_payroll_sheet(
    sheet: PayrollSheet,
    *,
    actor: Any = None,
    note: str = "",
) -> PayrollSheet:
    _require_director(actor, "Утвердить расчетный лист")
    normalized_note = (note or "").strip()

    candidate_accrual_ids = list(
        PayrollSheetLine.objects.filter(payroll_sheet_id=sheet.pk)
        .order_by("payroll_accrual_id")
        .values_list("payroll_accrual_id", flat=True)
    )
    candidate_accruals = list(
        PayrollAccrual.objects.filter(pk__in=candidate_accrual_ids)
        .select_related(
            "funding_source",
            "grant_fixed_compensation_revision",
            "grant_fixed_compensation_revision__fixed_compensation",
        )
        .order_by("pk")
    )
    if len(candidate_accruals) != len(candidate_accrual_ids):
        raise ValueError("В расчетном листе есть отсутствующие начисления.")
    candidate_signature = _payroll_budget_lock_signature(candidate_accruals)
    _, budgets = _lock_payroll_budgets_for_accruals(candidate_accruals)

    sheet = PayrollSheet.objects.select_for_update().get(pk=sheet.pk)
    if sheet.status != PayrollSheet.Status.DRAFT:
        raise ValueError("Утвердить можно только черновик расчетного листа.")

    lines = list(
        PayrollSheetLine.objects.select_for_update()
        .filter(payroll_sheet=sheet)
        .order_by("pk")
    )
    if not lines:
        raise ValueError("В расчетном листе нет строк начислений.")
    accrual_ids = [line.payroll_accrual_id for line in lines]
    accruals = list(
        PayrollAccrual.objects.select_for_update(of=("self",))
        .filter(pk__in=accrual_ids)
        .select_related(
            "funding_source",
            "grant_fixed_compensation_revision",
            "grant_fixed_compensation_revision__fixed_compensation",
        )
        .order_by("pk")
    )
    if len(accruals) != len(accrual_ids) or any(
        accrual.status != PayrollAccrual.Status.DRAFT for accrual in accruals
    ):
        raise ValueError("В расчетном листе есть начисления, которые уже нельзя утвердить.")
    if _payroll_budget_lock_signature(accruals) != candidate_signature:
        raise ValueError(
            "Состав или бюджет расчетного листа изменился. Повторите утверждение."
        )
    accruals_by_id = {accrual.pk: accrual for accrual in accruals}
    if any(
        line.amount != accruals_by_id[line.payroll_accrual_id].amount
        for line in lines
    ):
        raise ValueError(
            "Сумма строки расчетного листа не совпадает с начислением. "
            "Пересоздайте черновик перед утверждением."
        )

    accrual_budgets = _match_accruals_to_locked_budgets(accruals, budgets)
    current_by_budget: dict[int, Decimal] = {
        budget.pk: Decimal("0") for budget in budgets
    }
    lines_by_accrual_id = {line.payroll_accrual_id: line for line in lines}
    for accrual in accruals:
        budget = accrual_budgets.get(accrual.pk)
        if budget is not None:
            current_by_budget[budget.pk] += lines_by_accrual_id[accrual.pk].amount

    consumed_rows = (
        PayrollSheetLine.objects.filter(
            payroll_budget_revision__payroll_budget_id__in=[
                budget.pk for budget in budgets
            ],
            payroll_sheet__status__in=[
                PayrollSheet.Status.APPROVED,
                PayrollSheet.Status.SENT,
                PayrollSheet.Status.PAID,
            ],
        )
        .values("payroll_budget_revision__payroll_budget_id")
        .annotate(total=Sum("amount"))
    )
    consumed_by_budget = {
        row["payroll_budget_revision__payroll_budget_id"]: row["total"] or Decimal("0")
        for row in consumed_rows
    }
    overage_by_budget: dict[int, Decimal] = {}
    for budget in budgets:
        consumed = consumed_by_budget.get(budget.pk, Decimal("0"))
        projected = consumed + current_by_budget[budget.pk]
        overage = max(projected - budget.planned_amount, Decimal("0"))
        overage_by_budget[budget.pk] = overage
        if (
            overage
            and budget.enforcement_mode
            == FundingPayrollBudget.EnforcementMode.HARD
        ):
            raise ValueError(
                f"Утверждение превысит жесткий бюджет «{budget.funding_source}» "
                f"на {overage}."
            )
        if overage and len(normalized_note) < 5:
            raise ValueError(
                "Для превышения предупреждающего бюджета укажите отдельное основание."
            )

    now = timezone.now()
    total = sum((line.amount for line in lines), Decimal("0"))
    for accrual in accruals:
        budget = accrual_budgets.get(accrual.pk)
        budget_revision = budget.current_revision if budget is not None else None
        accrual.payroll_budget_revision = budget_revision
        accrual.status = PayrollAccrual.Status.APPROVED
        accrual.approved_by = actor
        accrual.approved_at = now
        accrual.updated_at = now
        line = lines_by_accrual_id[accrual.pk]
        line.payroll_budget_revision = budget_revision
        line.updated_at = now
    PayrollAccrual.objects.bulk_update(
        accruals,
        [
            "payroll_budget_revision",
            "status",
            "approved_by",
            "approved_at",
            "updated_at",
        ],
    )
    PayrollSheetLine.objects.bulk_update(
        lines,
        ["payroll_budget_revision", "updated_at"],
    )
    sheet.total_amount = total
    sheet.status = PayrollSheet.Status.APPROVED
    sheet.approved_by = actor
    sheet.approved_at = now
    sheet.save(update_fields=["total_amount", "status", "approved_by", "approved_at", "updated_at"])
    if budgets:
        for budget in budgets:
            PayrollSheetLifecycleEvent.objects.create(
                payroll_sheet=sheet,
                event_type=PayrollSheetLifecycleEvent.EventType.APPROVED,
                status_from=PayrollSheet.Status.DRAFT,
                status_to=PayrollSheet.Status.APPROVED,
                actor=actor,
                actor_role_snapshot=PayrollSheetLifecycleEvent.ActorRole.DIRECTOR,
                note=normalized_note if overage_by_budget[budget.pk] else "",
                payroll_budget_revision=budget.current_revision,
                budget_overage_amount=overage_by_budget[budget.pk],
                occurred_at=now,
            )
    else:
        PayrollSheetLifecycleEvent.objects.create(
            payroll_sheet=sheet,
            event_type=PayrollSheetLifecycleEvent.EventType.APPROVED,
            status_from=PayrollSheet.Status.DRAFT,
            status_to=PayrollSheet.Status.APPROVED,
            actor=actor,
            actor_role_snapshot=PayrollSheetLifecycleEvent.ActorRole.DIRECTOR,
            occurred_at=now,
        )
    return sheet
