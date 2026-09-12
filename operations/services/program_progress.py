"""Read-only, participant-first progress for program blocks.

This module deliberately does not project a block status or acquire lifecycle
locks. Lifecycle commands use their own single-statement canonical review
snapshot; this helper is for read-only overview totals.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from django.db.models import Count, Exists, OuterRef, Q

from operations.models import (
    Appointment,
    AppointmentParticipant,
    AppointmentSeriesCancellationResult,
    ProgramBlock,
)

_INACTIVE_APPOINTMENT_STATUSES = {
    Appointment.Status.CANCELLED,
    Appointment.Status.RESCHEDULED,
}
_TERMINAL_BLOCK_STATUSES = {
    ProgramBlock.Status.COMPLETED,
    ProgramBlock.Status.CANCELLED,
}
_ACTIVITY_STATUS_LABELS = dict(ProgramBlock.Status.choices)


@dataclass(frozen=True)
class ProgramBlockProgress:
    """The factual progress and allocation totals for one program block."""

    block_id: int
    planned: int
    completed: int
    remaining: int
    allocated: int
    missed: int
    charged: int
    activity_status: str

    @property
    def activity_status_label(self) -> str:
        """Human label for the read-only overview state."""

        return _ACTIVITY_STATUS_LABELS.get(self.activity_status, self.activity_status)


def _block_ids(blocks_or_ids: Iterable[ProgramBlock | int]) -> list[int]:
    ids: set[int] = set()
    for value in blocks_or_ids:
        block_id = value.pk if isinstance(value, ProgramBlock) else value
        if block_id is None:
            raise ValueError("Нельзя рассчитать прогресс несохраненного каскада.")
        if not isinstance(block_id, int):
            raise TypeError("Каскад должен быть ProgramBlock или его целочисленным ID.")
        ids.add(block_id)
    return sorted(ids)


def get_program_block_progress(
    blocks_or_ids: Iterable[ProgramBlock | int],
) -> dict[int, ProgramBlockProgress]:
    """Return factual progress for several blocks with a bounded query set.

    Participant rows are authoritative.  Legacy ``Appointment`` fields are
    considered only for an appointment which has *no* participant rows at all;
    this prevents a primary legacy projection from double-counting a group.
    ``allocated`` intentionally reflects capacity already consumed by an
    appointment: completed and no-show rows remain allocated, while cancelled,
    rescheduled and withdrawn rows do not. ``charged`` is a separate financial
    fact, deliberately retaining charged rows regardless of their operational
    status. ``activity_status`` is a transient overview state: terminal stored
    statuses win, otherwise actual participation and allocation describe the
    visible activity without changing ``ProgramBlock.status``.
    """

    block_ids = _block_ids(blocks_or_ids)
    if not block_ids:
        return {}

    block_metadata = {
        block_id: (planned, status)
        for block_id, planned, status in ProgramBlock.objects.filter(pk__in=block_ids).values_list(
            "pk", "planned_sessions", "status"
        )
    }
    if not block_metadata:
        return {}

    withdrawn_result = AppointmentSeriesCancellationResult.objects.filter(
        appointment_participant_id=OuterRef("pk"),
        outcome=AppointmentSeriesCancellationResult.Outcome.CANCELLED,
    )
    active_participant_status = ~Q(appointment_status__in=_INACTIVE_APPOINTMENT_STATUSES)
    active_appointment_status = ~Q(appointment__status__in=_INACTIVE_APPOINTMENT_STATUSES)
    participant_rows = (
        AppointmentParticipant.objects.filter(program_block_id__in=block_metadata)
        .annotate(is_withdrawn=Exists(withdrawn_result))
        .values("program_block_id")
        .annotate(
            completed=Count(
                "pk",
                filter=(
                    Q(appointment__status=Appointment.Status.COMPLETED)
                    & Q(appointment_status=Appointment.Status.COMPLETED)
                    & Q(attendance_status=Appointment.AttendanceStatus.ATTENDED)
                    & Q(is_withdrawn=False)
                ),
            ),
            allocated=Count(
                "pk",
                filter=(
                    active_participant_status
                    & active_appointment_status
                    & Q(is_withdrawn=False)
                ),
            ),
            missed=Count(
                "pk",
                filter=(
                    active_participant_status
                    & active_appointment_status
                    & Q(is_withdrawn=False)
                    & (
                        Q(appointment__status=Appointment.Status.NO_SHOW)
                        | Q(appointment_status=Appointment.Status.NO_SHOW)
                        | Q(attendance_status=Appointment.AttendanceStatus.MISSED)
                    )
                ),
            ),
            charged=Count(
                "pk",
                filter=(
                    Q(billing_decision=Appointment.BillingDecision.CHARGE)
                    & Q(billing_account__isnull=False)
                ),
            ),
        )
    )

    totals = {
        row["program_block_id"]: {
            "completed": row["completed"],
            "allocated": row["allocated"],
            "missed": row["missed"],
            "charged": row["charged"],
        }
        for row in participant_rows
    }

    has_any_participant = AppointmentParticipant.objects.filter(
        appointment_id=OuterRef("pk")
    )
    legacy_rows = (
        Appointment.objects.filter(program_block_id__in=block_metadata)
        .annotate(has_any_participant=Exists(has_any_participant))
        .filter(has_any_participant=False)
        .values("program_block_id")
        .annotate(
            completed=Count(
                "pk",
                filter=(
                    Q(status=Appointment.Status.COMPLETED)
                    & Q(attendance_status=Appointment.AttendanceStatus.ATTENDED)
                ),
            ),
            allocated=Count("pk", filter=~Q(status__in=_INACTIVE_APPOINTMENT_STATUSES)),
            missed=Count(
                "pk",
                filter=(
                    ~Q(status__in=_INACTIVE_APPOINTMENT_STATUSES)
                    & (Q(status=Appointment.Status.NO_SHOW)
                       | Q(attendance_status=Appointment.AttendanceStatus.MISSED))
                ),
            ),
            charged=Count(
                "pk",
                filter=(
                    Q(billing_decision=Appointment.BillingDecision.CHARGE)
                    & Q(billing_account__isnull=False)
                ),
            ),
        )
    )

    for row in legacy_rows:
        totals.setdefault(
            row["program_block_id"],
            {"completed": 0, "allocated": 0, "missed": 0, "charged": 0},
        )
        for field in ("completed", "allocated", "missed", "charged"):
            totals[row["program_block_id"]][field] += row[field]

    progress: dict[int, ProgramBlockProgress] = {}
    for block_id, (planned, stored_status) in block_metadata.items():
        values = totals.get(block_id, {})
        completed = values.get("completed", 0)
        allocated = values.get("allocated", 0)
        activity_status = (
            stored_status
            if stored_status in _TERMINAL_BLOCK_STATUSES
            else (
                ProgramBlock.Status.IN_PROGRESS
                if completed
                else ProgramBlock.Status.SCHEDULED
                if allocated
                else ProgramBlock.Status.PLANNED
            )
        )
        progress[block_id] = ProgramBlockProgress(
            block_id=block_id,
            planned=planned,
            completed=completed,
            remaining=max(planned - completed, 0),
            allocated=allocated,
            missed=values.get("missed", 0),
            charged=values.get("charged", 0),
            activity_status=activity_status,
        )
    return progress
