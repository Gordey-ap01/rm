"""Reviewed, immutable terminal decisions for one program block."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, connections, transaction

from operations.models import (
    Appointment,
    ProgramBlock,
    ProgramBlockLifecycleEvent,
    TreatmentProgram,
    normalize_immutable_reason,
)
from operations.services.authority import AuthorityRole, authority_role
from operations.services.series_revisions import canonical_fingerprint


class ProgramBlockLifecycleMismatch(ValidationError):
    """A command no longer describes the accepted decision or reviewed facts."""


@dataclass(frozen=True)
class ProgramBlockLifecycleResult:
    block: ProgramBlock
    event: ProgramBlockLifecycleEvent
    reused_event: bool


@dataclass(frozen=True)
class ProgramBlockLifecycleReview:
    snapshot: dict

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.snapshot)

    @property
    def planned(self) -> int:
        return self.snapshot["block"]["planned_sessions"]

    @property
    def completed(self) -> int:
        return sum(
            fact["appointment_status"] == Appointment.Status.COMPLETED
            and fact["participant_status"] == Appointment.Status.COMPLETED
            and fact["attendance_status"] == Appointment.AttendanceStatus.ATTENDED
            and not fact["withdrawn"]
            for fact in self.snapshot["facts"]
        )

    @property
    def remaining(self) -> int:
        return max(self.planned - self.completed, 0)

    @property
    def allocated(self) -> int:
        inactive = {Appointment.Status.CANCELLED, Appointment.Status.RESCHEDULED}
        return sum(
            fact["appointment_status"] not in inactive
            and fact["participant_status"] not in inactive
            and not fact["withdrawn"]
            for fact in self.snapshot["facts"]
        )

    @property
    def missed(self) -> int:
        inactive = {Appointment.Status.CANCELLED, Appointment.Status.RESCHEDULED}
        return sum(
            fact["appointment_status"] not in inactive
            and fact["participant_status"] not in inactive
            and not fact["withdrawn"]
            and (
                fact["appointment_status"] == Appointment.Status.NO_SHOW
                or fact["participant_status"] == Appointment.Status.NO_SHOW
                or fact["attendance_status"] == Appointment.AttendanceStatus.MISSED
            )
            for fact in self.snapshot["facts"]
        )

    @property
    def charged(self) -> int:
        return sum(
            fact["billing_decision"] == Appointment.BillingDecision.CHARGE
            and fact["billing_account_id"] is not None
            for fact in self.snapshot["facts"]
        )

    @property
    def can_complete_normally(self) -> bool:
        return self.completed >= self.planned

    @property
    def is_terminal(self) -> bool:
        return self.snapshot["block"]["status"] in {
            ProgramBlock.Status.COMPLETED,
            ProgramBlock.Status.CANCELLED,
        }

    @property
    def activity_status_label(self) -> str:
        stored_status = self.snapshot["block"]["status"]
        if stored_status in {ProgramBlock.Status.COMPLETED, ProgramBlock.Status.CANCELLED}:
            activity_status = stored_status
        elif self.completed:
            activity_status = ProgramBlock.Status.IN_PROGRESS
        elif self.allocated:
            activity_status = ProgramBlock.Status.SCHEDULED
        else:
            activity_status = ProgramBlock.Status.PLANNED
        return dict(ProgramBlock.Status.choices)[activity_status]


_SQLITE_REVIEW_SQL = """
WITH block AS (
    SELECT
        block.id,
        block.program_id,
        program.child_id,
        program.status AS program_status,
        block.number,
        block.title,
        block.status,
        block.planned_sessions,
        block.service_id,
        block.balance_account_id
    FROM operations_programblock AS block
    JOIN operations_treatmentprogram AS program ON program.id = block.program_id
    WHERE block.id = %s
), facts AS (
    SELECT
        'participant' AS kind,
        appointment.id AS appointment_id,
        participant.id AS participant_id,
        appointment.status AS appointment_status,
        participant.appointment_status AS participant_status,
        participant.attendance_status AS attendance_status,
        EXISTS (
            SELECT 1
            FROM operations_appointmentseriescancellationresult AS cancellation
            WHERE cancellation.appointment_participant_id = participant.id
              AND cancellation.outcome = 'cancelled'
        ) AS withdrawn,
        participant.billing_decision AS billing_decision,
        participant.billing_account_id AS billing_account_id
    FROM operations_appointmentparticipant AS participant
    JOIN operations_appointment AS appointment ON appointment.id = participant.appointment_id
    WHERE participant.program_block_id = %s

    UNION ALL

    SELECT
        'legacy' AS kind,
        appointment.id AS appointment_id,
        NULL AS participant_id,
        appointment.status AS appointment_status,
        appointment.status AS participant_status,
        appointment.attendance_status AS attendance_status,
        0 AS withdrawn,
        appointment.billing_decision AS billing_decision,
        appointment.billing_account_id AS billing_account_id
    FROM operations_appointment AS appointment
    WHERE appointment.program_block_id = %s
      AND NOT EXISTS (
          SELECT 1
          FROM operations_appointmentparticipant AS participant
          WHERE participant.appointment_id = appointment.id
      )
)
SELECT
    block.id,
    block.program_id,
    block.child_id,
    block.program_status,
    block.number,
    block.title,
    block.status,
    block.planned_sessions,
    block.service_id,
    block.balance_account_id,
    facts.kind,
    facts.appointment_id,
    facts.participant_id,
    facts.appointment_status,
    facts.participant_status,
    facts.attendance_status,
    facts.withdrawn,
    facts.billing_decision,
    facts.billing_account_id
FROM block
LEFT JOIN facts ON 1 = 1
ORDER BY facts.kind, facts.appointment_id, facts.participant_id
"""


def _sqlite_review_snapshot(block_id: int, *, using: str) -> dict:
    with connections[using].cursor() as cursor:
        cursor.execute(_SQLITE_REVIEW_SQL, [block_id, block_id, block_id])
        rows = cursor.fetchall()
    if not rows:
        raise ProgramBlock.DoesNotExist(f"ProgramBlock matching query does not exist: {block_id}")

    first = rows[0]
    facts = []
    for row in rows:
        if row[10] is None:
            continue
        facts.append(
            {
                "kind": row[10],
                "appointment_id": row[11],
                "participant_id": row[12],
                "appointment_status": row[13],
                "participant_status": row[14],
                "attendance_status": row[15],
                "withdrawn": bool(row[16]),
                "billing_decision": row[17],
                "billing_account_id": row[18],
            }
        )
    return {
        "block": {
            "id": first[0],
            "program_id": first[1],
            "child_id": first[2],
            "program_status": first[3],
            "number": first[4],
            "title": first[5],
            "status": first[6],
            "planned_sessions": first[7],
            "service_id": first[8],
            "balance_account_id": first[9],
        },
        "facts": facts,
    }


def get_program_block_lifecycle_review(block: ProgramBlock) -> ProgramBlockLifecycleReview:
    """Read one canonical point-in-time block snapshot in one SQL statement."""

    if block.pk is None:
        raise ValueError("Нельзя рассмотреть несохраненный каскад.")
    using = block._state.db or "default"
    if connections[using].vendor == "postgresql":
        with connections[using].cursor() as cursor:
            cursor.execute("SELECT operations_program_block_review_snapshot(%s)", [block.pk])
            row = cursor.fetchone()
        snapshot = row[0] if row else None
        if snapshot is None:
            raise ProgramBlock.DoesNotExist(
                f"ProgramBlock matching query does not exist: {block.pk}"
            )
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
    else:
        snapshot = _sqlite_review_snapshot(block.pk, using=using)
    return ProgramBlockLifecycleReview(snapshot=snapshot)


def _replay(
    event: ProgramBlockLifecycleEvent,
    block: ProgramBlock,
    *,
    event_type: str,
    actor,
    reason: str,
    expected_review_fingerprint: str,
) -> ProgramBlockLifecycleResult:
    if (
        event.block_id != block.pk
        or event.event_type != event_type
        or event.actor_id != actor.pk
        or event.reason != reason
        or event.fingerprint != canonical_fingerprint(event.fingerprint_payload())
        or expected_review_fingerprint != canonical_fingerprint(event.context_snapshot)
    ):
        raise ProgramBlockLifecycleMismatch(
            "Ключ операции уже использован для другого решения."
        )
    return ProgramBlockLifecycleResult(block=block, event=event, reused_event=True)


def _constraint_name(error: BaseException) -> str | None:
    current: BaseException | None = error
    while current is not None:
        diagnostic = getattr(current, "diag", None)
        name = getattr(diagnostic, "constraint_name", None)
        if name:
            return name
        current = current.__cause__
    return None


def _context_snapshot_mismatch(error: ValidationError) -> bool:
    try:
        return "context_snapshot" in error.message_dict
    except AttributeError:
        return False


@transaction.atomic
def _decide(
    block: ProgramBlock,
    *,
    event_type: str,
    actor,
    reason: str,
    operation_key: UUID,
    expected_review_fingerprint: str,
    expected_event_id: int | None,
) -> ProgramBlockLifecycleResult:
    role = authority_role(actor)
    if not actor or not actor.is_active or role not in {
        AuthorityRole.ADMINISTRATOR,
        AuthorityRole.DIRECTOR,
    }:
        raise PermissionDenied(
            "Управление каскадом доступно администратору и руководителю."
        )
    reason = normalize_immutable_reason(reason)

    using = block._state.db or "default"
    locked = (
        ProgramBlock.objects.using(using)
        .select_for_update(of=("self",))
        .get(pk=block.pk)
    )
    # Keep the global lock order stable: block first, then its program root.
    TreatmentProgram.objects.using(using).select_for_update(of=("self",)).get(
        pk=locked.program_id
    )

    existing = (
        ProgramBlockLifecycleEvent.objects.using(using)
        .filter(operation_key=operation_key)
        .first()
    )
    if existing is not None:
        return _replay(
            existing,
            locked,
            event_type=event_type,
            actor=actor,
            reason=reason,
            expected_review_fingerprint=expected_review_fingerprint,
        )

    previous = locked.lifecycle_events.first()
    if expected_event_id is not None and expected_event_id != (previous.pk if previous else 0):
        raise ProgramBlockLifecycleMismatch(
            "Состояние каскада уже изменилось. Откройте его карточку заново."
        )
    if previous is not None:
        raise ProgramBlockLifecycleMismatch("По каскаду уже принято терминальное решение.")
    if locked.status not in {
        ProgramBlock.Status.PLANNED,
        ProgramBlock.Status.SCHEDULED,
        ProgramBlock.Status.IN_PROGRESS,
    }:
        raise ProgramBlockLifecycleMismatch(
            "Команда недоступна для текущего состояния каскада."
        )

    review = get_program_block_lifecycle_review(locked)
    if expected_review_fingerprint != review.fingerprint:
        raise ProgramBlockLifecycleMismatch(
            "Факты каскада изменились. Проверьте решение заново."
        )
    if (
        event_type == ProgramBlockLifecycleEvent.EventType.COMPLETED
        and not review.can_complete_normally
        and role != AuthorityRole.DIRECTOR
    ):
        raise PermissionDenied("Завершить каскад до выполнения плана может только руководитель.")

    status_to = {
        ProgramBlockLifecycleEvent.EventType.COMPLETED: ProgramBlock.Status.COMPLETED,
        ProgramBlockLifecycleEvent.EventType.CANCELLED: ProgramBlock.Status.CANCELLED,
    }[event_type]
    event = ProgramBlockLifecycleEvent(
        block=locked,
        operation_key=operation_key,
        event_type=event_type,
        status_from=locked.status,
        status_to=status_to,
        actor=actor,
        actor_role_snapshot=role,
        reason=reason,
        context_snapshot=review.snapshot,
    )
    event.fingerprint = canonical_fingerprint(event.fingerprint_payload())
    try:
        with transaction.atomic(using=using):
            event.save(using=using)
    except ValidationError as error:
        existing = (
            ProgramBlockLifecycleEvent.objects.using(using)
            .filter(operation_key=operation_key)
            .first()
        )
        if existing is not None:
            return _replay(
                existing,
                locked,
                event_type=event_type,
                actor=actor,
                reason=reason,
                expected_review_fingerprint=expected_review_fingerprint,
            )
        if _context_snapshot_mismatch(error):
            raise ProgramBlockLifecycleMismatch(
                "Факты каскада изменились. Проверьте решение заново."
            ) from error
        raise
    except IntegrityError as error:
        if _constraint_name(error) == "program_block_review_stale":
            raise ProgramBlockLifecycleMismatch(
                "Факты каскада изменились. Проверьте решение заново."
            ) from error
        existing = (
            ProgramBlockLifecycleEvent.objects.using(using)
            .filter(operation_key=operation_key)
            .first()
        )
        if existing is None:
            raise
        return _replay(
            existing,
            locked,
            event_type=event_type,
            actor=actor,
            reason=reason,
            expected_review_fingerprint=expected_review_fingerprint,
        )

    locked.status = status_to
    locked.save(using=using, update_fields=["status", "updated_at"])
    return ProgramBlockLifecycleResult(block=locked, event=event, reused_event=False)


def complete_block(
    block: ProgramBlock,
    *,
    actor,
    reason: str,
    operation_key: UUID,
    expected_review_fingerprint: str,
    expected_event_id: int | None = None,
) -> ProgramBlockLifecycleResult:
    return _decide(
        block,
        event_type=ProgramBlockLifecycleEvent.EventType.COMPLETED,
        actor=actor,
        reason=reason,
        operation_key=operation_key,
        expected_review_fingerprint=expected_review_fingerprint,
        expected_event_id=expected_event_id,
    )


def cancel_block(
    block: ProgramBlock,
    *,
    actor,
    reason: str,
    operation_key: UUID,
    expected_review_fingerprint: str,
    expected_event_id: int | None = None,
) -> ProgramBlockLifecycleResult:
    return _decide(
        block,
        event_type=ProgramBlockLifecycleEvent.EventType.CANCELLED,
        actor=actor,
        reason=reason,
        operation_key=operation_key,
        expected_review_fingerprint=expected_review_fingerprint,
        expected_event_id=expected_event_id,
    )
