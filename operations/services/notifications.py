"""Уведомления: отправка писем-подтверждений, шаблоны."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.urls import reverse
from django.utils import timezone

from operations.models import Appointment


@dataclass(frozen=True)
class ConfirmationEmail:
    subject: str
    body: str
    url: str


def build_confirmation_email(
    appointment: Appointment, *, request: Any | None = None
) -> ConfirmationEmail:
    """Собирает subject/body/URL для отправки подтверждения по занятию.

    Используется в view, которая создаёт ``AppointmentConfirmation`` и зовёт эту функцию.
    """
    local_start = timezone.localtime(appointment.starts_at)
    participants = list(
        appointment.participants.exclude(
            appointment_status__in=[
                Appointment.Status.CANCELLED,
                Appointment.Status.RESCHEDULED,
            ]
        ).select_related("child").order_by(
            "starts_at_snapshot", "child__last_name", "child__first_name"
        )
    )
    if participants:
        child_names = ", ".join(participant.child.full_name for participant in participants)
    else:
        child_names = appointment.child.full_name
    assignments = list(
        appointment.staff_assignments.select_related("staff_member").order_by(
            "starts_at_snapshot", "staff_member__full_name"
        )
    )
    if assignments:
        staff_names = ", ".join(
            assignment.staff_member.full_name for assignment in assignments
        )
    else:
        staff_names = appointment.staff_member.full_name
    subject = f"Подтверждение занятия {local_start:%d.%m.%Y %H:%M}"
    body = "\n".join(
        [
            "Здравствуйте.",
            "",
            "Просим подтвердить занятие:",
            f"Получатель: {child_names}",
            f"Услуга: {appointment.service.name}",
            f"Специалист: {staff_names}",
            f"Дата и время: {local_start:%d.%m.%Y %H:%M}",
            f"Кабинет: {appointment.room.name if appointment.room else 'не указан'}",
            "",
            "Ответьте по ссылке ниже: подтвердить или отклонить.",
        ]
    )
    token = getattr(appointment, "_pending_token", None)
    if token is not None:
        path = reverse("appointment_confirmation_public", args=[token])
    else:
        path = reverse("appointment_detail", args=[appointment.pk])
    url = request.build_absolute_uri(path) if request is not None else path
    return ConfirmationEmail(subject=subject, body=body, url=url)


def send_confirmation_email(confirmation_id: int) -> bool:
    """Send via the durable queue shared with the background task."""
    from operations.services.confirmation_email_outbox import send_confirmation

    return send_confirmation(confirmation_id)
