"""Фоновые задачи (django-tasks)."""

from __future__ import annotations

from django.conf import settings
from django.core.mail import send_mail
from django_tasks import task

from .models import AppointmentConfirmation


@task
def send_appointment_confirmation_email(confirmation_id: int) -> bool:
    """Отправляет письмо с подтверждением занятия и помечает ``AppointmentConfirmation``.

    Возвращает ``True`` при успехе, ``False`` при ошибке доставки.
    """
    from django.urls import reverse
    from django.utils import timezone
    try:
        confirmation = AppointmentConfirmation.objects.select_related("appointment").get(pk=confirmation_id)
    except AppointmentConfirmation.DoesNotExist:
        return False
    if not confirmation.email:
        confirmation.delivery_status = AppointmentConfirmation.DeliveryStatus.FAILED
        confirmation.delivery_error = "Email получателя не указан"
        confirmation.save(update_fields=["delivery_status", "delivery_error", "updated_at"])
        return False
    try:
        confirmation_url = reverse(
            "appointment_confirmation_public", args=[confirmation.token]
        )
        body = f"{confirmation.message}\n\nСсылка для ответа: {confirmation_url}"
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
        return False
    confirmation.delivery_status = AppointmentConfirmation.DeliveryStatus.SENT
    confirmation.sent_at = timezone.now()
    confirmation.save(update_fields=["delivery_status", "sent_at", "updated_at"])
    return True
