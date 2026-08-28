"""Append-only decisions for appointment confirmations."""

from __future__ import annotations

from contextlib import suppress

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentConfirmation,
    AppointmentConfirmationDecision,
)
from operations.services import appointments as appointment_svc

from .authority import AuthorityRole, authority_role


def _external_source(confirmation: AppointmentConfirmation) -> tuple[str, str]:
    if confirmation.target_type == AppointmentConfirmation.TargetType.SPECIALIST:
        return (
            AppointmentConfirmationDecision.Source.SPECIALIST_RESPONSE,
            AppointmentConfirmationDecision.ActorRole.SPECIALIST,
        )
    if confirmation.target_type == AppointmentConfirmation.TargetType.REPRESENTATIVE:
        return (
            AppointmentConfirmationDecision.Source.REPRESENTATIVE_RESPONSE,
            AppointmentConfirmationDecision.ActorRole.REPRESENTATIVE,
        )
    return (
        AppointmentConfirmationDecision.Source.RECIPIENT_RESPONSE,
        AppointmentConfirmationDecision.ActorRole.RECIPIENT,
    )


def _decision_value(action: str) -> str:
    values = {
        "confirm": AppointmentConfirmationDecision.Decision.CONFIRMED,
        "decline": AppointmentConfirmationDecision.Decision.DECLINED,
    }
    try:
        return values[action]
    except KeyError as exc:
        raise ValueError("Неизвестное решение по согласованию.") from exc


def _sync_confirmation_effects(confirmation: AppointmentConfirmation) -> None:
    if confirmation.reschedule_step_id:
        from operations.services import rescheduling_plans as plan_svc

        plan_svc.refresh_step_confirmation_status(confirmation.reschedule_step)

    if (
        confirmation.status == AppointmentConfirmation.Status.CONFIRMED
        and confirmation.reschedule_step_id is None
        and confirmation.target_type
        in {
            AppointmentConfirmation.TargetType.REPRESENTATIVE,
            AppointmentConfirmation.TargetType.RECIPIENT,
        }
        and confirmation.appointment.status
        in {Appointment.Status.DRAFT, Appointment.Status.PROPOSED}
    ):
        with suppress(appointment_svc.AppointmentStateConflict):
            appointment_svc.transition_appointment_status(
                confirmation.appointment,
                status=Appointment.Status.CONFIRMED,
                allowed_from={Appointment.Status.DRAFT, Appointment.Status.PROPOSED},
                action="подтвердить занятие",
            )


def _record_decision(
    confirmation: AppointmentConfirmation,
    *,
    decision: str,
    source: str,
    actor_role: str,
    note: str,
    actor=None,
) -> AppointmentConfirmationDecision:
    previous = (
        AppointmentConfirmationDecision.objects.select_for_update()
        .filter(confirmation=confirmation, is_current=True)
        .first()
    )
    if previous:
        previous.is_current = False
        previous.save(update_fields=["is_current", "updated_at"])

    record = AppointmentConfirmationDecision.objects.create(
        confirmation=confirmation,
        decision=decision,
        source=source,
        actor=actor,
        actor_role_snapshot=actor_role,
        note=note,
        supersedes=previous,
        is_current=True,
    )
    confirmation.status = decision
    confirmation.response_note = note
    confirmation.responded_at = timezone.now()
    confirmation.save(update_fields=["status", "response_note", "responded_at", "updated_at"])
    _sync_confirmation_effects(confirmation)
    return record


@transaction.atomic
def record_external_response(
    confirmation: AppointmentConfirmation,
    *,
    action: str,
    note: str = "",
) -> AppointmentConfirmationDecision:
    locked = AppointmentConfirmation.objects.select_for_update().get(pk=confirmation.pk)
    if locked.status != AppointmentConfirmation.Status.PENDING:
        raise ValueError("По этому согласованию решение уже принято.")
    source, actor_role = _external_source(locked)
    return _record_decision(
        locked,
        decision=_decision_value(action),
        source=source,
        actor_role=actor_role,
        note=note.strip(),
    )


@transaction.atomic
def resolve_manually(
    confirmation: AppointmentConfirmation,
    *,
    action: str,
    reason: str,
    actor,
) -> AppointmentConfirmationDecision:
    role = authority_role(actor)
    if role not in {AuthorityRole.DIRECTOR, AuthorityRole.ADMINISTRATOR}:
        raise PermissionDenied("Недостаточно прав для ручного решения.")

    reason = reason.strip()
    if len(reason) < 5:
        raise ValueError("Укажите основание ручного решения не короче 5 символов.")

    locked = AppointmentConfirmation.objects.select_for_update().get(pk=confirmation.pk)
    previous = (
        AppointmentConfirmationDecision.objects.select_for_update()
        .filter(confirmation=locked, is_current=True)
        .first()
    )
    if (
        previous
        and previous.source == AppointmentConfirmationDecision.Source.DIRECTOR_MANUAL
        and role != AuthorityRole.DIRECTOR
    ):
        raise PermissionDenied("Решение руководителя может изменить только руководитель.")

    if role == AuthorityRole.DIRECTOR:
        source = AppointmentConfirmationDecision.Source.DIRECTOR_MANUAL
        actor_role = AppointmentConfirmationDecision.ActorRole.DIRECTOR
    else:
        source = AppointmentConfirmationDecision.Source.ADMINISTRATOR_MANUAL
        actor_role = AppointmentConfirmationDecision.ActorRole.ADMINISTRATOR

    return _record_decision(
        locked,
        decision=_decision_value(action),
        source=source,
        actor_role=actor_role,
        note=reason,
        actor=actor,
    )
