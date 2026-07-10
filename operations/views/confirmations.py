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
from .appointments import (
    appointment_detail_context,
    appointment_participants_label,
    appointment_staff_label,
)


def _confirmation_target_label(confirmation: AppointmentConfirmation) -> str:
    if confirmation.target_type == AppointmentConfirmation.TargetType.SPECIALIST:
        if confirmation.staff_assignment_id:
            return confirmation.staff_assignment.staff_member.full_name
        return appointment_staff_label(confirmation.appointment)
    if confirmation.target_type == AppointmentConfirmation.TargetType.REPRESENTATIVE:
        return str(confirmation.representative) if confirmation.representative else confirmation.email
    if confirmation.target_type == AppointmentConfirmation.TargetType.RECIPIENT:
        if confirmation.participant_id:
            return confirmation.participant.child.full_name
        return confirmation.appointment.child.full_name
    return confirmation.email


def _confirmation_next_action(confirmation: AppointmentConfirmation, submitted: bool) -> dict[str, str]:
    if submitted:
        return {
            "tone": "success",
            "label": "Статус",
            "title": "Ответ сохранен",
            "detail": "Администратор увидит решение в карточке занятия.",
            "href": "#confirmation-result",
        }
    if confirmation.status != AppointmentConfirmation.Status.PENDING:
        return {
            "tone": "info",
            "label": "Статус",
            "title": confirmation.get_status_display(),
            "detail": "По этой ссылке уже был отправлен ответ.",
            "href": "#confirmation-result",
        }
    return {
        "tone": "warning",
        "label": "Следующий шаг",
        "title": "Подтвердить или отклонить",
        "detail": "При отклонении можно оставить комментарий для администратора.",
        "href": "#confirmation-form",
    }


def _confirmation_summary_items(
    confirmation: AppointmentConfirmation,
    *,
    child_names: str,
    staff_names: str,
    target_label: str,
) -> list[dict[str, str]]:
    appointment = confirmation.appointment
    local_start = timezone.localtime(appointment.starts_at)
    local_end = timezone.localtime(appointment.ends_at)
    return [
        {
            "label": "Кому отправлено",
            "value": confirmation.get_target_type_display(),
            "hint": target_label,
        },
        {
            "label": "Получатель",
            "value": child_names,
            "hint": str(appointment.service),
        },
        {
            "label": "Дата и время",
            "value": f"{local_start:%d.%m.%Y %H:%M}",
            "hint": f"до {local_end:%H:%M}",
        },
        {
            "label": "Специалист",
            "value": staff_names,
            "hint": str(appointment.room) if appointment.room else "кабинет не указан",
        },
        {
            "label": "Ответ",
            "value": confirmation.get_status_display(),
            "hint": confirmation.responded_at.strftime("%d.%m.%Y %H:%M")
            if confirmation.responded_at
            else "ожидается",
        },
    ]


def _confirmation_control_items(
    confirmation: AppointmentConfirmation,
    *,
    submitted: bool,
) -> list[dict[str, str]]:
    if submitted:
        return [
            {
                "tone": "success",
                "title": "Ответ принят",
                "text": "Повторное подтверждение по этой ссылке не требуется.",
            }
        ]
    if confirmation.status != AppointmentConfirmation.Status.PENDING:
        return [
            {
                "tone": "info",
                "title": "Ссылка уже использована",
                "text": "Если решение нужно изменить, свяжитесь с администратором центра.",
            }
        ]
    return [
        {
            "tone": "info",
            "title": "Подтверждение фиксируется в системе",
            "text": "После ответа администратор увидит статус в карточке занятия.",
        },
        {
            "tone": "warning",
            "title": "Отклонение не отменяет занятие автоматически",
            "text": "Администратор вручную решит, переносить, отменять или согласовывать занятие дальше.",
        },
    ]


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
            "participant__child",
            "representative",
            "staff_assignment__staff_member",
            "reschedule_step",
        ),
        token=token,
    )
    participants = list(
        confirmation.appointment.participants.select_related("child").order_by(
            "starts_at_snapshot", "child__last_name", "child__first_name"
        )
    )
    if confirmation.participant_id:
        child_names = confirmation.participant.child.full_name
    else:
        child_names = appointment_participants_label(confirmation.appointment, participants)
    staff_names = (
        confirmation.staff_assignment.staff_member.full_name
        if confirmation.staff_assignment_id
        else appointment_staff_label(confirmation.appointment)
    )
    target_label = _confirmation_target_label(confirmation)
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
            if confirmation.reschedule_step_id:
                from operations.services import rescheduling_plans as plan_svc

                plan_svc.refresh_step_confirmation_status(confirmation.reschedule_step)

            if (
                confirmation.status == AppointmentConfirmation.Status.CONFIRMED
                and confirmation.reschedule_step_id is None
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
        {
            "confirmation": confirmation,
            "form": form,
            "submitted": submitted,
            "child_names": child_names,
            "staff_names": staff_names,
            "confirmation_target_label": target_label,
            "confirmation_next_action": _confirmation_next_action(confirmation, submitted),
            "confirmation_summary_items": _confirmation_summary_items(
                confirmation,
                child_names=child_names,
                staff_names=staff_names,
                target_label=target_label,
            ),
            "confirmation_control_items": _confirmation_control_items(
                confirmation,
                submitted=submitted,
            ),
        },
    )
