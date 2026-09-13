"""Bounded read models for program, block, and stopped-series attention."""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Count, Exists, F, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce

from operations.models import (
    Appointment,
    AppointmentParticipant,
    AppointmentSeries,
    AppointmentSeriesCancellationResult,
    AppointmentSeriesLifecycleEvent,
    ProgramBlock,
    TreatmentProgram,
)

BLOCK_READY = "block_ready"
PARENT_CLOSED = "parent_closed"
PROGRAM_READY = "program_ready"
PROGRAM_PAUSED = "program_paused"

_OPEN_BLOCK_STATUSES = (
    ProgramBlock.Status.PLANNED,
    ProgramBlock.Status.SCHEDULED,
    ProgramBlock.Status.IN_PROGRESS,
)
_CLOSED_PROGRAM_STATUSES = (
    TreatmentProgram.Status.COMPLETED,
    TreatmentProgram.Status.CANCELLED,
)


@dataclass(frozen=True)
class LifecycleOverviewCounts:
    block_ready: int
    parent_closed: int
    program_ready: int
    program_paused: int
    stopped_series: int
    total: int


def _completed_count_expression(*, using: str):
    withdrawn = AppointmentSeriesCancellationResult.objects.using(using).filter(
        appointment_participant_id=OuterRef("pk"),
        outcome=AppointmentSeriesCancellationResult.Outcome.CANCELLED,
    )
    participant_completed = (
        AppointmentParticipant.objects.using(using)
        .filter(
            program_block_id=OuterRef("pk"),
            appointment__status=Appointment.Status.COMPLETED,
            appointment_status=Appointment.Status.COMPLETED,
            attendance_status=Appointment.AttendanceStatus.ATTENDED,
        )
        .annotate(is_withdrawn=Exists(withdrawn))
        .filter(is_withdrawn=False)
        .order_by()
        .values("program_block_id")
        .annotate(total=Count("pk"))
        .values("total")[:1]
    )

    has_participants = AppointmentParticipant.objects.using(using).filter(
        appointment_id=OuterRef("pk")
    )
    legacy_completed = (
        Appointment.objects.using(using)
        .filter(
            program_block_id=OuterRef("pk"),
            status=Appointment.Status.COMPLETED,
            attendance_status=Appointment.AttendanceStatus.ATTENDED,
        )
        .annotate(has_participants=Exists(has_participants))
        .filter(has_participants=False)
        .order_by()
        .values("program_block_id")
        .annotate(total=Count("pk"))
        .values("total")[:1]
    )
    zero = Value(0, output_field=IntegerField())
    return Coalesce(
        Subquery(participant_completed, output_field=IntegerField()), zero
    ) + Coalesce(Subquery(legacy_completed, output_field=IntegerField()), zero)


def block_attention_queryset(focus: str = "", *, using: str = "default"):
    """Return open blocks which met plan or belong to a closed parent program."""

    if focus not in {"", BLOCK_READY, PARENT_CLOSED}:
        raise ValueError(f"Неизвестный фокус каскадов: {focus}")

    queryset = (
        ProgramBlock.objects.using(using)
        .filter(status__in=_OPEN_BLOCK_STATUSES)
        .annotate(completed_count=_completed_count_expression(using=using))
        .select_related("program", "program__child", "service")
    )
    ready = Q(completed_count__gte=F("planned_sessions"))
    parent_closed = Q(program__status__in=_CLOSED_PROGRAM_STATUSES)
    if focus == BLOCK_READY:
        queryset = queryset.filter(ready)
    elif focus == PARENT_CLOSED:
        queryset = queryset.filter(parent_closed)
    else:
        queryset = queryset.filter(ready | parent_closed)
    return queryset.order_by(
        "program__child__last_name",
        "program__child__first_name",
        "program__title",
        "number",
        "pk",
    )


def program_attention_queryset(focus: str = "", *, using: str = "default"):
    """Return completable or paused programs without deciding who may act."""

    if focus not in {"", PROGRAM_READY, PROGRAM_PAUSED}:
        raise ValueError(f"Неизвестный фокус программ: {focus}")

    unfinished_blocks = ProgramBlock.objects.using(using).filter(
        program_id=OuterRef("pk"), status__in=_OPEN_BLOCK_STATUSES
    )
    queryset = (
        TreatmentProgram.objects.using(using)
        .filter(status__in=(TreatmentProgram.Status.ACTIVE, TreatmentProgram.Status.PAUSED))
        .annotate(has_unfinished_blocks=Exists(unfinished_blocks))
        .select_related("child")
    )
    ready = Q(has_unfinished_blocks=False)
    paused = Q(status=TreatmentProgram.Status.PAUSED)
    if focus == PROGRAM_READY:
        queryset = queryset.filter(ready)
    elif focus == PROGRAM_PAUSED:
        queryset = queryset.filter(paused)
    else:
        queryset = queryset.filter(ready | paused)
    return queryset.order_by("child__last_name", "child__first_name", "title", "pk")


def _stopped_series_queryset(*, using: str):
    latest_event_type = (
        AppointmentSeriesLifecycleEvent.objects.using(using)
        .filter(series_id=OuterRef("pk"))
        .order_by("-event_number", "-pk")
        .values("event_type")[:1]
    )
    return (
        AppointmentSeries.objects.using(using)
        .annotate(latest_event_type=Subquery(latest_event_type))
        .filter(
            status=AppointmentSeries.Status.CANCELLED,
            latest_event_type=(
                AppointmentSeriesLifecycleEvent.EventType.STOP_MATERIALIZATION
            ),
        )
    )


def get_lifecycle_overview_counts(*, using: str = "default") -> LifecycleOverviewCounts:
    """Return all overview counters in three fixed queries.

    ``total`` counts unique attention blocks plus unique attention programs.
    A row matching both focuses is counted once. Stopped series remain a
    separate informational signal and are deliberately excluded from total.
    """

    blocks = block_attention_queryset(using=using).aggregate(
        block_ready=Count(
            "pk", filter=Q(completed_count__gte=F("planned_sessions"))
        ),
        parent_closed=Count(
            "pk", filter=Q(program__status__in=_CLOSED_PROGRAM_STATUSES)
        ),
        total=Count("pk"),
    )
    programs = program_attention_queryset(using=using).aggregate(
        program_ready=Count("pk", filter=Q(has_unfinished_blocks=False)),
        program_paused=Count(
            "pk", filter=Q(status=TreatmentProgram.Status.PAUSED)
        ),
        total=Count("pk"),
    )
    stopped_series = _stopped_series_queryset(using=using).count()
    return LifecycleOverviewCounts(
        block_ready=blocks["block_ready"],
        parent_closed=blocks["parent_closed"],
        program_ready=programs["program_ready"],
        program_paused=programs["program_paused"],
        stopped_series=stopped_series,
        total=blocks["total"] + programs["total"],
    )
