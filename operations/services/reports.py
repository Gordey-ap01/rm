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
    BalanceAccount,
    Certificate,
    Discount,
    FundingSource,
    LedgerEntry,
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
        .order_by("starts_at")
    )
    needs_billing = list(
        Appointment.objects.filter(
            billing_decision=Appointment.BillingDecision.UNDECIDED,
        )
        .filter(
            Q(status__in=ACTION_REQUIRED_BILLING_STATUSES)
            | ~Q(attendance_status=Appointment.AttendanceStatus.UNKNOWN)
        )
        .select_related("child", "staff_member", "service", "room")
        .order_by("-starts_at")[:20]
    )
    needs_attendance = list(
        Appointment.objects.filter(
            status__in=[Appointment.Status.CONFIRMED, Appointment.Status.PROPOSED],
            attendance_status=Appointment.AttendanceStatus.UNKNOWN,
            ends_at__lt=timezone.now(),
        )
        .select_related("child", "staff_member", "service", "room")
        .order_by("starts_at")[:20]
    )
    pending_confirmations = list(
        AppointmentConfirmation.objects.filter(
            Q(delivery_status=AppointmentConfirmation.DeliveryStatus.FAILED)
            | Q(status=AppointmentConfirmation.Status.PENDING)
            | Q(status=AppointmentConfirmation.Status.DECLINED)
        )
        .select_related("appointment", "appointment__child", "appointment__staff_member")
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
        if account.current_balance <= 2
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


@dataclass
class Timesheet:
    staff: StaffMember
    date_from: date
    date_to: date
    rows: list[TimesheetRow]
    totals: TimesheetRow


def timesheet(staff: StaffMember, date_from: date, date_to: date) -> Timesheet:
    """Табель специалиста: сколько занятий каждого статуса по дням + итого часов."""
    if date_to < date_from:
        raise ValueError("Дата окончания не может быть раньше даты начала.")

    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(date_from, time.min), tz)
    end_dt = timezone.make_aware(datetime.combine(date_to, time.max), tz)

    qs = (
        Appointment.objects.filter(
            staff_member=staff,
            starts_at__gte=start_dt,
            starts_at__lte=end_dt,
        )
        .values("starts_at__date", "status")
        .annotate(total=Count("id"))
    )

    by_day: dict[date, dict[str, int]] = defaultdict(
        lambda: {"total": 0, "completed": 0, "cancelled": 0, "no_show": 0, "minutes": 0}
    )
    for row in qs:
        day = row["starts_at__date"]
        bucket = by_day[day]
        bucket["total"] += row["total"]
        if row["status"] == Appointment.Status.COMPLETED:
            bucket["completed"] += row["total"]
        elif row["status"] == Appointment.Status.CANCELLED:
            bucket["cancelled"] += row["total"]
        elif row["status"] == Appointment.Status.NO_SHOW:
            bucket["no_show"] += row["total"]

    for appt in Appointment.objects.filter(
        staff_member=staff,
        starts_at__gte=start_dt,
        starts_at__lte=end_dt,
        ends_at__isnull=False,
    ).only("starts_at", "ends_at"):
        day = timezone.localtime(appt.starts_at, tz).date()
        minutes = int((appt.ends_at - appt.starts_at).total_seconds() // 60)
        by_day[day]["minutes"] += max(minutes, 0)

    rows: list[TimesheetRow] = []
    totals = {"total": 0, "completed": 0, "cancelled": 0, "no_show": 0, "minutes": 0}
    cur = date_from
    while cur <= date_to:
        bucket = by_day.get(cur, {"total": 0, "completed": 0, "cancelled": 0, "no_show": 0, "minutes": 0})
        rows.append(
            TimesheetRow(
                date=cur,
                total=bucket["total"],
                completed=bucket["completed"],
                cancelled=bucket["cancelled"],
                no_show=bucket["no_show"],
                hours=Decimal(bucket["minutes"]) / Decimal(60),
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
        ),
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
class GrantReport:
    funding: FundingSource
    date_from: date
    date_to: date
    rows: list[GrantReportRow]
    totals: GrantReportRow
    certificates: list[Certificate] = field(default_factory=list)
    discounts: list[Discount] = field(default_factory=list)


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
        credits = (
            account.ledger_entries.filter(
                created_at__gte=start_dt, created_at__lte=end_dt, entry_type=LedgerEntry.EntryType.CREDIT
            ).aggregate(s=Sum("amount"))["s"]
            or Decimal("0")
        )
        debits = (
            account.ledger_entries.filter(
                created_at__gte=start_dt, created_at__lte=end_dt, entry_type=LedgerEntry.EntryType.DEBIT
            ).aggregate(s=Sum("amount"))["s"]
            or Decimal("0")
        )
        appointments_qs = account.appointments.filter(starts_at__gte=start_dt, starts_at__lte=end_dt)
        appointments_count = appointments_qs.count()
        planned_count = appointments_qs.filter(
            status__in=[Appointment.Status.CONFIRMED, Appointment.Status.PROPOSED, Appointment.Status.DRAFT]
        ).count()
        completed_count = appointments_qs.filter(status=Appointment.Status.COMPLETED).count()
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

    certificates = list(Certificate.objects.filter(child__balance_accounts__funding_source=funding).distinct())
    discounts = list(Discount.objects.filter(child__balance_accounts__funding_source=funding, is_active=True).distinct())

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
    )
