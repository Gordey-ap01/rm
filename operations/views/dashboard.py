"""Дашборд и очередь задач администратора."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.utils import timezone

from operations.forms import default_charge_amount
from operations.models import (
    ACTION_REQUIRED_BILLING_STATUSES,
    Appointment,
    AppointmentConfirmation,
    BalanceAccount,
    TimeOffRequest,
)

from ._common import is_admin_user


def needs_billing_queryset():
    return (
        Appointment.objects.filter(billing_decision=Appointment.BillingDecision.UNDECIDED)
        .filter(Q(status__in=ACTION_REQUIRED_BILLING_STATUSES) | ~Q(attendance_status=Appointment.AttendanceStatus.UNKNOWN))
        .select_related("child", "staff_member", "service", "room", "billing_account")
        .order_by("-starts_at")
    )


def needs_attendance_queryset(now=None):
    now = now or timezone.now()
    return (
        Appointment.objects.filter(
            status__in=[Appointment.Status.CONFIRMED, Appointment.Status.PROPOSED],
            attendance_status=Appointment.AttendanceStatus.UNKNOWN,
            ends_at__lt=now,
        )
        .select_related("child", "staff_member", "service", "room")
        .order_by("starts_at")
    )


def needs_transfer_queryset():
    return (
        Appointment.objects.filter(
            status__in=[Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW],
            rescheduled_to__isnull=True,
        )
        .select_related("child", "staff_member", "service", "room")
        .order_by("-starts_at")
    )


def low_balance_accounts():
    return [
        account
        for account in BalanceAccount.objects.select_related("child", "funding_source", "service")
        if account.current_balance <= 2
    ]


@login_required
def dashboard(request):
    if not request.user.is_staff:
        return redirect("specialist_home")

    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)
    unresolved_billing = needs_billing_queryset().count()
    awaiting_transfer = needs_transfer_queryset().count()
    overdue_attendance = needs_attendance_queryset().count()
    confirmation_tasks = AppointmentConfirmation.objects.filter(
        Q(status=AppointmentConfirmation.Status.DECLINED)
        | Q(delivery_status=AppointmentConfirmation.DeliveryStatus.FAILED)
        | Q(status=AppointmentConfirmation.Status.PENDING)
    ).count()
    time_off_requests = TimeOffRequest.objects.filter(status=TimeOffRequest.Status.PENDING).count()
    priority_total = (
        unresolved_billing
        + awaiting_transfer
        + overdue_attendance
        + confirmation_tasks
        + time_off_requests
    )
    today_appointments = Appointment.objects.filter(starts_at__date=today).select_related(
        "child", "staff_member", "service", "room"
    ).order_by("starts_at")
    tomorrow_appointments = Appointment.objects.filter(starts_at__date=tomorrow).select_related(
        "child", "staff_member", "service", "room"
    ).order_by("starts_at")
    staff_load = (
        Appointment.objects.filter(starts_at__date__gte=today, starts_at__date__lt=today + timedelta(days=14))
        .values("staff_member__full_name")
        .annotate(total=Count("id"))
        .order_by("staff_member__full_name")
    )
    low_balances = low_balance_accounts()
    return render(
        request,
        "operations/dashboard.html",
        {
            "today": today,
            "tomorrow": tomorrow,
            "today_appointments": today_appointments,
            "unresolved_billing": unresolved_billing,
            "awaiting_transfer": awaiting_transfer,
            "overdue_attendance": overdue_attendance,
            "confirmation_tasks": confirmation_tasks,
            "time_off_requests": time_off_requests,
            "priority_total": priority_total,
            "staff_load": staff_load,
            "low_balances": low_balances,
            "tomorrow_appointments": tomorrow_appointments,
        },
    )


@login_required
@user_passes_test(is_admin_user)
def work_queue(request):
    needs_billing = list(needs_billing_queryset()[:40])
    for appointment in needs_billing:
        appointment.quick_charge_amount = (
            default_charge_amount(appointment.billing_account, appointment)
            if appointment.billing_account_id
            else None
        )
    confirmation_tasks = (
        AppointmentConfirmation.objects.filter(
            Q(status=AppointmentConfirmation.Status.DECLINED)
            | Q(delivery_status=AppointmentConfirmation.DeliveryStatus.FAILED)
            | Q(status=AppointmentConfirmation.Status.PENDING)
        )
        .select_related("appointment", "appointment__child", "appointment__staff_member", "appointment__service")
        .order_by("status", "-created_at")[:40]
    )
    time_off_requests = (
        TimeOffRequest.objects.filter(status=TimeOffRequest.Status.PENDING)
        .select_related("staff_member")
        .order_by("starts_on", "staff_member__full_name")[:40]
    )
    return render(
        request,
        "operations/work_queue.html",
        {
            "needs_billing": needs_billing,
            "needs_attendance": needs_attendance_queryset()[:40],
            "needs_transfer": needs_transfer_queryset()[:40],
            "low_balances": low_balance_accounts(),
            "confirmation_tasks": confirmation_tasks,
            "time_off_requests": time_off_requests,
        },
    )
