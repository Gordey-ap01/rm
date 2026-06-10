"""Подтверждения занятий: отправка писем и публичный ответ."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from operations.forms import AppointmentConfirmationSendForm, ConfirmationResponseForm
from operations.models import Appointment, AppointmentConfirmation
from operations.tasks import send_appointment_confirmation_email

from ._common import is_admin_user
from .appointments import appointment_detail_context


@login_required
@user_passes_test(is_admin_user)
def appointment_send_confirmation(request, pk: int):
    appointment = get_object_or_404(
        Appointment.objects.select_related("child", "child__primary_parent", "staff_member", "staff_member__user", "service", "room"),
        pk=pk,
    )
    if request.method != "POST":
        return redirect("appointment_detail", pk=appointment.pk)

    form = AppointmentConfirmationSendForm(request.POST, appointment=appointment)
    if form.is_valid():
        confirmation = form.save(request.user)
        send_appointment_confirmation_email.enqueue(confirmation.pk)
        if appointment.status == Appointment.Status.DRAFT:
            appointment.status = Appointment.Status.PROPOSED
            appointment.save(update_fields=["status", "updated_at"])
        messages.success(
            request,
            f"Письмо поставлено в очередь на {confirmation.email}.",
        )
    else:
        messages.error(request, "Письмо не отправлено. Проверьте адресата и текст.")
        return render(
            request,
            "operations/appointment_detail.html",
            appointment_detail_context(appointment) | {"confirmation_form": form},
            status=400,
        )
    return redirect("appointment_detail", pk=appointment.pk)


def appointment_confirmation_public(request, token):
    confirmation = get_object_or_404(
        AppointmentConfirmation.objects.select_related(
            "appointment",
            "appointment__child",
            "appointment__staff_member",
            "appointment__service",
            "appointment__room",
            "representative",
        ),
        token=token,
    )
    submitted = False
    if request.method == "POST":
        form = ConfirmationResponseForm(request.POST)
        if form.is_valid() and confirmation.status == AppointmentConfirmation.Status.PENDING:
            action = form.cleaned_data["action"]
            confirmation.status = (
                AppointmentConfirmation.Status.CONFIRMED
                if action == "confirm"
                else AppointmentConfirmation.Status.DECLINED
            )
            confirmation.response_note = form.cleaned_data.get("response_note", "").strip()
            confirmation.responded_at = timezone.now()
            confirmation.save(update_fields=["status", "response_note", "responded_at", "updated_at"])

            if (
                confirmation.status == AppointmentConfirmation.Status.CONFIRMED
                and confirmation.target_type
                in [AppointmentConfirmation.TargetType.REPRESENTATIVE, AppointmentConfirmation.TargetType.RECIPIENT]
                and confirmation.appointment.status in [Appointment.Status.DRAFT, Appointment.Status.PROPOSED]
            ):
                confirmation.appointment.status = Appointment.Status.CONFIRMED
                confirmation.appointment.save(update_fields=["status", "updated_at"])
            submitted = True
    else:
        form = ConfirmationResponseForm()

    return render(
        request,
        "operations/appointment_confirmation_public.html",
        {"confirmation": confirmation, "form": form, "submitted": submitted},
    )
