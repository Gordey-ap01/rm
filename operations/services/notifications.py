"""Уведомления: отправка писем-подтверждений, шаблоны."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone

from operations.models import Appointment, AppointmentConfirmation

logger = logging.getLogger(__name__)


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
    subject = f"Подтверждение занятия {local_start:%d.%m.%Y %H:%M}"
    body = "\n".join(
        [
            "Здравствуйте.",
            "",
            "Просим подтвердить занятие:",
            f"Получатель: {appointment.child.full_name}",
            f"Услуга: {appointment.service.name}",
            f"Специалист: {appointment.staff_member.full_name}",
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
    """Отправляет письмо для существующего ``AppointmentConfirmation``.

    Возвращает ``True`` при успехе. При ошибке обновляет
    ``delivery_status=FAILED`` и ``delivery_error`` и возвращает ``False``.
    Используется как задача :py:mod:`operations.tasks` (django-tasks).
    """
    confirmation = (
        AppointmentConfirmation.objects.select_related(
            "appointment",
            "appointment__child",
            "appointment__staff_member",
            "appointment__service",
            "appointment__room",
        )
        .filter(pk=confirmation_id)
        .first()
    )
    if confirmation is None:
        logger.warning("Confirmation %s disappeared before send", confirmation_id)
        return False

    email = build_confirmation_email(confirmation.appointment)
    body = f"{email.body}\n\nСсылка для ответа: {email.url}"
    try:
        send_mail(
            confirmation.subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [confirmation.email],
            fail_silently=False,
        )
    except Exception as exc:
        confirmation.delivery_status = AppointmentConfirmation.DeliveryStatus.FAILED
        confirmation.delivery_error = str(exc)
        confirmation.save(update_fields=["delivery_status", "delivery_error", "updated_at"])
        logger.exception("Confirmation email failed: %s", confirmation_id)
        return False

    confirmation.delivery_status = AppointmentConfirmation.DeliveryStatus.SENT
    confirmation.sent_at = timezone.now()
    confirmation.save(update_fields=["delivery_status", "sent_at", "updated_at"])

    if confirmation.appointment.status == Appointment.Status.DRAFT:
        confirmation.appointment.status = Appointment.Status.PROPOSED
        confirmation.appointment.save(update_fields=["status", "updated_at"])
    return True
