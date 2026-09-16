"""Durable confirmation emails. SMTP is at-least-once, never exactly-once."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage, get_connection
from django.core.validators import URLValidator, validate_email
from django.db import connection, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from operations.models import Appointment, AppointmentConfirmation, ConfirmationEmailDelivery
from operations.services import appointments as appointment_svc, schedule_decisions

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
LEASE_SECONDS = 300
TERMINAL_APPOINTMENT_STATUSES = {
    Appointment.Status.CANCELLED,
    Appointment.Status.RESCHEDULED,
    Appointment.Status.COMPLETED,
    Appointment.Status.NO_SHOW,
}


def _confirmation(confirmation_id):
    return (
        AppointmentConfirmation.objects.select_related(
            "appointment__child",
            "appointment__staff_member",
            "appointment__service",
            "appointment__room",
            "participant",
            "staff_assignment",
            "representative",
            "reschedule_step__plan",
            "reschedule_step__proposed_room",
            "reschedule_step__proposed_primary_staff",
        )
        .filter(pk=confirmation_id)
        .first()
    )


def _fingerprint(confirmation):
    appointment = confirmation.appointment
    values = {
        "email": confirmation.email,
        "subject": confirmation.subject,
        "message": confirmation.message,
        "token": str(confirmation.token),
        "target": confirmation.target_type,
        "participant": confirmation.participant_id,
        "assignment": confirmation.staff_assignment_id,
        "representative": confirmation.representative_id,
        "representative_name": confirmation.representative.full_name
        if confirmation.representative_id
        else None,
        "labels": [
            appointment.child.full_name,
            appointment.staff_member.full_name,
            appointment.service.name,
            appointment.room.name if appointment.room_id else None,
        ],
        "step": confirmation.reschedule_step_id,
        "appointment": [
            appointment.pk,
            str(appointment.starts_at),
            str(appointment.ends_at),
            appointment.child_id,
            appointment.staff_member_id,
            appointment.service_id,
            appointment.room_id,
        ],
        "participants": list(
            appointment.participants.exclude(
                appointment_status__in=TERMINAL_APPOINTMENT_STATUSES,
            )
            .order_by("pk")
            .values_list(
                "pk", "child_id", "child__last_name", "child__first_name", "child__middle_name"
            )
        ),
        "staff": list(
            appointment.staff_assignments.exclude(
                appointment_status__in=TERMINAL_APPOINTMENT_STATUSES,
            )
            .order_by("pk")
            .values_list("pk", "staff_member_id", "staff_member__full_name")
        ),
    }
    if confirmation.reschedule_step_id:
        step = confirmation.reschedule_step
        values["proposal"] = [
            str(step.proposed_starts_at),
            str(step.proposed_ends_at),
            step.proposed_primary_staff_id,
            step.proposed_room_id,
            step.proposed_primary_staff.full_name if step.proposed_primary_staff_id else None,
            step.proposed_room.name if step.proposed_room_id else None,
        ]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _public_url(confirmation, base_url):
    origin = getattr(settings, "RM_PUBLIC_BASE_URL", "") or base_url
    if not origin and settings.EMAIL_BACKEND == "django.core.mail.backends.locmem.EmailBackend":
        origin = "http://localhost:8000"  # In-memory tests never contact this host.
    parsed = urlsplit(origin or "")
    in_memory_test_host = (
        settings.EMAIL_BACKEND == "django.core.mail.backends.locmem.EmailBackend"
        and parsed.hostname == "testserver"
    )
    try:
        URLValidator(schemes=["http", "https"])(origin or "")
    except ValidationError as exc:
        if not in_memory_test_host:
            raise ValueError(
                "Укажите корректный внешний адрес приложения в RM_PUBLIC_BASE_URL."
            ) from exc
    local_http = parsed.scheme == "http" and (
        settings.DEBUG
        or in_memory_test_host
        or parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    )
    if (
        not parsed.netloc
        or (parsed.scheme != "https" and not local_http)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Укажите внешний адрес приложения в RM_PUBLIC_BASE_URL (https://домен).")
    return origin.rstrip("/") + reverse(
        "appointment_confirmation_public", args=[confirmation.token]
    )


@transaction.atomic
def queue_confirmation(confirmation, *, base_url=None):
    """Call within the same transaction that persists the confirmation intent."""
    existing = ConfirmationEmailDelivery.objects.filter(confirmation_id=confirmation.pk).first()
    if existing:
        return existing
    current = _confirmation(confirmation.pk)
    if current is None:
        raise ValueError("Согласование не найдено.")
    url = _public_url(current, base_url)
    sent = current.delivery_status == AppointmentConfirmation.DeliveryStatus.SENT
    delivery, _ = ConfirmationEmailDelivery.objects.get_or_create(
        confirmation=current,
        defaults={
            "email": current.email,
            "subject": current.subject,
            "body": f"{current.message}\n\nСсылка для ответа: {url}",
            "fingerprint": _fingerprint(current),
            "status": ConfirmationEmailDelivery.Status.SENT
            if sent
            else ConfirmationEmailDelivery.Status.PENDING,
            "sent_at": (current.sent_at or timezone.now()) if sent else None,
        },
    )
    return delivery


def _lock(queryset):
    # SQLite remains usable for simple unit tests; concurrency is proved on PG.
    return queryset.select_for_update(
        skip_locked=connection.features.has_select_for_update_skip_locked
    )


def _finish(delivery, *, status, error=""):
    delivery.status = status
    delivery.last_error = error
    delivery.claim_token = None
    delivery.locked_until = None
    if status == ConfirmationEmailDelivery.Status.SENT:
        delivery.sent_at = timezone.now()
    delivery.save(
        update_fields=[
            "status",
            "last_error",
            "claim_token",
            "locked_until",
            "sent_at",
            "next_attempt_at",
            "updated_at",
        ]
    )
    sent = status == ConfirmationEmailDelivery.Status.SENT
    # Do not save a stale model instance: response/decision fields belong to operators.
    projection = {
        "delivery_status": AppointmentConfirmation.DeliveryStatus.SENT
        if sent
        else AppointmentConfirmation.DeliveryStatus.FAILED,
        "delivery_error": "" if sent else _error_label(error),
        "updated_at": timezone.now(),
    }
    if sent:
        projection["sent_at"] = delivery.sent_at
    AppointmentConfirmation.objects.filter(pk=delivery.confirmation_id).update(**projection)


def _error_label(code):
    return {
        "obsolete": "Отправка отменена: согласование больше не актуально.",
        "changed": "Отправка отменена: данные занятия или адресата изменились.",
        "missing_email": "Email получателя не указан или некорректен.",
        "attempts_exhausted": "Попытки отправки исчерпаны. Проверьте почтовый сервис.",
        "smtp_error": "Почтовый сервис не принял письмо. Повторная попытка запланирована.",
        "empty_result": "Почтовый сервис не подтвердил отправку. Повторная попытка запланирована.",
        "invalid_backend": "Для отправки требуется настроенный почтовый сервис.",
        "invalid_timeout": "Настройте ограничение времени отправки EMAIL_TIMEOUT от 1 до 299 секунд.",
    }.get(code, "Не удалось отправить письмо.")


@transaction.atomic
def claim_delivery(*, confirmation_id=None):
    now = timezone.now()
    eligible = Q(status__in=["pending", "retry"], next_attempt_at__lte=now) | Q(
        status="processing",
        locked_until__lte=now,
    )
    candidates = _lock(ConfirmationEmailDelivery.objects.filter(eligible)).order_by(
        "next_attempt_at", "pk"
    )
    if confirmation_id is not None:
        candidates = candidates.filter(confirmation_id=confirmation_id)
    while (delivery := candidates.first()) is not None:
        if delivery.attempts >= MAX_ATTEMPTS:
            _finish(delivery, status="failed", error="attempts_exhausted")
            continue
        delivery.status = ConfirmationEmailDelivery.Status.PROCESSING
        delivery.claim_token = uuid4()
        delivery.locked_until = now + timedelta(seconds=LEASE_SECONDS)
        delivery.attempts += 1
        delivery.save(
            update_fields=["status", "claim_token", "locked_until", "attempts", "updated_at"]
        )
        return delivery
    return None


def _obsolete(confirmation):
    starts_at = (
        confirmation.reschedule_step.proposed_starts_at
        if confirmation.reschedule_step_id
        else confirmation.appointment.starts_at
    )
    if (
        confirmation.status != AppointmentConfirmation.Status.PENDING
        or confirmation.appointment.status in TERMINAL_APPOINTMENT_STATUSES
        or starts_at is None
        or starts_at <= timezone.now()
        or schedule_decisions.current_confirmation_decision(confirmation)
    ):
        return True
    if confirmation.participant_id and (
        confirmation.participant.appointment_status in TERMINAL_APPOINTMENT_STATUSES
        or appointment_svc.participant_has_series_result(confirmation.participant)
    ):
        return True
    if (
        confirmation.staff_assignment_id
        and confirmation.staff_assignment.appointment_status in TERMINAL_APPOINTMENT_STATUSES
    ):
        return True
    if confirmation.reschedule_step_id:
        step = confirmation.reschedule_step
        return step.status != "valid" or step.plan.status in {"applied", "cancelled"}
    return False


def deliver_claim(delivery_id, claim_token):
    """Keep the delivery lock through bounded SMTP I/O, even if its lease expires."""
    sent_confirmation = None
    with transaction.atomic():
        delivery = _lock(
            ConfirmationEmailDelivery.objects.filter(
                pk=delivery_id,
                status="processing",
                claim_token=claim_token,
            )
        ).first()
        if delivery is None:
            return False
        confirmation = _confirmation(delivery.confirmation_id)
        if confirmation is None:
            return False
        if confirmation.delivery_status == AppointmentConfirmation.DeliveryStatus.SENT:
            _finish(delivery, status="sent")
            return True
        if _obsolete(confirmation):
            _finish(delivery, status="cancelled", error="obsolete")
            return False
        if delivery.fingerprint != _fingerprint(confirmation):
            _finish(delivery, status="cancelled", error="changed")
            return False
        try:
            validate_email(delivery.email)
        except ValidationError:
            _finish(delivery, status="failed", error="missing_email")
            return False
        backend = settings.EMAIL_BACKEND
        if backend not in {
            "django.core.mail.backends.smtp.EmailBackend",
            "django.core.mail.backends.locmem.EmailBackend",
        }:
            _finish(delivery, status="failed", error="invalid_backend")
            return False
        if not 1 <= (settings.EMAIL_TIMEOUT or 0) < LEASE_SECONDS:
            _finish(delivery, status="failed", error="invalid_timeout")
            return False
        error = ""
        try:
            mail_connection = get_connection(timeout=settings.EMAIL_TIMEOUT)
            message = EmailMessage(
                delivery.subject,
                delivery.body,
                settings.DEFAULT_FROM_EMAIL,
                [delivery.email],
                connection=mail_connection,
                headers={"Message-ID": f"<{delivery.message_id}@rm-confirmation>"},
            )
            if message.send(fail_silently=False) != 1:
                error = "empty_result"
        except Exception:
            # SMTP exceptions may contain addresses, tokens, server replies or credentials.
            error = "smtp_error"
        if error:
            exhausted = delivery.attempts >= MAX_ATTEMPTS
            delivery.next_attempt_at = timezone.now() + timedelta(
                seconds=min(3600, 60 * 2 ** (delivery.attempts - 1))
            )
            _finish(
                delivery,
                status="failed" if exhausted else "retry",
                error="attempts_exhausted" if exhausted else error,
            )
            logger.warning(
                "Confirmation delivery %s: %s (attempt %s)",
                delivery.pk,
                delivery.last_error,
                delivery.attempts,
            )
            return False
        _finish(delivery, status="sent")
        sent_confirmation = confirmation
    # Legacy direct callers may send drafts. Never hold the delivery/confirmation
    # locks while acquiring the appointment lock used by domain write services.
    if sent_confirmation.appointment.status == Appointment.Status.DRAFT:
        try:
            appointment_svc.transition_appointment_status(
                sent_confirmation.appointment,
                status=Appointment.Status.PROPOSED,
                allowed_from={Appointment.Status.DRAFT},
                action="отправить согласование",
                target_participant_id=sent_confirmation.participant_id,
            )
        except appointment_svc.AppointmentStateConflict:
            logger.info("Confirmation %s sent; appointment state preserved", sent_confirmation.pk)
    return True


def send_confirmation(confirmation_id):
    confirmation = _confirmation(confirmation_id)
    if confirmation is None:
        return False
    if confirmation.delivery_status == AppointmentConfirmation.DeliveryStatus.SENT:
        return True
    try:
        queue_confirmation(confirmation)
    except ValueError:
        AppointmentConfirmation.objects.filter(pk=confirmation_id).update(
            delivery_status=AppointmentConfirmation.DeliveryStatus.FAILED,
            delivery_error="Не настроен внешний адрес приложения для ссылки в письме.",
            updated_at=timezone.now(),
        )
        return False
    delivery = claim_delivery(confirmation_id=confirmation_id)
    return bool(delivery and deliver_claim(delivery.pk, delivery.claim_token))


def process_due(*, limit=100):
    if not 1 <= limit <= 1000:
        raise ValueError("Размер пакета должен быть от 1 до 1000.")
    result = {"processed": 0, "sent": 0, "not_sent": 0}
    for _ in range(limit):
        delivery = claim_delivery()
        if delivery is None:
            break
        sent = deliver_claim(delivery.pk, delivery.claim_token)
        result["processed"] += 1
        result["sent" if sent else "not_sent"] += 1
    return result
