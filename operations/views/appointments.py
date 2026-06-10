"""Занятия: создание, изменение, перенос, отмена, списание, детальная страница."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from operations.forms import (
    AppointmentCancelForm,
    AppointmentConfirmationSendForm,
    AppointmentForm,
    AppointmentMoveForm,
    BillingDecisionForm,
)
from operations.models import Appointment, LedgerEntry

from ._common import is_admin_user, safe_next_url
from .scheduling_helpers import suggested_transfer_slots


def appointment_detail_context(appointment, billing_form=None) -> dict:
    local_day = timezone.localtime(appointment.starts_at).date()
    related_child_appointments = (
        Appointment.objects.filter(
            child=appointment.child,
            starts_at__date__gte=local_day - timedelta(days=7),
            starts_at__date__lte=local_day + timedelta(days=14),
        )
        .exclude(pk=appointment.pk)
        .select_related("staff_member", "service", "room")
        .order_by("starts_at")[:12]
    )
    ledger_entries = LedgerEntry.objects.filter(appointment=appointment).select_related("account", "created_by")
    confirmations = appointment.confirmations.select_related("representative", "sent_by").order_by("-created_at")
    return {
        "appointment": appointment,
        "related_child_appointments": related_child_appointments,
        "suggested_slots": suggested_transfer_slots(appointment),
        "ledger_entries": ledger_entries,
        "confirmations": confirmations,
        "confirmation_form": AppointmentConfirmationSendForm(appointment=appointment),
        "billing_form": billing_form or BillingDecisionForm(appointment=appointment),
        "schedule_date": local_day,
    }


@login_required
@user_passes_test(is_admin_user)
def appointment_detail(request, pk: int):
    appointment = get_object_or_404(
        Appointment.objects.select_related("child", "staff_member", "service", "room", "billing_account"),
        pk=pk,
    )
    return render(request, "operations/appointment_detail.html", appointment_detail_context(appointment))


@login_required
@user_passes_test(is_admin_user)
def appointment_create(request):
    initial = {
        "date": request.GET.get("date") or timezone.localdate(),
        "duration_minutes": 30,
    }
    if request.GET.get("child_id"):
        initial["child"] = request.GET["child_id"]
    if request.GET.get("service_id"):
        initial["service"] = request.GET["service_id"]
    if request.GET.get("staff_id"):
        initial["staff_member"] = request.GET["staff_id"]
    if request.GET.get("room_id"):
        initial["room"] = request.GET["room_id"]
    if request.GET.get("time"):
        initial["time"] = request.GET["time"]

    if request.method == "POST":
        form = AppointmentForm(request.POST)
        if form.is_valid():
            try:
                appointment = form.save()
            except IntegrityError:
                form.add_error(None, "Не удалось сохранить: найден конфликт расписания.")
            else:
                messages.success(request, "Занятие создано.")
                day = timezone.localtime(appointment.starts_at).date()
                return redirect(f"{reverse('schedule')}?{urlencode({'date': day.isoformat()})}")
    else:
        form = AppointmentForm(initial=initial)
    return render(request, "operations/appointment_form.html", {"form": form, "title": "Создать занятие"})


@login_required
@user_passes_test(is_admin_user)
def appointment_edit(request, pk: int):
    appointment = get_object_or_404(Appointment, pk=pk)
    if request.method == "POST":
        form = AppointmentForm(request.POST, instance=appointment)
        if form.is_valid():
            try:
                appointment = form.save()
            except IntegrityError:
                form.add_error(None, "Не удалось сохранить: найден конфликт расписания.")
            else:
                messages.success(request, "Занятие обновлено.")
                return redirect("appointment_detail", pk=appointment.pk)
    else:
        form = AppointmentForm(instance=appointment)
    return render(
        request,
        "operations/appointment_form.html",
        {"form": form, "title": "Редактировать занятие", "appointment": appointment},
    )


@login_required
@user_passes_test(is_admin_user)
def appointment_move(request, pk: int):
    appointment = get_object_or_404(Appointment.objects.select_related("child", "service", "staff_member", "room"), pk=pk)
    local_start = timezone.localtime(appointment.starts_at)
    initial = {
        "date": request.GET.get("date") or local_start.date(),
        "time": request.GET.get("time") or local_start.time().replace(second=0, microsecond=0),
        "duration_minutes": appointment.duration_minutes,
        "staff_member": request.GET.get("staff_id") or appointment.staff_member_id,
        "room": request.GET.get("room_id") or appointment.room_id,
    }
    if request.method == "POST":
        form = AppointmentMoveForm(request.POST, appointment=appointment)
        if form.is_valid():
            try:
                new_appointment = form.save()
            except IntegrityError:
                form.add_error(None, "Не удалось перенести: найден конфликт расписания.")
            else:
                messages.success(request, "Занятие перенесено. Решение по списанию исходного занятия остается за администратором.")
                return redirect("appointment_detail", pk=new_appointment.pk)
    else:
        form = AppointmentMoveForm(appointment=appointment, initial=initial)
    return render(
        request,
        "operations/appointment_move.html",
        {
            "form": form,
            "appointment": appointment,
            "suggested_slots": suggested_transfer_slots(appointment),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def appointment_cancel(request, pk: int):
    appointment = get_object_or_404(Appointment, pk=pk)
    if request.method == "POST":
        form = AppointmentCancelForm(request.POST, appointment=appointment)
        if form.is_valid():
            form.save()
            messages.success(request, "Статус занятия изменен. Решение по списанию примите отдельно.")
            return redirect("appointment_detail", pk=appointment.pk)
    else:
        form = AppointmentCancelForm(appointment=appointment, initial={"status": Appointment.Status.CANCELLED})
    return render(
        request,
        "operations/appointment_cancel.html",
        {"form": form, "appointment": appointment},
    )


@login_required
@user_passes_test(is_admin_user)
def appointment_billing(request, pk: int):
    appointment = get_object_or_404(Appointment, pk=pk)
    if request.method == "POST":
        form = BillingDecisionForm(request.POST, appointment=appointment)
        if form.is_valid():
            form.save(request.user)
            messages.success(request, "Решение по списанию сохранено.")
            return redirect(safe_next_url(request, reverse("appointment_detail", args=[appointment.pk])))
        else:
            messages.error(request, "Решение по списанию не сохранено. Проверьте поля формы.")
            return render(
                request,
                "operations/appointment_detail.html",
                appointment_detail_context(appointment, billing_form=form),
                status=400,
            )
    return redirect("appointment_detail", pk=appointment.pk)
