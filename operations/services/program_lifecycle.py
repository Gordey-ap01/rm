"""Audited pause/resume of a program without changing its scheduling facts."""

from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from operations.models import (
    TreatmentProgram,
    TreatmentProgramLifecycleEvent,
    normalize_immutable_reason,
)
from operations.services.authority import AuthorityRole, authority_role
from operations.services.series_revisions import canonical_fingerprint


class ProgramLifecycleMismatch(ValidationError):
    """A submitted command no longer describes the accepted decision or state."""


@dataclass(frozen=True)
class ProgramLifecycleResult:
    program: TreatmentProgram
    event: TreatmentProgramLifecycleEvent
    reused_event: bool


def _replay(event, program, *, event_type, actor, reason):
    if (
        event.program_id != program.pk or event.event_type != event_type
        or event.actor_id != actor.pk or event.reason != reason
        or event.fingerprint != canonical_fingerprint(event.fingerprint_payload())
    ):
        raise ProgramLifecycleMismatch("Ключ операции уже использован для другого решения.")
    return ProgramLifecycleResult(program, event, True)


@transaction.atomic
def _decide(program, *, event_type, actor, reason, operation_key, expected_event_id):
    role = authority_role(actor)
    if not actor or not actor.is_active or role not in {
        AuthorityRole.ADMINISTRATOR, AuthorityRole.DIRECTOR,
    }:
        raise PermissionDenied("Управление программой доступно администратору и руководителю.")
    if event_type == TreatmentProgramLifecycleEvent.EventType.RESUMED and role != AuthorityRole.DIRECTOR:
        raise PermissionDenied("Возобновить программу может только руководитель.")
    reason = normalize_immutable_reason(reason)
    if len(reason) < 5:
        raise ValidationError({"reason": "Укажите основание решения: не менее 5 символов."})

    # Never acquire a block or schedule lock after this root lock.
    locked = TreatmentProgram.objects.select_for_update(of=("self",)).get(pk=program.pk)
    existing = TreatmentProgramLifecycleEvent.objects.filter(operation_key=operation_key).first()
    if existing:
        return _replay(existing, locked, event_type=event_type, actor=actor, reason=reason)
    previous = locked.lifecycle_events.order_by("-event_number", "-pk").first()
    if expected_event_id is not None and expected_event_id != (previous.pk if previous else 0):
        raise ProgramLifecycleMismatch("Состояние программы уже изменилось. Откройте ее карточку заново.")
    if previous and previous.actor_role_snapshot == AuthorityRole.DIRECTOR and role != AuthorityRole.DIRECTOR:
        raise PermissionDenied("Последнее решение руководителя может изменить только руководитель.")

    pausing = event_type == TreatmentProgramLifecycleEvent.EventType.PAUSED
    status_from = TreatmentProgram.Status.ACTIVE if pausing else TreatmentProgram.Status.PAUSED
    status_to = TreatmentProgram.Status.PAUSED if pausing else TreatmentProgram.Status.ACTIVE
    if locked.status != status_from:
        raise ProgramLifecycleMismatch("Команда недоступна для текущего состояния программы.")
    event = TreatmentProgramLifecycleEvent(
        program=locked, event_type=event_type, event_number=previous.event_number + 1 if previous else 1,
        status_from=status_from, status_to=status_to, actor=actor, actor_role_snapshot=role,
        reason=reason, operation_key=operation_key, supersedes=previous,
    )
    event.fingerprint = canonical_fingerprint(event.fingerprint_payload())
    try:
        with transaction.atomic():
            event.save()
    except (IntegrityError, ValidationError):
        existing = TreatmentProgramLifecycleEvent.objects.filter(operation_key=operation_key).first()
        if existing is None:
            raise
        return _replay(existing, locked, event_type=event_type, actor=actor, reason=reason)
    locked.status = status_to
    locked.save(update_fields=["status", "updated_at"])
    return ProgramLifecycleResult(locked, event, False)


def pause_program(
    program: TreatmentProgram, *, actor, reason: str, operation_key: UUID,
    expected_event_id: int | None = None,
) -> ProgramLifecycleResult:
    return _decide(
        program, event_type=TreatmentProgramLifecycleEvent.EventType.PAUSED,
        actor=actor, reason=reason, operation_key=operation_key, expected_event_id=expected_event_id,
    )


def resume_program(
    program: TreatmentProgram, *, actor, reason: str, operation_key: UUID,
    expected_event_id: int | None = None,
) -> ProgramLifecycleResult:
    return _decide(
        program, event_type=TreatmentProgramLifecycleEvent.EventType.RESUMED,
        actor=actor, reason=reason, operation_key=operation_key, expected_event_id=expected_event_id,
    )
