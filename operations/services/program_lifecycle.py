"""Audited program decisions without changing scheduling or financial facts."""

from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from operations.models import (
    ProgramBlock,
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


@dataclass(frozen=True)
class ProgramLifecycleReview:
    snapshot: dict
    activation_error: str

    @property
    def fingerprint(self):
        return canonical_fingerprint(self.snapshot)

    @property
    def blocks(self):
        return self.snapshot["blocks"]

    @property
    def unfinished_blocks(self):
        return [block for block in self.blocks if block["status"] not in {"completed", "cancelled"}]

    @property
    def can_activate(self):
        return not self.activation_error


def get_program_lifecycle_review(program: TreatmentProgram) -> ProgramLifecycleReview:
    using = program._state.db or "default"
    current = TreatmentProgram.objects.using(using).get(pk=program.pk)
    blocks = list(ProgramBlock.objects.using(using).filter(program_id=current.pk)
                  .select_related("balance_account").order_by("pk"))
    valid = any(
        block.status in {ProgramBlock.Status.PLANNED, ProgramBlock.Status.SCHEDULED, ProgramBlock.Status.IN_PROGRESS}
        and block.planned_sessions > 0 and block.balance_account_id
        and block.balance_account.child_id == current.child_id
        and (block.balance_account.service_scope == "any"
             or block.balance_account.service_id == block.service_id)
        for block in blocks
    )
    activation_error = "" if valid else "Для активации нужен хотя бы один каскад с положительным планом и подходящим счетом получателя."
    if current.starts_on and current.ends_on and current.ends_on < current.starts_on:
        activation_error = "Дата окончания программы не может быть раньше даты начала."
    return ProgramLifecycleReview(
        snapshot={
            "program_id": current.pk, "child_id": current.child_id, "status": current.status,
            "starts_on": current.starts_on.isoformat() if current.starts_on else None,
            "ends_on": current.ends_on.isoformat() if current.ends_on else None,
            "blocks": [
                {"id": block.pk, "number": block.number, "title": block.title,
                 "status": block.status, "planned_sessions": block.planned_sessions,
                 "service_id": block.service_id, "balance_account_id": block.balance_account_id}
                for block in blocks
            ],
        },
        activation_error=activation_error,
    )


def _replay(event, program, *, event_type, actor, reason, expected_review_fingerprint=None):
    if (
        event.program_id != program.pk or event.event_type != event_type
        or event.actor_id != actor.pk or event.reason != reason
        or event.fingerprint != canonical_fingerprint(event.fingerprint_payload())
        or (event.context_snapshot and expected_review_fingerprint != canonical_fingerprint(event.context_snapshot))
    ):
        raise ProgramLifecycleMismatch("Ключ операции уже использован для другого решения.")
    return ProgramLifecycleResult(program, event, True)


@transaction.atomic
def _decide(program, *, event_type, actor, reason, operation_key, expected_event_id,
            expected_review_fingerprint=None):
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
        return _replay(existing, locked, event_type=event_type, actor=actor, reason=reason,
                       expected_review_fingerprint=expected_review_fingerprint)
    previous = locked.lifecycle_events.order_by("-event_number", "-pk").first()
    if expected_event_id is not None and expected_event_id != (previous.pk if previous else 0):
        raise ProgramLifecycleMismatch("Состояние программы уже изменилось. Откройте ее карточку заново.")
    if previous and previous.actor_role_snapshot == AuthorityRole.DIRECTOR and role != AuthorityRole.DIRECTOR:
        raise PermissionDenied("Последнее решение руководителя может изменить только руководитель.")

    transitions = {
        "activated": ({"draft"}, "active"),
        "paused": ({"active"}, "paused"),
        "resumed": ({"paused"}, "active"),
        "completed": ({"active", "paused"}, "completed"),
        "cancelled": ({"draft", "active", "paused"}, "cancelled"),
    }
    allowed_from, status_to = transitions[event_type]
    status_from = locked.status
    if status_from not in allowed_from:
        raise ProgramLifecycleMismatch("Команда недоступна для текущего состояния программы.")
    context_snapshot = {}
    if event_type in {"activated", "completed", "cancelled"}:
        review = get_program_lifecycle_review(locked)
        if expected_review_fingerprint != review.fingerprint:
            raise ProgramLifecycleMismatch("Состав программы изменился. Проверьте решение заново.")
        if event_type == "activated" and not review.can_activate:
            raise ValidationError(review.activation_error)
        if event_type == "completed" and review.unfinished_blocks and role != AuthorityRole.DIRECTOR:
            raise PermissionDenied("Досрочно завершить программу может только руководитель.")
        context_snapshot = review.snapshot
    event = TreatmentProgramLifecycleEvent(
        program=locked, event_type=event_type, event_number=previous.event_number + 1 if previous else 1,
        status_from=status_from, status_to=status_to, actor=actor, actor_role_snapshot=role,
        reason=reason, operation_key=operation_key, supersedes=previous,
        context_snapshot=context_snapshot,
    )
    event.fingerprint = canonical_fingerprint(event.fingerprint_payload())
    try:
        with transaction.atomic():
            event.save()
    except (IntegrityError, ValidationError):
        existing = TreatmentProgramLifecycleEvent.objects.filter(operation_key=operation_key).first()
        if existing is None:
            raise
        return _replay(existing, locked, event_type=event_type, actor=actor, reason=reason,
                       expected_review_fingerprint=expected_review_fingerprint)
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


def activate_program(program, *, actor, reason, operation_key, expected_review_fingerprint,
                     expected_event_id=None) -> ProgramLifecycleResult:
    return _decide(program, event_type="activated", actor=actor, reason=reason,
                   operation_key=operation_key, expected_event_id=expected_event_id,
                   expected_review_fingerprint=expected_review_fingerprint)


def complete_program(program, *, actor, reason, operation_key, expected_review_fingerprint,
                     expected_event_id=None) -> ProgramLifecycleResult:
    return _decide(program, event_type="completed", actor=actor, reason=reason,
                   operation_key=operation_key, expected_event_id=expected_event_id,
                   expected_review_fingerprint=expected_review_fingerprint)


def cancel_program(program, *, actor, reason, operation_key, expected_review_fingerprint,
                   expected_event_id=None) -> ProgramLifecycleResult:
    return _decide(program, event_type="cancelled", actor=actor, reason=reason,
                   operation_key=operation_key, expected_event_id=expected_event_id,
                   expected_review_fingerprint=expected_review_fingerprint)
