"""Отчёты: экран «Завтра», табель специалиста, грант-отчёт."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, Q, Sum
from django.utils import timezone

from operations.models import (
    ACTION_REQUIRED_BILLING_STATUSES,
    Appointment,
    AppointmentConfirmation,
    AppointmentParticipant,
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
            undecided_participant_count=Count(
                "participants",
                filter=Q(participants__billing_decision=Appointment.BillingDecision.UNDECIDED),
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
            | Q(participant_count__gt=0, undecided_participant_count__gt=0)
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
            unknown_participant_count=Count(
                "participants",
                filter=Q(participants__attendance_status=Appointment.AttendanceStatus.UNKNOWN),
                distinct=True,
            ),
        )
        .filter(
            Q(
                status__in=[Appointment.Status.CONFIRMED, Appointment.Status.PROPOSED],
                attendance_status=Appointment.AttendanceStatus.UNKNOWN,
            )
            | Q(participant_count__gt=0, unknown_participant_count__gt=0)
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
    pending_time_off = list(
        TimeOffRequest.objects.filter(status=TimeOffRequest.Status.PENDING)
        .select_related("staff_member")
        .order_by("starts_on", "staff_member__full_name")[:20]
    )
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


def _billing_decision_label(appointment: Appointment) -> str:
    participants = list(appointment.participants.all())
    if not participants:
        return appointment.get_billing_decision_display()
    if len(participants) == 1:
        return participants[0].get_billing_decision_display()

    counts: dict[str, int] = defaultdict(int)
    for participant in participants:
        counts[participant.billing_decision] += 1
    parts = []
    if counts[Appointment.BillingDecision.CHARGE]:
        parts.append(f"списать: {counts[Appointment.BillingDecision.CHARGE]}")
    if counts[Appointment.BillingDecision.DO_NOT_CHARGE]:
        parts.append(f"не списывать: {counts[Appointment.BillingDecision.DO_NOT_CHARGE]}")
    if counts[Appointment.BillingDecision.UNDECIDED]:
        parts.append(f"не решено: {counts[Appointment.BillingDecision.UNDECIDED]}")
    return ", ".join(parts) if parts else appointment.get_billing_decision_display()


def _charged_funding_source(appointment: Appointment) -> tuple[bool, FundingSource | None, str]:
    participants = list(appointment.participants.all())
    charged_participants = [
        participant
        for participant in participants
        if participant.billing_decision == Appointment.BillingDecision.CHARGE
        and participant.billing_account_id
    ]
    if charged_participants:
        participant = sorted(charged_participants, key=lambda item: item.pk)[0]
        source_ids = {
            participant.billing_account.funding_source_id for participant in charged_participants
        }
        if len(source_ids) == 1:
            source = participant.billing_account.funding_source
            return True, source, f"списано участников: {len(charged_participants)}"
        return True, None, (
            f"списано участников: {len(charged_participants)}; "
            "смешанные источники финансирования"
        )

    if participants:
        return False, None, "нет решения «Списать» по участникам"

    if (
        appointment.billing_decision == Appointment.BillingDecision.CHARGE
        and appointment.billing_account_id
    ):
        source = appointment.billing_account.funding_source
        return True, source, "списано по занятию"

    return False, None, "нет решения «Списать»"


def _charged_funding_source_ids(appointment: Appointment) -> set[int]:
    participants = list(appointment.participants.all())
    source_ids = {
        participant.billing_account.funding_source_id
        for participant in participants
        if participant.billing_decision == Appointment.BillingDecision.CHARGE
        and participant.billing_account_id
    }
    if participants:
        return source_ids
    if (
        appointment.billing_decision == Appointment.BillingDecision.CHARGE
        and appointment.billing_account_id
    ):
        source_ids.add(appointment.billing_account.funding_source_id)
    return source_ids


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


def _compensation_amount(rule: StaffCompensationRule | None, minutes: int) -> Decimal:
    if rule is None:
        return Decimal("0")
    if rule.rate_type == StaffCompensationRule.RateType.HOURLY:
        return (rule.amount * Decimal(minutes) / Decimal(60)).quantize(Decimal("0.01"))
    return rule.amount


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

        is_charged, funding_source, note = _charged_funding_source(appointment)
        rule = None
        amount = Decimal("0")
        rate_label = ""
        if is_charged:
            bucket["payable"] += 1
            allocation = _matching_grant_allocation(
                staff=staff,
                service_id=appointment.service_id,
                funding_source_id=funding_source.pk if funding_source else None,
                work_date=day,
            )
            if allocation is not None:
                amount = allocation.session_pay_amount
                rate_label = f"{allocation.session_pay_amount} / грантовая квота"
                note = f"{note}; ставка из распределения грантовой квоты"
            else:
                rule = _matching_compensation_rule(
                    rules,
                    service_id=appointment.service_id,
                    funding_source_id=funding_source.pk if funding_source else None,
                    work_date=day,
                    duration_minutes=max(minutes, 0),
                )
                if rule is None:
                    note = f"{note}; ставка не задана"
                else:
                    amount = _compensation_amount(rule, max(minutes, 0))
                    rate_label = f"{rule.amount} / {rule.get_rate_type_display()}"
            bucket["pay_amount_cents"] += int((amount * Decimal("100")).to_integral_value())

        pay_lines.append(
            TimesheetPayLine(
                assignment=assignment,
                appointment=appointment,
                date=day,
                starts_at=assignment.starts_at_snapshot,
                ends_at=assignment.ends_at_snapshot,
                service_name=appointment.service.name,
                funding_source=funding_source,
                status=appointment.get_status_display(),
                billing_decision=_billing_decision_label(appointment),
                payable=is_charged,
                rule=rule,
                amount=amount,
                note=note,
                rate_label=rate_label,
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
        is_charged, funding_source, note = _charged_funding_source(appointment)
        rule = None
        amount = Decimal("0")
        rate_label = ""
        if is_charged:
            bucket["payable"] += 1
            allocation = _matching_grant_allocation(
                staff=staff,
                service_id=appointment.service_id,
                funding_source_id=funding_source.pk if funding_source else None,
                work_date=day,
            )
            if allocation is not None:
                amount = allocation.session_pay_amount
                rate_label = f"{allocation.session_pay_amount} / грантовая квота"
                note = f"{note}; ставка из распределения грантовой квоты"
            else:
                rule = _matching_compensation_rule(
                    rules,
                    service_id=appointment.service_id,
                    funding_source_id=funding_source.pk if funding_source else None,
                    work_date=day,
                    duration_minutes=max(minutes, 0),
                )
                if rule is None:
                    note = f"{note}; ставка не задана"
                else:
                    amount = _compensation_amount(rule, max(minutes, 0))
                    rate_label = f"{rule.amount} / {rule.get_rate_type_display()}"
            bucket["pay_amount_cents"] += int((amount * Decimal("100")).to_integral_value())
        pay_lines.append(
            TimesheetPayLine(
                assignment=None,
                appointment=appointment,
                date=day,
                starts_at=appointment.starts_at,
                ends_at=appointment.ends_at,
                service_name=appointment.service.name,
                funding_source=funding_source,
                status=appointment.get_status_display(),
                billing_decision=_billing_decision_label(appointment),
                payable=is_charged,
                rule=rule,
                amount=amount,
                note=note,
                rate_label=rate_label,
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
    initial_amount: Decimal
    topups: Decimal
    charges: Decimal
    current_balance: Decimal
    appointments_count: int
    planned_count: int = 0
    completed_count: int = 0
    discount_count: int = 0
    certificate_count: int = 0


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
    totals: GrantReportRow
    certificates: list[Certificate] = field(default_factory=list)
    discounts: list[Discount] = field(default_factory=list)
    quota_rows: list[GrantQuotaRow] = field(default_factory=list)
    recipient_allocation_rows: list[GrantRecipientAllocationRow] = field(default_factory=list)


GRANT_REPORT_PLANNED_STATUSES = {
    Appointment.Status.CONFIRMED,
    Appointment.Status.PROPOSED,
    Appointment.Status.DRAFT,
    Appointment.Status.RESERVED,
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
) -> list[GrantQuotaRow]:
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
        return []

    assignments = list(
        AppointmentStaffAssignment.objects.filter(
            starts_at_snapshot__gte=start_dt,
            starts_at_snapshot__lte=end_dt,
            appointment__service_id__in=service_ids,
        )
        .select_related("appointment", "appointment__service", "staff_member")
        .prefetch_related("appointment__participants__billing_account__funding_source")
        .order_by("starts_at_snapshot", "appointment_id")
    )
    charged_assignments: list[AppointmentStaffAssignment] = []
    for assignment in assignments:
        appointment = assignment.appointment
        if funding.pk not in _charged_funding_source_ids(appointment):
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
                    remaining_sessions=max(allocation.allocated_sessions - charged_sessions, 0),
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
                remaining_sessions=max(quota.planned_sessions - charged_sessions, 0),
                staff_rows=staff_rows,
            )
        )

    direct_by_service: dict[int, list[FundingStaffAllocation]] = defaultdict(list)
    for allocation in direct_allocations:
        direct_by_service[allocation.service_id].append(allocation)
    quota_service_ids = {quota.service_id for quota in quotas}
    for service_id, allocations in direct_by_service.items():
        if service_id in quota_service_ids:
            continue
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
                    remaining_sessions=max(allocation.allocated_sessions - charged_sessions, 0),
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
                remaining_sessions=max(allocated_sessions - charged_sessions, 0),
                staff_rows=staff_rows,
            )
        )

    return rows


def _grant_recipient_allocation_rows(
    funding: FundingSource,
    *,
    date_from: date,
    date_to: date,
    start_dt: datetime,
    end_dt: datetime,
) -> list[GrantRecipientAllocationRow]:
    allocations = list(
        GrantRecipientAllocation.objects.filter(funding_source=funding)
        .filter(Q(valid_from__isnull=True) | Q(valid_from__lte=date_to))
        .filter(Q(valid_until__isnull=True) | Q(valid_until__gte=date_from))
        .select_related("child", "service", "balance_account")
        .order_by("service__name", "child__last_name", "child__first_name", "pk")
    )
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
        ledger_qs = allocation.balance_account.ledger_entries.none()
        if charge_start <= charge_end:
            ledger_qs = allocation.balance_account.ledger_entries.filter(
                created_at__gte=charge_start,
                created_at__lte=charge_end,
                entry_type=LedgerEntry.EntryType.DEBIT,
                appointment__service=allocation.service,
            )
        charged = ledger_qs.aggregate(total=Sum("amount"))["total"] or Decimal("0")
        rows.append(
            GrantRecipientAllocationRow(
                allocation=allocation,
                child=allocation.child,
                service=allocation.service,
                balance_account=allocation.balance_account,
                allocated_sessions=allocation.allocated_sessions,
                charged_sessions=abs(charged),
                remaining_sessions=allocation.balance_account.current_balance,
            )
        )
    return rows


def _account_appointment_statuses(
    account: BalanceAccount,
    *,
    start_dt: datetime,
    end_dt: datetime,
) -> dict[int, str]:
    statuses_by_appointment = dict(
        account.appointments.filter(
            starts_at__gte=start_dt,
            starts_at__lte=end_dt,
        ).values_list("id", "status")
    )
    participant_rows = AppointmentParticipant.objects.filter(
        billing_account=account,
        starts_at_snapshot__gte=start_dt,
        starts_at_snapshot__lte=end_dt,
    ).values_list("appointment_id", "appointment_status")
    for appointment_id, status in participant_rows:
        statuses_by_appointment[appointment_id] = status
    return statuses_by_appointment


def grant_report(funding: FundingSource, date_from: date, date_to: date) -> GrantReport:
    """Грант-отчёт: по каждому счёту источника — начальный остаток, пополнения, списания, текущий.

    Добавлены план vs факт (запланировано / проведено), скидки и сертификаты.
    """
    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(date_from, time.min), tz)
    end_dt = timezone.make_aware(datetime.combine(date_to, time.max), tz)

    accounts = list(
        BalanceAccount.objects.filter(funding_source=funding).select_related("child", "service")
    )
    rows: list[GrantReportRow] = []
    totals: dict[str, Any] = {
        "initial": Decimal("0"),
        "topups": Decimal("0"),
        "charges": Decimal("0"),
        "current": Decimal("0"),
        "appointments": 0,
        "planned": 0,
        "completed": 0,
        "discounts": 0,
        "certificates": 0,
    }
    for account in accounts:
        credits = account.ledger_entries.filter(
            created_at__gte=start_dt,
            created_at__lte=end_dt,
            entry_type=LedgerEntry.EntryType.CREDIT,
        ).aggregate(s=Sum("amount"))["s"] or Decimal("0")
        debits = account.ledger_entries.filter(
            created_at__gte=start_dt, created_at__lte=end_dt, entry_type=LedgerEntry.EntryType.DEBIT
        ).aggregate(s=Sum("amount"))["s"] or Decimal("0")
        appointment_statuses = _account_appointment_statuses(
            account,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        appointments_count = len(appointment_statuses)
        planned_count = sum(
            1 for status in appointment_statuses.values() if status in GRANT_REPORT_PLANNED_STATUSES
        )
        completed_count = sum(
            1 for status in appointment_statuses.values() if status == Appointment.Status.COMPLETED
        )
        current = account.current_balance

        child = account.child
        discount_count = Discount.objects.filter(child=child, is_active=True).count()
        certificate_count = Certificate.objects.filter(child=child).count()

        row = GrantReportRow(
            account=account,
            initial_amount=account.initial_amount,
            topups=credits,
            charges=abs(debits),
            current_balance=current,
            appointments_count=appointments_count,
            planned_count=planned_count,
            completed_count=completed_count,
            discount_count=discount_count,
            certificate_count=certificate_count,
        )
        rows.append(row)
        totals["initial"] += account.initial_amount
        totals["topups"] += credits
        totals["charges"] += abs(debits)
        totals["current"] += current
        totals["appointments"] += appointments_count
        totals["planned"] += planned_count
        totals["completed"] += completed_count
        totals["discounts"] += discount_count
        totals["certificates"] += certificate_count

    certificates = list(
        Certificate.objects.filter(child__balance_accounts__funding_source=funding).distinct()
    )
    discounts = list(
        Discount.objects.filter(
            child__balance_accounts__funding_source=funding, is_active=True
        ).distinct()
    )
    quota_rows = _grant_quota_rows(
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
    )

    return GrantReport(
        funding=funding,
        date_from=date_from,
        date_to=date_to,
        rows=rows,
        totals=GrantReportRow(
            account=None,  # type: ignore[arg-type]
            initial_amount=totals["initial"],
            topups=totals["topups"],
            charges=totals["charges"],
            current_balance=totals["current"],
            appointments_count=totals["appointments"],
            planned_count=totals["planned"],
            completed_count=totals["completed"],
            discount_count=totals["discounts"],
            certificate_count=totals["certificates"],
        ),
        certificates=certificates,
        discounts=discounts,
        quota_rows=quota_rows,
        recipient_allocation_rows=recipient_allocation_rows,
    )
