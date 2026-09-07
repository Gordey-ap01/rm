"""Manual acceptance of an assigned specialist's current appointment schedule."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q

from operations.models import (
    Appointment,
    AppointmentConfirmation,
    AppointmentConfirmationDecision,
    AppointmentScheduleDecision,
    normalize_immutable_reason,
)
from operations.services.authority import AuthorityRole, authority_role


def current_decision(appointment: Appointment, staff_member_id: int):
    return appointment.schedule_decisions.filter(
        staff_member_id=staff_member_id,
        starts_at_snapshot=appointment.starts_at,
        ends_at_snapshot=appointment.ends_at,
    ).first()


def confirmation_staff_id(confirmation: AppointmentConfirmation):
    if confirmation.reschedule_step_id or confirmation.target_type != "specialist":
        return None
    return (
        confirmation.staff_assignment.staff_member_id
        if confirmation.staff_assignment_id else confirmation.appointment.staff_member_id
    )


def current_confirmation_decision(confirmation: AppointmentConfirmation):
    staff_id = confirmation_staff_id(confirmation)
    if staff_id is None:
        return None
    # Manual decisions apply to one specialist and the current appointment time.
    appointment = confirmation.appointment
    return current_decision(appointment, staff_id)


@transaction.atomic
def resolve_manually(
    appointment: Appointment, *, staff_member, action: str, reason: str, actor,
    operation_key=None, expected_schedule: str | None = None,
) -> AppointmentScheduleDecision:
    role = authority_role(actor)
    if role not in {AuthorityRole.DIRECTOR, AuthorityRole.ADMINISTRATOR}:
        raise PermissionDenied("Недостаточно прав для принятия расписания.")
    try:
        reason = normalize_immutable_reason(reason)
    except ValidationError as exc:
        raise ValueError(" ".join(exc.messages)) from exc
    if not 5 <= len(reason) <= 2000:
        raise ValueError("Укажите основание от 5 до 2000 символов.")
    if action not in {"confirm", "decline"}:
        raise ValueError("Неизвестное решение по расписанию.")
    expected_schedule = expected_schedule or (
        appointment.starts_at.isoformat() + "|" + appointment.ends_at.isoformat()
    )
    appointment = Appointment.objects.select_for_update().get(pk=appointment.pk)
    actual_schedule = appointment.starts_at.isoformat() + "|" + appointment.ends_at.isoformat()
    try:
        expected_start, expected_end = map(datetime.fromisoformat, expected_schedule.split("|"))
    except (ValueError, TypeError) as exc:
        raise ValueError("Откройте карточку заново, чтобы проверить время расписания.") from exc
    if expected_start != appointment.starts_at or expected_end != appointment.ends_at:
        raise ValueError("Расписание изменилось. Обновите карточку и проверьте новое время перед решением.")
    operation_key = operation_key or uuid4()
    fingerprint = hashlib.sha256(json.dumps({
        "appointment": appointment.pk, "staff_member": staff_member.pk,
        "actor": actor.pk, "action": action, "reason": reason,
        "schedule": actual_schedule,
    }, sort_keys=True).encode()).hexdigest()
    reused = AppointmentScheduleDecision.objects.filter(operation_key=operation_key).first()
    if reused:
        if reused.fingerprint != fingerprint:
            raise ValueError("Ключ операции уже использован для другого решения.")
        return reused
    if appointment.status not in {"draft", "proposed", "confirmed", "reserved"}:
        raise ValueError("Расписание завершенного, отмененного или перенесенного занятия не утверждается.")
    assignments = list(appointment.staff_assignments.select_for_update().values_list("staff_member_id", flat=True))
    if staff_member.pk not in (assignments or [appointment.staff_member_id]):
        raise ValueError("Специалист больше не назначен на это занятие.")
    current = current_decision(appointment, staff_member.pk)
    existing_director = AppointmentConfirmationDecision.objects.filter(
        confirmation__appointment=appointment,
        confirmation__target_type="specialist",
        confirmation__reschedule_step__isnull=True,
        is_current=True, source="director_manual",
    ).filter(
        Q(confirmation__staff_assignment__staff_member=staff_member)
        | Q(confirmation__staff_assignment__isnull=True, confirmation__appointment__staff_member=staff_member)
    ).exists()
    if role != AuthorityRole.DIRECTOR and (
        (current and current.actor_role_snapshot == AuthorityRole.DIRECTOR)
        or existing_director
    ):
        raise PermissionDenied("Решение руководителя может изменить только руководитель.")
    previous = appointment.schedule_decisions.filter(staff_member=staff_member).first()
    record = AppointmentScheduleDecision.objects.create(
        appointment=appointment, staff_member=staff_member,
        actor=actor, actor_role_snapshot=role, reason=reason,
        operation_key=operation_key, fingerprint=fingerprint,
        decision="confirmed" if action == "confirm" else "declined",
        decision_number=previous.decision_number + 1 if previous else 1,
        supersedes=previous, starts_at_snapshot=appointment.starts_at,
        ends_at_snapshot=appointment.ends_at,
    )
    # Keep existing request projections consistent with the canonical decision.
    # No request is created and no message is sent for a standalone manual action.
    from .confirmation_decisions import _record_decision

    confirmations = AppointmentConfirmation.objects.select_for_update(of=("self",)).filter(
        appointment=appointment,
        target_type=AppointmentConfirmation.TargetType.SPECIALIST,
        reschedule_step__isnull=True,
    ).filter(
        Q(staff_assignment__staff_member=staff_member)
        | Q(staff_assignment__isnull=True, appointment__staff_member=staff_member)
    ).order_by("pk")
    source = (
        AppointmentConfirmationDecision.Source.DIRECTOR_MANUAL
        if role == AuthorityRole.DIRECTOR
        else AppointmentConfirmationDecision.Source.ADMINISTRATOR_MANUAL
    )
    for confirmation in confirmations:
        confirmation.appointment = appointment
        _record_decision(
            confirmation, decision=record.decision, source=source,
            actor_role=role, note=reason, actor=actor,
        )
    return record
