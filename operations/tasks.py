"""Compatibility entry point for already queued django-tasks jobs."""

from django_tasks import task


@task
def send_appointment_confirmation_email(confirmation_id: int) -> bool:
    """Use the same durable claim and guards as the outbox worker."""
    from operations.services.confirmation_email_outbox import send_confirmation

    return send_confirmation(confirmation_id)
