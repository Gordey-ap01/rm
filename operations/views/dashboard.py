"""Дашборд и очередь задач администратора."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Count, Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from operations.forms import default_charge_amount
from operations.models import (
    ACTION_REQUIRED_BILLING_STATUSES,
    Appointment,
    AppointmentConfirmation,
    AppointmentStaffAssignment,
    BalanceAccount,
    TimeOffRequest,
)

from ._common import is_admin_user


def needs_billing_queryset():
    return (
        Appointment.objects.all()
        .filter(
            Q(status__in=ACTION_REQUIRED_BILLING_STATUSES)
            | ~Q(attendance_status=Appointment.AttendanceStatus.UNKNOWN)
        )
        .annotate(
            participant_count=Count("participants", distinct=True),
            undecided_participant_count=Count(
                "participants",
                filter=Q(participants__billing_decision=Appointment.BillingDecision.UNDECIDED),
                distinct=True,
            ),
        )
        .filter(
            Q(
                participant_count=0,
                billing_decision=Appointment.BillingDecision.UNDECIDED,
            )
            | Q(participant_count__gt=0, undecided_participant_count__gt=0)
        )
        .select_related("child", "staff_member", "service", "room", "billing_account")
        .prefetch_related(
            "participants__child",
            "participants__billing_account",
            "staff_assignments__staff_member",
        )
        .order_by("-starts_at")
    )


def needs_attendance_queryset(now=None):
    now = now or timezone.now()
    return (
        Appointment.objects.filter(ends_at__lt=now)
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
        .order_by("starts_at")
    )


def needs_transfer_queryset():
    return (
        Appointment.objects.filter(
            status__in=[Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW],
            rescheduled_to__isnull=True,
        )
        .select_related("child", "staff_member", "service", "room")
        .prefetch_related("participants__child", "staff_assignments__staff_member")
        .order_by("-starts_at")
    )


def low_balance_accounts():
    return [
        account
        for account in BalanceAccount.objects.select_related("child", "funding_source", "service")
        if account.is_low_balance
    ]


def quick_billing_account(appointment):
    participants = list(appointment.participants.all())
    if len(participants) == 1:
        return participants[0].billing_account
    if not participants:
        return appointment.billing_account
    return None


def attendance_summary_label(appointment):
    participants = list(appointment.participants.all())
    if not participants:
        return appointment.get_attendance_status_display()
    if len(participants) == 1:
        return participants[0].get_attendance_status_display()
    unknown = sum(
        1
        for participant in participants
        if participant.attendance_status == Appointment.AttendanceStatus.UNKNOWN
    )
    if unknown:
        return f"не отмечено участников: {unknown} из {len(participants)}"
    attended = sum(
        1
        for participant in participants
        if participant.attendance_status == Appointment.AttendanceStatus.ATTENDED
    )
    missed = sum(
        1
        for participant in participants
        if participant.attendance_status == Appointment.AttendanceStatus.MISSED
    )
    return f"пришли {attended}, не пришли {missed}"


def dashboard_focus_items(
    *,
    unresolved_billing: int,
    overdue_attendance: int,
    awaiting_transfer: int,
    confirmation_tasks: int,
    time_off_requests: int,
    low_balance_count: int,
):
    queue_url = reverse("work_queue")
    items = []
    if unresolved_billing:
        items.append(
            {
                "tone": "warning",
                "value": unresolved_billing,
                "title": "Решить списания",
                "detail": "Есть занятия без решения по оплате или участникам.",
                "href": queue_url,
            }
        )
    if overdue_attendance:
        items.append(
            {
                "tone": "warning",
                "value": overdue_attendance,
                "title": "Отметить факт",
                "detail": "Прошедшие занятия ждут отметки посещения.",
                "href": queue_url,
            }
        )
    if awaiting_transfer:
        items.append(
            {
                "tone": "danger",
                "value": awaiting_transfer,
                "title": "Перенести отмены",
                "detail": "Отмененные занятия еще не связаны с новым временем.",
                "href": queue_url,
            }
        )
    if confirmation_tasks:
        items.append(
            {
                "tone": "info",
                "value": confirmation_tasks,
                "title": "Проверить согласования",
                "detail": "Есть ожидающие, отклоненные или неотправленные подтверждения.",
                "href": queue_url,
            }
        )
    if time_off_requests:
        items.append(
            {
                "tone": "info",
                "value": time_off_requests,
                "title": "Разобрать отгулы",
                "detail": "Специалисты ждут решения по отсутствию.",
                "href": queue_url,
            }
        )
    if low_balance_count:
        items.append(
            {
                "tone": "warning",
                "value": low_balance_count,
                "title": "Пополнить балансы",
                "detail": "Есть счета с низким остатком.",
                "href": reverse("balances"),
            }
        )
    if not items:
        items.append(
            {
                "tone": "success",
                "value": 0,
                "title": "Критичных задач нет",
                "detail": "Проверьте календарь и подготовку завтрашнего дня.",
                "href": reverse("schedule"),
            }
        )
    return items


def work_queue_summary_items(
    *,
    needs_billing_count: int,
    needs_attendance_count: int,
    needs_transfer_count: int,
    low_balance_count: int,
    confirmation_count: int,
    time_off_count: int,
):
    return [
        {
            "label": "Решения по списанию",
            "value": needs_billing_count,
            "href": "#queue-billing",
            "tone": "warning" if needs_billing_count else "success",
            "detail": "Списать, не списывать или решить по участникам.",
        },
        {
            "label": "Факт посещения",
            "value": needs_attendance_count,
            "href": "#queue-attendance",
            "tone": "warning" if needs_attendance_count else "success",
            "detail": "Прошедшие занятия без отметки администратора.",
        },
        {
            "label": "Переносы",
            "value": needs_transfer_count,
            "href": "#queue-transfer",
            "tone": "danger" if needs_transfer_count else "success",
            "detail": "Отмененные занятия без нового времени.",
        },
        {
            "label": "Низкие балансы",
            "value": low_balance_count,
            "href": "#queue-balances",
            "tone": "warning" if low_balance_count else "success",
            "detail": "Счета, где скоро нечем будет списывать занятия.",
        },
        {
            "label": "Email-согласования",
            "value": confirmation_count,
            "href": "#queue-confirmations",
            "tone": "info" if confirmation_count else "success",
            "detail": "Ожидают ответа, отклонены или не отправлены.",
        },
        {
            "label": "Заявки специалистов",
            "value": time_off_count,
            "href": "#queue-time-off",
            "tone": "info" if time_off_count else "success",
            "detail": "Отпуска, отгулы и другие отсутствия.",
        },
    ]


def work_queue_next_action(summary_items: list[dict[str, object]]) -> dict[str, object]:
    for item in summary_items:
        if item["value"]:
            return {
                "label": "Следующее действие",
                "title": item["label"],
                "value": item["value"],
                "detail": item["detail"],
                "href": item["href"],
                "tone": item["tone"],
            }
    return {
        "label": "Следующее действие",
        "title": "Критичных задач нет",
        "value": 0,
        "detail": "Можно проверить календарь или подготовку завтрашнего дня.",
        "href": reverse("schedule"),
        "tone": "success",
    }


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
    today_appointments = (
        Appointment.objects.filter(starts_at__date=today)
        .select_related("child", "staff_member", "service", "room")
        .prefetch_related("participants__child", "staff_assignments__staff_member")
        .order_by("starts_at")
    )
    tomorrow_appointments = (
        Appointment.objects.filter(starts_at__date=tomorrow)
        .select_related("child", "staff_member", "service", "room")
        .prefetch_related("participants__child", "staff_assignments__staff_member")
        .order_by("starts_at")
    )
    staff_load = (
        AppointmentStaffAssignment.objects.filter(
            starts_at_snapshot__date__gte=today,
            starts_at_snapshot__date__lt=today + timedelta(days=14),
        )
        .values("staff_member__full_name")
        .annotate(total=Count("appointment_id", distinct=True))
        .order_by("staff_member__full_name")
    )
    low_balances = low_balance_accounts()
    dashboard_focus = dashboard_focus_items(
        unresolved_billing=unresolved_billing,
        overdue_attendance=overdue_attendance,
        awaiting_transfer=awaiting_transfer,
        confirmation_tasks=confirmation_tasks,
        time_off_requests=time_off_requests,
        low_balance_count=len(low_balances),
    )
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
            "dashboard_focus_items": dashboard_focus,
        },
    )


@login_required
@user_passes_test(is_admin_user)
def work_queue(request):
    needs_billing = list(needs_billing_queryset()[:40])
    for appointment in needs_billing:
        appointment.attendance_summary_label = attendance_summary_label(appointment)
        appointment.quick_billing_account = quick_billing_account(appointment)
        appointment.quick_charge_amount = (
            default_charge_amount(appointment.quick_billing_account, appointment)
            if appointment.quick_billing_account
            else None
        )
    needs_attendance = list(needs_attendance_queryset()[:40])
    for appointment in needs_attendance:
        appointment.attendance_summary_label = attendance_summary_label(appointment)
    needs_transfer = list(needs_transfer_queryset()[:40])
    for appointment in needs_transfer:
        appointment.attendance_summary_label = attendance_summary_label(appointment)
    confirmation_tasks = (
        AppointmentConfirmation.objects.filter(
            Q(status=AppointmentConfirmation.Status.DECLINED)
            | Q(delivery_status=AppointmentConfirmation.DeliveryStatus.FAILED)
            | Q(status=AppointmentConfirmation.Status.PENDING)
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
        .order_by("status", "-created_at")[:40]
    )
    time_off_requests = (
        TimeOffRequest.objects.filter(status=TimeOffRequest.Status.PENDING)
        .select_related("staff_member")
        .order_by("starts_on", "staff_member__full_name")[:40]
    )
    low_balances = low_balance_accounts()
    queue_summary = work_queue_summary_items(
        needs_billing_count=len(needs_billing),
        needs_attendance_count=len(needs_attendance),
        needs_transfer_count=len(needs_transfer),
        low_balance_count=len(low_balances),
        confirmation_count=len(confirmation_tasks),
        time_off_count=len(time_off_requests),
    )
    return render(
        request,
        "operations/work_queue.html",
        {
            "needs_billing": needs_billing,
            "needs_attendance": needs_attendance,
            "needs_transfer": needs_transfer,
            "low_balances": low_balances,
            "confirmation_tasks": confirmation_tasks,
            "time_off_requests": time_off_requests,
            "queue_summary_items": queue_summary,
            "queue_next_action": work_queue_next_action(queue_summary),
        },
    )
