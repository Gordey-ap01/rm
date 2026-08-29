"""Отчёты: экран «Завтра», табель специалиста, грант-отчёт."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Case, Count, DateTimeField, F, Prefetch, Q, Sum, When
from django.utils import timezone

from operations.models import (
    ACTION_REQUIRED_BILLING_STATUSES,
    Appointment,
    AppointmentConfirmation,
    AppointmentParticipant,
    AppointmentSeriesCancellationResult,
    AppointmentStaffAssignment,
    BalanceAccount,
    Certificate,
    Discount,
    FundingServiceQuota,
    FundingSource,
    FundingStaffAllocation,
    GrantRecipientAllocation,
    LedgerEntry,
    StaffCompensationRule,
    StaffMember,
    TimeOffRequest,
)
from operations.services.compensation import calculate_staff_compensation
from operations.services.financial_facts import appointment_charge_fact
from operations.services.time_off_decisions import attention_rows as time_off_attention_rows


@dataclass
class TomorrowOverview:
    date: date
    appointments: list[Appointment]
    needs_billing: list[Appointment]
    needs_attendance: list[Appointment]
    pending_confirmations: list[AppointmentConfirmation]
    pending_time_off: list[TimeOffRequest]
    low_balances: list[BalanceAccount]
    summary: dict[str, int] = field(default_factory=dict)


def tomorrow_overview(target_date: date | None = None) -> TomorrowOverview:
    """Сводка для экрана «Завтра».

    Возвращает 5 разделов: занятия дня, ожидают списания, не отмечены по факту,
    непрочитанные подтверждения, заявки специалистов.
    """
    target = target_date or (timezone.localdate() + timedelta(days=1))
    tz = timezone.get_current_timezone()
    day_start = timezone.make_aware(datetime.combine(target, time.min), tz)
    day_end = day_start + timedelta(days=1)

    appointments = list(
        Appointment.objects.filter(starts_at__gte=day_start, starts_at__lt=day_end)
        .select_related("child", "staff_member", "service", "room")
        .prefetch_related("participants__child", "staff_assignments__staff_member")
        .order_by("starts_at")
    )
    needs_billing = list(
        Appointment.objects.all()
        .annotate(
            participant_count=Count("participants", distinct=True),
            operational_participant_count=Count(
                "participants",
                filter=Q(participants__series_withdrawal_results__isnull=True)
                & ~Q(
                    participants__appointment_status__in=[
                        Appointment.Status.CANCELLED,
                        Appointment.Status.RESCHEDULED,
                    ]
                ),
                distinct=True,
            ),
            undecided_participant_count=Count(
                "participants",
                filter=Q(
                    participants__billing_decision=Appointment.BillingDecision.UNDECIDED,
                    participants__series_withdrawal_results__isnull=True,
                )
                & ~Q(
                    participants__appointment_status__in=[
                        Appointment.Status.CANCELLED,
                        Appointment.Status.RESCHEDULED,
                    ]
                ),
                distinct=True,
            ),
        )
        .filter(
            Q(status__in=ACTION_REQUIRED_BILLING_STATUSES)
            | ~Q(attendance_status=Appointment.AttendanceStatus.UNKNOWN)
        )
        .filter(
            Q(
                participant_count=0,
                billing_decision=Appointment.BillingDecision.UNDECIDED,
            )
            | Q(
                operational_participant_count__gt=0,
                undecided_participant_count__gt=0,
            )
        )
        .select_related("child", "staff_member", "service", "room")
        .prefetch_related(
            "participants__child",
            "participants__billing_account",
            "staff_assignments__staff_member",
        )
        .order_by("-starts_at")[:20]
    )
    needs_attendance = list(
        Appointment.objects.filter(ends_at__lt=timezone.now())
        .annotate(
            participant_count=Count("participants", distinct=True),
            operational_participant_count=Count(
                "participants",
                filter=Q(participants__series_withdrawal_results__isnull=True)
                & ~Q(
                    participants__appointment_status__in=[
                        Appointment.Status.CANCELLED,
                        Appointment.Status.RESCHEDULED,
                    ]
                ),
                distinct=True,
            ),
            unknown_participant_count=Count(
                "participants",
                filter=Q(
                    participants__attendance_status=Appointment.AttendanceStatus.UNKNOWN,
                    participants__series_withdrawal_results__isnull=True,
                )
                & ~Q(
                    participants__appointment_status__in=[
                        Appointment.Status.CANCELLED,
                        Appointment.Status.RESCHEDULED,
                    ]
                ),
                distinct=True,
            ),
            unresolved_series_result_count=Count(
                "participants__series_withdrawal_results",
                filter=Q(participants__series_withdrawal_results__isnull=False)
                & ~Q(
                    participants__series_withdrawal_results__outcome=(
                        AppointmentSeriesCancellationResult.Outcome.CANCELLED
                    )
                ),
                distinct=True,
            ),
        )
        .filter(unresolved_series_result_count=0)
        .filter(
            Q(
                participant_count=0,
                status__in=[Appointment.Status.CONFIRMED, Appointment.Status.PROPOSED],
                attendance_status=Appointment.AttendanceStatus.UNKNOWN,
            )
            | Q(
                operational_participant_count__gt=0,
                unknown_participant_count__gt=0,
            )
        )
        .select_related("child", "staff_member", "service", "room")
        .prefetch_related("participants__child", "staff_assignments__staff_member")
        .order_by("starts_at")[:20]
    )
    pending_confirmations = list(
        AppointmentConfirmation.objects.filter(
            Q(delivery_status=AppointmentConfirmation.DeliveryStatus.FAILED)
            | Q(status=AppointmentConfirmation.Status.PENDING)
            | Q(status=AppointmentConfirmation.Status.DECLINED)
        )
        .filter(
            Q(participant__isnull=True)
            | (
                Q(participant__series_withdrawal_results__isnull=True)
                & ~Q(
                    participant__appointment_status__in=[
                        Appointment.Status.CANCELLED,
                        Appointment.Status.RESCHEDULED,
                    ]
                )
            )
        )
        .distinct()
        .select_related(
            "appointment",
            "appointment__child",
            "appointment__staff_member",
            "appointment__service",
            "participant__child",
            "staff_assignment__staff_member",
        )
        .prefetch_related("appointment__participants__child")
        .order_by("status", "-created_at")[:20]
    )
    pending_time_off = time_off_attention_rows(limit=20)
    low_balances = [
        account
        for account in BalanceAccount.objects.select_related("child", "funding_source", "service")
        if account.is_low_balance
    ]

    return TomorrowOverview(
        date=target,
        appointments=appointments,
        needs_billing=needs_billing,
        needs_attendance=needs_attendance,
        pending_confirmations=pending_confirmations,
        pending_time_off=pending_time_off,
        low_balances=low_balances,
        summary={
            "appointments_count": len(appointments),
            "needs_billing_count": len(needs_billing),
            "needs_attendance_count": len(needs_attendance),
            "pending_confirmations_count": len(pending_confirmations),
            "pending_time_off_count": len(pending_time_off),
            "low_balances_count": len(low_balances),
        },
    )


@dataclass
class TimesheetRow:
    date: date
    total: int
    completed: int
    cancelled: int
    no_show: int
    hours: Decimal
    payable: int = 0
    pay_amount: Decimal = Decimal("0")


@dataclass
class TimesheetPayLine:
    assignment: AppointmentStaffAssignment | None
    appointment: Appointment
    date: date
    starts_at: datetime
    ends_at: datetime
    service_name: str
    funding_source: FundingSource | None
    status: str
    billing_decision: str
    payable: bool
    rule: StaffCompensationRule | None
    amount: Decimal
    note: str
    rate_label: str = ""
    group_pay_policy: str = StaffCompensationRule.GroupPayPolicy.PER_SESSION
    charged_participants_count: int = 1
    pay_units: int = 1

    @property
    def has_rate(self) -> bool:
        return bool(self.rate_label)


@dataclass
class Timesheet:
    staff: StaffMember
    date_from: date
    date_to: date
    rows: list[TimesheetRow]
    totals: TimesheetRow
    pay_lines: list[TimesheetPayLine] = field(default_factory=list)


def timesheet(staff: StaffMember, date_from: date, date_to: date) -> Timesheet:
    """Табель специалиста: сколько занятий каждого статуса по дням + итого часов."""
    if date_to < date_from:
        raise ValueError("Дата окончания не может быть раньше даты начала.")

    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(date_from, time.min), tz)
    end_dt = timezone.make_aware(datetime.combine(date_to, time.max), tz)

    assignments = list(
        AppointmentStaffAssignment.objects.filter(
            staff_member=staff,
            starts_at_snapshot__gte=start_dt,
            starts_at_snapshot__lte=end_dt,
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
    by_day: dict[date, dict[str, int]] = defaultdict(
        lambda: {
            "total": 0,
            "completed": 0,
            "cancelled": 0,
            "no_show": 0,
            "minutes": 0,
            "payable": 0,
            "pay_amount_cents": 0,
        }
    )
    rules = list(
        StaffCompensationRule.objects.filter(staff_member=staff, is_active=True)
        .select_related("staff_member", "service", "funding_source")
        .order_by("staff_member__full_name", "service__name", "funding_source__name")
    )
    pay_lines: list[TimesheetPayLine] = []

    for assignment in assignments:
        appointment = assignment.appointment
        day = timezone.localtime(assignment.starts_at_snapshot, tz).date()
        minutes = int(
            (assignment.ends_at_snapshot - assignment.starts_at_snapshot).total_seconds() // 60
        )
        bucket = by_day[day]
        bucket["total"] += 1
        if assignment.appointment_status == Appointment.Status.COMPLETED:
            bucket["completed"] += 1
        elif assignment.appointment_status == Appointment.Status.CANCELLED:
            bucket["cancelled"] += 1
        elif assignment.appointment_status == Appointment.Status.NO_SHOW:
            bucket["no_show"] += 1
        by_day[day]["minutes"] += max(minutes, 0)

        charge_fact = appointment_charge_fact(appointment, include_ledger=False)
        compensation = calculate_staff_compensation(
            staff=staff,
            appointment=appointment,
            charge_fact=charge_fact,
            rules=rules,
            work_date=day,
            duration_minutes=max(minutes, 0),
        )
        if compensation.payable:
            bucket["payable"] += 1
            bucket["pay_amount_cents"] += int(
                (compensation.amount * Decimal("100")).to_integral_value()
            )

        pay_lines.append(
            TimesheetPayLine(
                assignment=assignment,
                appointment=appointment,
                date=day,
                starts_at=assignment.starts_at_snapshot,
                ends_at=assignment.ends_at_snapshot,
                service_name=appointment.service.name,
                funding_source=compensation.funding_source,
                status=appointment.get_status_display(),
                billing_decision=charge_fact.billing_decision_label,
                payable=compensation.payable,
                rule=compensation.rule,
                amount=compensation.amount,
                note=compensation.note,
                rate_label=compensation.rate_label,
                group_pay_policy=compensation.group_pay_policy,
                charged_participants_count=compensation.charged_participants_count,
                pay_units=compensation.pay_units,
            )
        )

    legacy_appointments = (
        Appointment.objects.filter(
            staff_member=staff,
            starts_at__gte=start_dt,
            starts_at__lte=end_dt,
        )
        .exclude(staff_assignments__staff_member=staff)
        .select_related("service", "billing_account__funding_source")
        .prefetch_related("participants__billing_account__funding_source")
        .distinct()
    )
    for appointment in legacy_appointments:
        day = timezone.localtime(appointment.starts_at, tz).date()
        minutes = int((appointment.ends_at - appointment.starts_at).total_seconds() // 60)
        bucket = by_day[day]
        bucket["total"] += 1
        if appointment.status == Appointment.Status.COMPLETED:
            bucket["completed"] += 1
        elif appointment.status == Appointment.Status.CANCELLED:
            bucket["cancelled"] += 1
        elif appointment.status == Appointment.Status.NO_SHOW:
            bucket["no_show"] += 1
        by_day[day]["minutes"] += max(minutes, 0)
        charge_fact = appointment_charge_fact(appointment, include_ledger=False)
        compensation = calculate_staff_compensation(
            staff=staff,
            appointment=appointment,
            charge_fact=charge_fact,
            rules=rules,
            work_date=day,
            duration_minutes=max(minutes, 0),
        )
        if compensation.payable:
            bucket["payable"] += 1
            bucket["pay_amount_cents"] += int(
                (compensation.amount * Decimal("100")).to_integral_value()
            )
        pay_lines.append(
            TimesheetPayLine(
                assignment=None,
                appointment=appointment,
                date=day,
                starts_at=appointment.starts_at,
                ends_at=appointment.ends_at,
                service_name=appointment.service.name,
                funding_source=compensation.funding_source,
                status=appointment.get_status_display(),
                billing_decision=charge_fact.billing_decision_label,
                payable=compensation.payable,
                rule=compensation.rule,
                amount=compensation.amount,
                note=compensation.note,
                rate_label=compensation.rate_label,
                group_pay_policy=compensation.group_pay_policy,
                charged_participants_count=compensation.charged_participants_count,
                pay_units=compensation.pay_units,
            )
        )

    rows: list[TimesheetRow] = []
    totals = {
        "total": 0,
        "completed": 0,
        "cancelled": 0,
        "no_show": 0,
        "minutes": 0,
        "payable": 0,
        "pay_amount_cents": 0,
    }
    cur = date_from
    while cur <= date_to:
        bucket = by_day.get(
            cur,
            {
                "total": 0,
                "completed": 0,
                "cancelled": 0,
                "no_show": 0,
                "minutes": 0,
                "payable": 0,
                "pay_amount_cents": 0,
            },
        )
        rows.append(
            TimesheetRow(
                date=cur,
                total=bucket["total"],
                completed=bucket["completed"],
                cancelled=bucket["cancelled"],
                no_show=bucket["no_show"],
                hours=Decimal(bucket["minutes"]) / Decimal(60),
                payable=bucket["payable"],
                pay_amount=Decimal(bucket["pay_amount_cents"]) / Decimal(100),
            )
        )
        for k in totals:
            totals[k] += bucket[k]
        cur += timedelta(days=1)

    return Timesheet(
        staff=staff,
        date_from=date_from,
        date_to=date_to,
        rows=rows,
        totals=TimesheetRow(
            date=date_to,
            total=totals["total"],
            completed=totals["completed"],
            cancelled=totals["cancelled"],
            no_show=totals["no_show"],
            hours=Decimal(totals["minutes"]) / Decimal(60),
            payable=totals["payable"],
            pay_amount=Decimal(totals["pay_amount_cents"]) / Decimal(100),
        ),
        pay_lines=pay_lines,
    )


@dataclass
class GrantReportRow:
    account: BalanceAccount
    opening_balance: Decimal
    inflows: Decimal
    outflows: Decimal
    closing_balance: Decimal
    current_balance: Decimal
    appointments_count: int
    planned_count: int = 0
    completed_count: int = 0
    discount_count: int = 0
    certificate_count: int = 0


@dataclass
class GrantReportUnitTotals:
    unit: str
    unit_label: str
    opening_balance: Decimal
    inflows: Decimal
    outflows: Decimal
    closing_balance: Decimal
    current_balance: Decimal
    appointments_count: int
    planned_count: int
    completed_count: int
    discount_count: int
    certificate_count: int


@dataclass
class GrantReportTotals:
    appointments_count: int
    planned_count: int
    completed_count: int
    discount_count: int
    certificate_count: int


@dataclass
class GrantStaffQuotaRow:
    allocation: FundingStaffAllocation
    staff_member: StaffMember
    allocated_sessions: int
    charged_sessions: int
    remaining_sessions: int
    session_pay_amount: Decimal | None = None


@dataclass
class GrantQuotaRow:
    quota: FundingServiceQuota | None
    service: Any
    planned_sessions: int
    allocated_sessions: int
    charged_sessions: int
    remaining_sessions: int
    staff_rows: list[GrantStaffQuotaRow] = field(default_factory=list)


@dataclass
class GrantRecipientAllocationRow:
    allocation: GrantRecipientAllocation
    child: Any
    service: Any
    balance_account: BalanceAccount
    allocated_sessions: int
    charged_sessions: Decimal
    remaining_sessions: Decimal


@dataclass
class GrantReport:
    funding: FundingSource
    date_from: date
    date_to: date
    rows: list[GrantReportRow]
    totals: GrantReportTotals
    unit_totals: list[GrantReportUnitTotals]
    certificates: list[Certificate] = field(default_factory=list)
    discounts: list[Discount] = field(default_factory=list)
    quota_rows: list[GrantQuotaRow] = field(default_factory=list)
    recipient_allocation_rows: list[GrantRecipientAllocationRow] = field(default_factory=list)
    quota_missing_debit_count: int = 0


GRANT_REPORT_PLANNED_STATUSES = {
    Appointment.Status.CONFIRMED,
    Appointment.Status.PROPOSED,
    Appointment.Status.DRAFT,
    Appointment.Status.RESERVED,
    Appointment.Status.COMPLETED,
    Appointment.Status.NO_SHOW,
}


def _date_range_matches(
    starts_on: date | None, ends_on: date | None, date_from: date, date_to: date
) -> bool:
    if starts_on and starts_on > date_to:
        return False
    return not (ends_on and ends_on < date_from)


def _charged_sessions_for_allocation(
    allocation: FundingStaffAllocation,
    charged_assignments: list[AppointmentStaffAssignment],
) -> int:
    count = 0
    for assignment in charged_assignments:
        if assignment.staff_member_id != allocation.staff_member_id:
            continue
        if assignment.appointment.service_id != allocation.service_id:
            continue
        work_date = timezone.localtime(assignment.starts_at_snapshot).date()
        if allocation.starts_on and work_date < allocation.starts_on:
            continue
        if allocation.ends_on and work_date > allocation.ends_on:
            continue
        count += 1
    return count


def _work_date_matches(
    work_date: date,
    starts_on: date | None,
    ends_on: date | None,
) -> bool:
    if starts_on and work_date < starts_on:
        return False
    return not (ends_on and work_date > ends_on)


def _ledger_effective_at_expression() -> Case:
    return Case(
        When(
            entry_type=LedgerEntry.EntryType.DEBIT,
            appointment_participant__isnull=False,
            then=F("appointment_participant__starts_at_snapshot"),
        ),
        When(
            entry_type=LedgerEntry.EntryType.DEBIT,
            appointment__isnull=False,
            then=F("appointment__starts_at"),
        ),
        default=F("created_at"),
        output_field=DateTimeField(),
    )


def _account_period_balance_deltas(
    funding: FundingSource,
    *,
    start_dt: datetime,
    end_dt: datetime,
) -> dict[int, dict[str, Decimal | None]]:
    rows = (
        LedgerEntry.objects.filter(account__funding_source=funding)
        .alias(grant_effective_at=_ledger_effective_at_expression())
        .values("account_id")
        .annotate(
            current_delta=Sum("amount"),
            opening_delta=Sum(
                "amount",
                filter=Q(grant_effective_at__lt=start_dt),
            ),
            inflows=Sum(
                "amount",
                filter=Q(
                    grant_effective_at__gte=start_dt,
                    grant_effective_at__lte=end_dt,
                    amount__gt=0,
                ),
            ),
            outflow_delta=Sum(
                "amount",
                filter=Q(
                    grant_effective_at__gte=start_dt,
                    grant_effective_at__lte=end_dt,
                    amount__lt=0,
                ),
            ),
        )
    )
    return {row["account_id"]: row for row in rows}


def _account_period_balances(
    account: BalanceAccount,
    balance_deltas: dict[int, dict[str, Decimal | None]],
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
    deltas = balance_deltas.get(account.pk, {})
    opening = account.initial_amount + (deltas.get("opening_delta") or Decimal("0"))
    inflows = deltas.get("inflows") or Decimal("0")
    outflows = abs(deltas.get("outflow_delta") or Decimal("0"))
    current = account.initial_amount + (deltas.get("current_delta") or Decimal("0"))
    closing = opening + inflows - outflows
    return opening, inflows, outflows, closing, current


def _charged_appointment_ids_for_quota(
    quota: FundingServiceQuota,
    charged_assignments: list[AppointmentStaffAssignment],
) -> set[int]:
    appointment_ids = set()
    for assignment in charged_assignments:
        if assignment.appointment.service_id != quota.service_id:
            continue
        work_date = timezone.localtime(assignment.starts_at_snapshot).date()
        if not _work_date_matches(work_date, quota.starts_on, quota.ends_on):
            continue
        appointment_ids.add(assignment.appointment_id)
    return appointment_ids


def _charged_appointment_ids_for_allocations(
    allocations: list[FundingStaffAllocation],
    charged_assignments: list[AppointmentStaffAssignment],
) -> set[int]:
    appointment_ids = set()
    for allocation in allocations:
        for assignment in charged_assignments:
            if assignment.staff_member_id != allocation.staff_member_id:
                continue
            if assignment.appointment.service_id != allocation.service_id:
                continue
            work_date = timezone.localtime(assignment.starts_at_snapshot).date()
            if not _work_date_matches(work_date, allocation.starts_on, allocation.ends_on):
                continue
            appointment_ids.add(assignment.appointment_id)
    return appointment_ids


def _grant_quota_rows(
    funding: FundingSource,
    *,
    date_from: date,
    date_to: date,
    start_dt: datetime,
    end_dt: datetime,
) -> tuple[list[GrantQuotaRow], int]:
    quotas = list(
        FundingServiceQuota.objects.filter(funding_source=funding)
        .filter(Q(starts_on__isnull=True) | Q(starts_on__lte=date_to))
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=date_from))
        .select_related("service")
        .prefetch_related("staff_allocations__staff_member")
        .order_by("service__name", "starts_on", "pk")
    )
    direct_allocations = list(
        FundingStaffAllocation.objects.filter(funding_source=funding, service_quota__isnull=True)
        .filter(Q(starts_on__isnull=True) | Q(starts_on__lte=date_to))
        .filter(Q(ends_on__isnull=True) | Q(ends_on__gte=date_from))
        .select_related("service", "staff_member")
        .order_by("service__name", "staff_member__full_name", "pk")
    )
    service_ids = {quota.service_id for quota in quotas}
    service_ids.update(allocation.service_id for allocation in direct_allocations)
    if not service_ids:
        return [], 0

    participant_qs = AppointmentParticipant.objects.select_related(
        "billing_account__funding_source"
    )

    assignments = list(
        AppointmentStaffAssignment.objects.filter(
            starts_at_snapshot__gte=start_dt,
            starts_at_snapshot__lte=end_dt,
            appointment__service_id__in=service_ids,
        )
        .select_related("appointment", "appointment__service", "staff_member")
        .prefetch_related(
            Prefetch(
                "appointment__participants",
                queryset=participant_qs,
            )
        )
        .order_by("starts_at_snapshot", "appointment_id")
    )
    appointment_ids = {assignment.appointment_id for assignment in assignments}
    debited_appointment_ids = set(
        LedgerEntry.objects.filter(
            account__funding_source=funding,
            appointment_id__in=appointment_ids,
            entry_type=LedgerEntry.EntryType.DEBIT,
        ).values_list("appointment_id", flat=True)
    )
    charged_assignments: list[AppointmentStaffAssignment] = []
    decision_charged_appointment_ids: set[int] = set()
    for assignment in assignments:
        appointment = assignment.appointment
        if (
            funding.pk
            not in appointment_charge_fact(
                appointment,
                include_ledger=False,
            ).funding_source_ids
        ):
            continue
        decision_charged_appointment_ids.add(appointment.pk)
        if appointment.pk not in debited_appointment_ids:
            continue
        charged_assignments.append(assignment)

    rows: list[GrantQuotaRow] = []
    for quota in quotas:
        allocations = [
            allocation
            for allocation in quota.staff_allocations.all()
            if _date_range_matches(allocation.starts_on, allocation.ends_on, date_from, date_to)
        ]
        staff_rows = []
        for allocation in allocations:
            charged_sessions = _charged_sessions_for_allocation(allocation, charged_assignments)
            staff_rows.append(
                GrantStaffQuotaRow(
                    allocation=allocation,
                    staff_member=allocation.staff_member,
                    allocated_sessions=allocation.allocated_sessions,
                    charged_sessions=charged_sessions,
                    remaining_sessions=allocation.allocated_sessions - charged_sessions,
                    session_pay_amount=allocation.session_pay_amount,
                )
            )
        allocated_sessions = sum(row.allocated_sessions for row in staff_rows)
        charged_sessions = len(_charged_appointment_ids_for_quota(quota, charged_assignments))
        rows.append(
            GrantQuotaRow(
                quota=quota,
                service=quota.service,
                planned_sessions=quota.planned_sessions,
                allocated_sessions=allocated_sessions,
                charged_sessions=charged_sessions,
                remaining_sessions=quota.planned_sessions - charged_sessions,
                staff_rows=staff_rows,
            )
        )

    direct_by_service: dict[int, list[FundingStaffAllocation]] = defaultdict(list)
    for allocation in direct_allocations:
        direct_by_service[allocation.service_id].append(allocation)
    for _service_id, allocations in direct_by_service.items():
        service = allocations[0].service
        staff_rows = []
        for allocation in allocations:
            charged_sessions = _charged_sessions_for_allocation(allocation, charged_assignments)
            staff_rows.append(
                GrantStaffQuotaRow(
                    allocation=allocation,
                    staff_member=allocation.staff_member,
                    allocated_sessions=allocation.allocated_sessions,
                    charged_sessions=charged_sessions,
                    remaining_sessions=allocation.allocated_sessions - charged_sessions,
                    session_pay_amount=allocation.session_pay_amount,
                )
            )
        allocated_sessions = sum(row.allocated_sessions for row in staff_rows)
        charged_sessions = len(
            _charged_appointment_ids_for_allocations(allocations, charged_assignments)
        )
        rows.append(
            GrantQuotaRow(
                quota=None,
                service=service,
                planned_sessions=allocated_sessions,
                allocated_sessions=allocated_sessions,
                charged_sessions=charged_sessions,
                remaining_sessions=allocated_sessions - charged_sessions,
                staff_rows=staff_rows,
            )
        )

    return rows, len(decision_charged_appointment_ids - debited_appointment_ids)


def _grant_recipient_allocation_rows(
    funding: FundingSource,
    *,
    date_from: date,
    date_to: date,
    start_dt: datetime,
    end_dt: datetime,
    closing_balances: dict[int, Decimal],
) -> list[GrantRecipientAllocationRow]:
    allocations = list(
        GrantRecipientAllocation.objects.filter(funding_source=funding)
        .filter(Q(valid_from__isnull=True) | Q(valid_from__lte=date_to))
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gte=date_from))
        .select_related("child", "service", "balance_account")
        .order_by("service__name", "child__last_name", "child__first_name", "pk")
    )
    account_ids = {allocation.balance_account_id for allocation in allocations}
    service_ids = {allocation.service_id for allocation in allocations}
    period_debits: dict[tuple[int, int], list[tuple[datetime, Decimal]]] = defaultdict(list)
    if account_ids and service_ids:
        debit_rows = (
            LedgerEntry.objects.filter(
                account_id__in=account_ids,
                appointment__service_id__in=service_ids,
                entry_type=LedgerEntry.EntryType.DEBIT,
            )
            .annotate(grant_effective_at=_ledger_effective_at_expression())
            .filter(
                grant_effective_at__gte=start_dt,
                grant_effective_at__lte=end_dt,
            )
            .values_list(
                "account_id",
                "appointment__service_id",
                "grant_effective_at",
                "amount",
            )
        )
        for account_id, service_id, effective_at, amount in debit_rows:
            period_debits[(account_id, service_id)].append((effective_at, amount))

    rows: list[GrantRecipientAllocationRow] = []
    for allocation in allocations:
        charge_start = start_dt
        charge_end = end_dt
        if allocation.valid_from:
            charge_start = max(
                charge_start,
                timezone.make_aware(datetime.combine(allocation.valid_from, time.min)),
            )
        if allocation.valid_until:
            charge_end = min(
                charge_end,
                timezone.make_aware(datetime.combine(allocation.valid_until, time.max)),
            )
        charged = sum(
            (
                amount
                for effective_at, amount in period_debits.get(
                    (allocation.balance_account_id, allocation.service_id),
                    [],
                )
                if charge_start <= effective_at <= charge_end
            ),
            Decimal("0"),
        )
        rows.append(
            GrantRecipientAllocationRow(
                allocation=allocation,
                child=allocation.child,
                service=allocation.service,
                balance_account=allocation.balance_account,
                allocated_sessions=allocation.allocated_sessions,
                charged_sessions=abs(charged),
                remaining_sessions=closing_balances.get(
                    allocation.balance_account_id,
                    allocation.balance_account.initial_amount,
                ),
            )
        )
    return rows


def _appointment_statuses_by_account(
    account_ids: set[int],
    *,
    start_dt: datetime,
    end_dt: datetime,
) -> dict[int, dict[int, str]]:
    statuses_by_account: dict[int, dict[int, str]] = defaultdict(dict)
    appointment_rows = Appointment.objects.filter(
        billing_account_id__in=account_ids,
        starts_at__gte=start_dt,
        starts_at__lte=end_dt,
    ).values_list("billing_account_id", "id", "status")
    for account_id, appointment_id, status in appointment_rows:
        statuses_by_account[account_id][appointment_id] = status

    participant_rows = AppointmentParticipant.objects.filter(
        billing_account_id__in=account_ids,
        starts_at_snapshot__gte=start_dt,
        starts_at_snapshot__lte=end_dt,
    ).values_list("billing_account_id", "appointment_id", "appointment_status")
    for account_id, appointment_id, status in participant_rows:
        statuses_by_account[account_id][appointment_id] = status
    return statuses_by_account


def grant_report(funding: FundingSource, date_from: date, date_to: date) -> GrantReport:
    """Build period balances, quota utilization and recipient grant allocations."""
    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(date_from, time.min), tz)
    end_dt = timezone.make_aware(datetime.combine(date_to, time.max), tz)

    accounts = list(
        BalanceAccount.all_objects.filter(funding_source=funding).select_related(
            "child", "service"
        )
    )
    account_ids = {account.pk for account in accounts}
    child_ids = {account.child_id for account in accounts}
    balance_deltas = _account_period_balance_deltas(
        funding,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    appointment_statuses_by_account = _appointment_statuses_by_account(
        account_ids,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    certificates = list(Certificate.objects.filter(child_id__in=child_ids))
    discounts = list(Discount.objects.filter(child_id__in=child_ids, is_active=True))
    certificate_ids_by_child: dict[int, set[int]] = defaultdict(set)
    discount_ids_by_child: dict[int, set[int]] = defaultdict(set)
    for certificate in certificates:
        certificate_ids_by_child[certificate.child_id].add(certificate.pk)
    for discount in discounts:
        discount_ids_by_child[discount.child_id].add(discount.pk)

    rows: list[GrantReportRow] = []
    closing_balances: dict[int, Decimal] = {}
    totals: dict[str, int] = {
        "appointments": 0,
        "planned": 0,
        "completed": 0,
    }
    unit_totals: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "opening": Decimal("0"),
            "inflows": Decimal("0"),
            "outflows": Decimal("0"),
            "closing": Decimal("0"),
            "current": Decimal("0"),
            "appointments": 0,
            "planned": 0,
            "completed": 0,
            "discount_ids": set(),
            "certificate_ids": set(),
        }
    )
    for account in accounts:
        opening, inflows, outflows, closing, current = _account_period_balances(
            account,
            balance_deltas,
        )
        closing_balances[account.pk] = closing
        appointment_statuses = appointment_statuses_by_account.get(account.pk, {})
        appointments_count = len(appointment_statuses)
        planned_count = sum(
            1 for status in appointment_statuses.values() if status in GRANT_REPORT_PLANNED_STATUSES
        )
        completed_count = sum(
            1 for status in appointment_statuses.values() if status == Appointment.Status.COMPLETED
        )
        discount_ids = discount_ids_by_child[account.child_id]
        certificate_ids = certificate_ids_by_child[account.child_id]
        discount_count = len(discount_ids)
        certificate_count = len(certificate_ids)

        row = GrantReportRow(
            account=account,
            opening_balance=opening,
            inflows=inflows,
            outflows=outflows,
            closing_balance=closing,
            current_balance=current,
            appointments_count=appointments_count,
            planned_count=planned_count,
            completed_count=completed_count,
            discount_count=discount_count,
            certificate_count=certificate_count,
        )
        rows.append(row)
        unit_total = unit_totals[account.unit]
        unit_total["opening"] += opening
        unit_total["inflows"] += inflows
        unit_total["outflows"] += outflows
        unit_total["closing"] += closing
        unit_total["current"] += current
        unit_total["appointments"] += appointments_count
        unit_total["planned"] += planned_count
        unit_total["completed"] += completed_count
        unit_total["discount_ids"].update(discount_ids)
        unit_total["certificate_ids"].update(certificate_ids)
        totals["appointments"] += appointments_count
        totals["planned"] += planned_count
        totals["completed"] += completed_count

    quota_rows, quota_missing_debit_count = _grant_quota_rows(
        funding,
        date_from=date_from,
        date_to=date_to,
        start_dt=start_dt,
        end_dt=end_dt,
    )
    recipient_allocation_rows = _grant_recipient_allocation_rows(
        funding,
        date_from=date_from,
        date_to=date_to,
        start_dt=start_dt,
        end_dt=end_dt,
        closing_balances=closing_balances,
    )

    return GrantReport(
        funding=funding,
        date_from=date_from,
        date_to=date_to,
        rows=rows,
        totals=GrantReportTotals(
            appointments_count=totals["appointments"],
            planned_count=totals["planned"],
            completed_count=totals["completed"],
            discount_count=len(discounts),
            certificate_count=len(certificates),
        ),
        unit_totals=[
            GrantReportUnitTotals(
                unit=unit,
                unit_label=BalanceAccount.Unit(unit).label,
                opening_balance=values["opening"],
                inflows=values["inflows"],
                outflows=values["outflows"],
                closing_balance=values["closing"],
                current_balance=values["current"],
                appointments_count=values["appointments"],
                planned_count=values["planned"],
                completed_count=values["completed"],
                discount_count=len(values["discount_ids"]),
                certificate_count=len(values["certificate_ids"]),
            )
            for unit, values in sorted(
                unit_totals.items(),
                key=lambda item: list(BalanceAccount.Unit.values).index(item[0]),
            )
        ],
        certificates=certificates,
        discounts=discounts,
        quota_rows=quota_rows,
        recipient_allocation_rows=recipient_allocation_rows,
        quota_missing_debit_count=quota_missing_debit_count,
    )
