"""Authority-aware lifecycle transitions for appointment series."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentParticipant,
    AppointmentSeries,
    AppointmentSeriesCancellationResult,
    AppointmentSeriesLifecycleEvent,
    AppointmentSeriesMaterializationResult,
    AppointmentSeriesOccurrence,
    AppointmentSeriesRevisionParticipant,
    AppointmentSeriesRevisionStaffAssignment,
    AppointmentStaffAssignment,
    BalanceAccount,
    LedgerEntry,
    ProgramBlock,
    normalize_immutable_reason,
)
from operations.services import appointments as appointment_svc
from operations.services.authority import AuthorityRole, authority_role
from operations.services.series_revisions import (
    canonical_fingerprint,
    interrupt_run,
    require_operator_role,
)


class SeriesLifecycleMismatch(ValidationError):
    """An idempotency key or immutable lifecycle chain does not match the request."""


@dataclass(frozen=True)
class SeriesLifecycleResult:
    series: AppointmentSeries
    event: AppointmentSeriesLifecycleEvent
    reused_event: bool


@dataclass(frozen=True)
class SeriesCancellationResult:
    series: AppointmentSeries
    event: AppointmentSeriesLifecycleEvent
    results: tuple[AppointmentSeriesCancellationResult, ...]
    reused_event: bool

    @property
    def cancelled_count(self) -> int:
        return sum(
            result.outcome == AppointmentSeriesCancellationResult.Outcome.CANCELLED
            for result in self.results
        )

    @property
    def withdrawn_count(self) -> int:
        return self.cancelled_count

    @property
    def unchanged_count(self) -> int:
        return sum(
            result.outcome == AppointmentSeriesCancellationResult.Outcome.UNCHANGED
            for result in self.results
        )

    @property
    def manual_review_count(self) -> int:
        return sum(
            result.outcome == AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW
            for result in self.results
        )


_CANCELLABLE_APPOINTMENT_STATUSES = {
    Appointment.Status.PROPOSED,
    Appointment.Status.CONFIRMED,
    Appointment.Status.RESERVED,
}


def _normalized_reason(reason: str) -> str:
    normalized = normalize_immutable_reason(reason)
    if len(normalized) < 5:
        raise ValidationError("Укажите основание действия не короче 5 символов.")
    return normalized


def _event_payload(
    *,
    series_id: int,
    event_type: str,
    event_number: int,
    status_from: str,
    status_to: str,
    actor_id: int,
    actor_role_snapshot: str,
    reason: str,
    supersedes_id: int | None,
) -> dict[str, Any]:
    return {
        "series_id": series_id,
        "event_type": event_type,
        "event_number": event_number,
        "status_from": status_from,
        "status_to": status_to,
        "actor_id": actor_id,
        "actor_role_snapshot": actor_role_snapshot,
        "reason": reason,
        "supersedes_id": supersedes_id,
    }


def _validate_existing_event(
    event: AppointmentSeriesLifecycleEvent,
    series: AppointmentSeries,
    *,
    event_type: str,
    actor: Any,
    reason: str,
) -> AppointmentSeriesLifecycleEvent:
    payload = _event_payload(
        series_id=event.series_id,
        event_type=event.event_type,
        event_number=event.event_number,
        status_from=event.status_from,
        status_to=event.status_to,
        actor_id=event.actor_id,
        actor_role_snapshot=event.actor_role_snapshot,
        reason=event.reason,
        supersedes_id=event.supersedes_id,
    )
    if (
        event.series_id != series.pk
        or event.event_type != event_type
        or event.actor_id != actor.pk
        or event.reason != reason
        or event.fingerprint != canonical_fingerprint(payload)
    ):
        raise SeriesLifecycleMismatch(
            "Ключ lifecycle-операции уже использован для другого действия."
        )
    return event


def _existing_event(
    operation_key: UUID,
) -> AppointmentSeriesLifecycleEvent | None:
    return (
        AppointmentSeriesLifecycleEvent.objects.select_related(
            "series",
            "actor",
            "supersedes",
        )
        .filter(operation_key=operation_key)
        .first()
    )


def _latest_series_event(
    series: AppointmentSeries,
) -> AppointmentSeriesLifecycleEvent | None:
    return (
        AppointmentSeriesLifecycleEvent.objects.select_for_update()
        .filter(series=series)
        .order_by("-event_number", "-pk")
        .first()
    )


def _assert_decision_priority(
    role: str,
    previous: AppointmentSeriesLifecycleEvent | None,
) -> None:
    if (
        role == AppointmentSeriesLifecycleEvent.ActorRole.ADMINISTRATOR
        and previous is not None
        and previous.actor_role_snapshot
        == AppointmentSeriesLifecycleEvent.ActorRole.DIRECTOR
    ):
        raise PermissionDenied(
            "Администратор не может отменить последнее решение руководителя по серии."
        )


def _interrupt_unfinished_runs(series: AppointmentSeries) -> None:
    unfinished_runs = list(
        series.materialization_runs.select_for_update()
        .exclude(events__event_type="completed")
        .order_by("started_at", "pk")
    )
    for run in unfinished_runs:
        interrupt_run(
            run,
            reason="Запуск прерван явной остановкой materialization серии.",
        )


def _cancellation_results(
    event: AppointmentSeriesLifecycleEvent,
) -> tuple[AppointmentSeriesCancellationResult, ...]:
    return tuple(
        event.cancellation_results.select_related(
            "appointment",
            "appointment_participant",
            "source_materialization_result",
        ).order_by(
            "appointment__starts_at",
            "appointment_id",
        )
    )


@transaction.atomic
def stop_materialization(
    series: AppointmentSeries,
    *,
    operation_key: UUID,
    actor: Any,
    reason: str,
) -> SeriesLifecycleResult:
    role = require_operator_role(actor)
    reason = _normalized_reason(reason)
    locked = AppointmentSeries.objects.select_for_update().get(pk=series.pk)

    existing = _existing_event(operation_key)
    if existing is not None:
        return SeriesLifecycleResult(
            series=locked,
            event=_validate_existing_event(
                existing,
                locked,
                event_type=AppointmentSeriesLifecycleEvent.EventType.STOP_MATERIALIZATION,
                actor=actor,
                reason=reason,
            ),
            reused_event=True,
        )
    previous = _latest_series_event(locked)
    _assert_decision_priority(role, previous)
    if locked.status != AppointmentSeries.Status.ACTIVE:
        raise ValidationError("Остановить materialization можно только для активной серии.")
    event_number = (previous.event_number if previous else 0) + 1

    payload = _event_payload(
        series_id=locked.pk,
        event_type=AppointmentSeriesLifecycleEvent.EventType.STOP_MATERIALIZATION,
        event_number=event_number,
        status_from=AppointmentSeries.Status.ACTIVE,
        status_to=AppointmentSeries.Status.CANCELLED,
        actor_id=actor.pk,
        actor_role_snapshot=role,
        reason=reason,
        supersedes_id=None,
    )
    try:
        with transaction.atomic():
            event = AppointmentSeriesLifecycleEvent.objects.create(
                series=locked,
                operation_key=operation_key,
                fingerprint=canonical_fingerprint(payload),
                event_type=AppointmentSeriesLifecycleEvent.EventType.STOP_MATERIALIZATION,
                event_number=event_number,
                status_from=AppointmentSeries.Status.ACTIVE,
                status_to=AppointmentSeries.Status.CANCELLED,
                actor=actor,
                actor_role_snapshot=role,
                reason=reason,
            )
    except IntegrityError:
        existing = _existing_event(operation_key)
        if existing is None:
            raise
        event = _validate_existing_event(
            existing,
            locked,
            event_type=AppointmentSeriesLifecycleEvent.EventType.STOP_MATERIALIZATION,
            actor=actor,
            reason=reason,
        )
        return SeriesLifecycleResult(series=locked, event=event, reused_event=True)

    locked.status = AppointmentSeries.Status.CANCELLED
    locked.save(update_fields=["status", "updated_at"])
    _interrupt_unfinished_runs(locked)
    return SeriesLifecycleResult(series=locked, event=event, reused_event=False)


@transaction.atomic
def resume_materialization(
    series: AppointmentSeries,
    *,
    operation_key: UUID,
    actor: Any,
    reason: str,
) -> SeriesLifecycleResult:
    role = authority_role(actor)
    if role != AuthorityRole.DIRECTOR:
        raise PermissionDenied("Возобновить materialization может только руководитель.")
    reason = _normalized_reason(reason)
    locked = AppointmentSeries.objects.select_for_update().get(pk=series.pk)

    existing = _existing_event(operation_key)
    if existing is not None:
        return SeriesLifecycleResult(
            series=locked,
            event=_validate_existing_event(
                existing,
                locked,
                event_type=AppointmentSeriesLifecycleEvent.EventType.RESUME_MATERIALIZATION,
                actor=actor,
                reason=reason,
            ),
            reused_event=True,
        )
    if locked.status != AppointmentSeries.Status.CANCELLED:
        raise ValidationError("Возобновить materialization можно только после явной остановки.")

    previous = _latest_series_event(locked)
    if previous is None or previous.event_type == (
        AppointmentSeriesLifecycleEvent.EventType.RESUME_MATERIALIZATION
    ):
        raise SeriesLifecycleMismatch(
            "У серии нет остановившего lifecycle-события для переопределения."
        )

    payload = _event_payload(
        series_id=locked.pk,
        event_type=AppointmentSeriesLifecycleEvent.EventType.RESUME_MATERIALIZATION,
        event_number=previous.event_number + 1,
        status_from=AppointmentSeries.Status.CANCELLED,
        status_to=AppointmentSeries.Status.ACTIVE,
        actor_id=actor.pk,
        actor_role_snapshot=role.value,
        reason=reason,
        supersedes_id=previous.pk,
    )
    try:
        with transaction.atomic():
            event = AppointmentSeriesLifecycleEvent.objects.create(
                series=locked,
                operation_key=operation_key,
                fingerprint=canonical_fingerprint(payload),
                event_type=AppointmentSeriesLifecycleEvent.EventType.RESUME_MATERIALIZATION,
                event_number=previous.event_number + 1,
                status_from=AppointmentSeries.Status.CANCELLED,
                status_to=AppointmentSeries.Status.ACTIVE,
                actor=actor,
                actor_role_snapshot=AppointmentSeriesLifecycleEvent.ActorRole.DIRECTOR,
                reason=reason,
                supersedes=previous,
            )
    except IntegrityError:
        existing = _existing_event(operation_key)
        if existing is None:
            raise
        event = _validate_existing_event(
            existing,
            locked,
            event_type=AppointmentSeriesLifecycleEvent.EventType.RESUME_MATERIALIZATION,
            actor=actor,
            reason=reason,
        )
        return SeriesLifecycleResult(series=locked, event=event, reused_event=True)

    locked.status = AppointmentSeries.Status.ACTIVE
    locked.save(update_fields=["status", "updated_at"])
    return SeriesLifecycleResult(series=locked, event=event, reused_event=False)


def _composition_maps(
    series: AppointmentSeries,
    appointments: list[Appointment],
) -> tuple[
    dict[int, AppointmentSeriesMaterializationResult],
    dict[int, set[int]],
    dict[int, set[int]],
    dict[int, set[int]],
    dict[int, set[int]],
    set[int],
    set[int],
    set[int],
]:
    appointment_ids = [appointment.pk for appointment in appointments]
    source_by_appointment: dict[int, AppointmentSeriesMaterializationResult] = {}
    source_results = (
        AppointmentSeriesMaterializationResult.objects.select_related("revision")
        .filter(
            series=series,
            appointment_id__in=appointment_ids,
            outcome="created",
        )
        .order_by("appointment_id", "-attempt_number", "-pk")
    )
    for result in source_results:
        source_by_appointment.setdefault(result.appointment_id, result)

    revision_ids = {result.revision_id for result in source_by_appointment.values()}
    expected_children: dict[int, set[int]] = {revision_id: set() for revision_id in revision_ids}
    for revision_id, child_id in AppointmentSeriesRevisionParticipant.objects.filter(
        revision_id__in=revision_ids
    ).values_list("revision_id", "child_id"):
        expected_children[revision_id].add(child_id)
    expected_staff: dict[int, set[int]] = {revision_id: set() for revision_id in revision_ids}
    for revision_id, staff_id in AppointmentSeriesRevisionStaffAssignment.objects.filter(
        revision_id__in=revision_ids
    ).values_list("revision_id", "staff_member_id"):
        expected_staff[revision_id].add(staff_id)

    actual_children: dict[int, set[int]] = {appointment_id: set() for appointment_id in appointment_ids}
    participant_rows = list(
        AppointmentParticipant.objects.select_for_update()
        .filter(appointment_id__in=appointment_ids)
        .order_by("appointment_id", "pk")
    )
    for participant in participant_rows:
        if participant.appointment_status in _CANCELLABLE_APPOINTMENT_STATUSES:
            actual_children[participant.appointment_id].add(participant.child_id)
    marked_appointments = {
        participant.appointment_id
        for participant in participant_rows
        if participant.attendance_status != Appointment.AttendanceStatus.UNKNOWN
        or participant.marked_by_staff_at is not None
    }

    actual_staff: dict[int, set[int]] = {appointment_id: set() for appointment_id in appointment_ids}
    staff_rows = list(
        AppointmentStaffAssignment.objects.select_for_update()
        .filter(appointment_id__in=appointment_ids)
        .order_by("appointment_id", "pk")
    )
    for assignment in staff_rows:
        if assignment.appointment_status in _CANCELLABLE_APPOINTMENT_STATUSES:
            actual_staff[assignment.appointment_id].add(assignment.staff_member_id)

    external_join_appointments = set(
        AppointmentSeriesMaterializationResult.objects.filter(
            appointment_id__in=appointment_ids,
            outcome="joined",
            appointment_participant__appointment_status__in=(
                _CANCELLABLE_APPOINTMENT_STATUSES
            ),
        )
        .exclude(series=series)
        .values_list("appointment_id", flat=True)
    )
    foreign_created_appointments = set(
        AppointmentSeriesMaterializationResult.objects.filter(
            appointment_id__in=appointment_ids,
            outcome="created",
        )
        .exclude(series=series)
        .values_list("appointment_id", flat=True)
    )
    return (
        source_by_appointment,
        expected_children,
        expected_staff,
        actual_children,
        actual_staff,
        external_join_appointments,
        foreign_created_appointments,
        marked_appointments,
    )


def _lock_cancellation_financial_facts(appointments: list[Appointment]) -> None:
    appointment_ids = [appointment.pk for appointment in appointments]
    block_ids = set(
        AppointmentParticipant.objects.filter(
            appointment_id__in=appointment_ids,
            program_block_id__isnull=False,
        ).values_list("program_block_id", flat=True)
    )
    block_ids.update(
        appointment.program_block_id
        for appointment in appointments
        if appointment.program_block_id
    )
    list(
        ProgramBlock.objects.select_for_update()
        .filter(pk__in=block_ids)
        .order_by("pk")
    )

    account_ids = set(
        AppointmentParticipant.objects.filter(
            appointment_id__in=appointment_ids,
            billing_account_id__isnull=False,
        ).values_list("billing_account_id", flat=True)
    )
    account_ids.update(
        appointment.billing_account_id
        for appointment in appointments
        if appointment.billing_account_id
    )
    list(
        BalanceAccount.all_objects.select_for_update()
        .filter(pk__in=account_ids)
        .order_by("pk")
    )
    list(
        LedgerEntry.objects.select_for_update()
        .filter(appointment_id__in=appointment_ids)
        .order_by("pk")
    )


def _discover_participant_lineage_rows(
    root_ids: set[int],
) -> dict[int, dict[str, int | None]]:
    fields = (
        "pk",
        "appointment_id",
        "source_participant_id",
    )
    rows = {
        row["pk"]: row
        for row in AppointmentParticipant.objects.filter(pk__in=root_ids).values(*fields)
    }
    frontier = set(rows)
    while frontier:
        successors = list(
            AppointmentParticipant.objects.filter(
                source_participant_id__in=frontier
            ).values(*fields)
        )
        frontier = {
            row["pk"]
            for row in successors
            if row["pk"] not in rows
        }
        rows.update({row["pk"]: row for row in successors})
    return rows


def _lock_withdrawal_appointments(root_ids: set[int]) -> list[Appointment]:
    """Lock every committed appointment in each lineage, including a racing move."""

    locked_by_id: dict[int, Appointment] = {}
    known_participant_ids: set[int] = set()
    while True:
        lineage_rows = _discover_participant_lineage_rows(root_ids)
        appointment_ids = {
            int(row["appointment_id"])
            for row in lineage_rows.values()
            if row["appointment_id"] is not None
        }
        unlocked_ids = appointment_ids - set(locked_by_id)
        if unlocked_ids:
            locked_by_id.update(
                {
                    appointment.pk: appointment
                    for appointment in Appointment.objects.select_for_update()
                    .filter(pk__in=unlocked_ids)
                    .order_by("pk")
                }
            )
        current_participant_ids = set(lineage_rows)
        if current_participant_ids == known_participant_ids and not unlocked_ids:
            break
        known_participant_ids = current_participant_ids
    return [locked_by_id[pk] for pk in sorted(locked_by_id)]


def _locked_withdrawal_participants(
    appointments: list[Appointment],
) -> list[AppointmentParticipant]:
    appointment_ids = [appointment.pk for appointment in appointments]
    participants = list(
        AppointmentParticipant.objects.select_for_update(of=("self",))
        .select_related(
            "appointment",
            "appointment__service",
        )
        .filter(appointment_id__in=appointment_ids)
        .order_by("appointment_id", "pk")
    )
    list(
        AppointmentStaffAssignment.objects.select_for_update()
        .filter(appointment_id__in=appointment_ids)
        .order_by("appointment_id", "pk")
    )
    return participants


def _participant_lineage(
    source: AppointmentSeriesMaterializationResult,
    *,
    participants_by_id: dict[int, AppointmentParticipant],
    successors_by_source_id: dict[int, list[AppointmentParticipant]],
) -> tuple[list[AppointmentParticipant], str | None]:
    root_id = source.appointment_participant_id
    root = participants_by_id.get(root_id)
    if root is None:
        return [], "missing_root_participant"
    lineage = [root]
    seen = {root.pk}
    current = root
    while True:
        successors = successors_by_source_id.get(current.pk, [])
        if not successors:
            return lineage, None
        if len(successors) != 1:
            return lineage, "branched_lineage"
        successor = successors[0]
        if successor.pk in seen:
            return lineage, "cyclic_lineage"
        seen.add(successor.pk)
        lineage.append(successor)
        current = successor


def _withdrawal_decision(
    source: AppointmentSeriesMaterializationResult,
    lineage: list[AppointmentParticipant],
    lineage_error: str | None,
    *,
    event: AppointmentSeriesLifecycleEvent,
    foreign_owned_participant_ids: set[int],
) -> tuple[AppointmentParticipant, str, str, str]:
    target = lineage[-1]
    appointment = target.appointment
    if lineage_error:
        return (
            target,
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            lineage_error,
            "Линия переносов участия повреждена или неоднозначна.",
        )
    if source.provenance_kind == (
        AppointmentSeriesMaterializationResult.ProvenanceKind.LEGACY_UNKNOWN
    ):
        return (
            target,
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "legacy_unknown",
            "Legacy-происхождение участия недостаточно точно для массового снятия.",
        )
    root = lineage[0]
    if (
        root.pk != source.appointment_participant_id
        or root.appointment_id != source.appointment_id
    ):
        return (
            target,
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "source_projection_changed",
            "Канонический joined-result не совпадает с корнем линии участия.",
        )
    if any(
        participant.child_id != root.child_id
        or participant.program_block_id != root.program_block_id
        or participant.appointment.service_id != source.revision.service_id
        for participant in lineage
    ):
        return (
            target,
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "lineage_projection_changed",
            "Получатель, каскад или услуга изменились внутри линии переносов.",
        )
    membership_exists = AppointmentSeriesRevisionParticipant.objects.filter(
        revision_id=source.revision_id,
        child_id=target.child_id,
        program_block_id=target.program_block_id,
    ).exists()
    if not membership_exists:
        return (
            target,
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "revision_membership_changed",
            "Текущее участие не совпадает с получателем и каскадом редакции серии.",
        )
    if any(item.pk in foreign_owned_participant_ids for item in lineage):
        return (
            target,
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "foreign_join_owner",
            "На эту линию участия указывает joined-result другой серии.",
        )
    active_lineage = [
        participant
        for participant in lineage
        if participant.appointment_status in _CANCELLABLE_APPOINTMENT_STATUSES
    ]
    if len(active_lineage) > 1 or (active_lineage and active_lineage[0].pk != target.pk):
        return (
            target,
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "multiple_active_lineage_rows",
            "В линии переносов найдено несколько активных участий.",
        )
    if appointment.starts_at <= event.occurred_at:
        return (
            target,
            AppointmentSeriesCancellationResult.Outcome.UNCHANGED,
            "not_future",
            "Текущее участие уже началось или осталось в прошлом.",
        )
    if appointment.status in {
        Appointment.Status.COMPLETED,
        Appointment.Status.NO_SHOW,
        Appointment.Status.CANCELLED,
    }:
        return (
            target,
            AppointmentSeriesCancellationResult.Outcome.UNCHANGED,
            "terminal_appointment",
            "Терминальный статус общего занятия не изменяется.",
        )
    if target.appointment_status == Appointment.Status.CANCELLED:
        return (
            target,
            AppointmentSeriesCancellationResult.Outcome.UNCHANGED,
            "already_withdrawn",
            "Участие уже отменено; повторное изменение не требуется.",
        )
    if target.appointment_status == Appointment.Status.RESCHEDULED:
        return (
            target,
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "orphaned_reschedule",
            "Перенесенное участие не имеет доказуемого актуального преемника.",
        )
    if (
        appointment.status not in _CANCELLABLE_APPOINTMENT_STATUSES
        or target.appointment_status not in _CANCELLABLE_APPOINTMENT_STATUSES
    ):
        return (
            target,
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "unsupported_status",
            "Статус общего занятия или участия требует ручного решения.",
        )
    if (
        target.attendance_status != Appointment.AttendanceStatus.UNKNOWN
        or target.marked_by_staff_at is not None
        or appointment.attendance_status != Appointment.AttendanceStatus.UNKNOWN
        or appointment.specialist_marked_at is not None
    ):
        return (
            target,
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "attendance_recorded",
            "По участию или общему занятию уже зафиксирована отметка специалиста.",
        )
    return (
        target,
        AppointmentSeriesCancellationResult.Outcome.CANCELLED,
        "withdrawn",
        "Будущее участие безопасно снято join-серией.",
    )


def _cancellation_decision(
    appointment: Appointment,
    source: AppointmentSeriesMaterializationResult | None,
    *,
    expected_children: dict[int, set[int]],
    expected_staff: dict[int, set[int]],
    actual_children: dict[int, set[int]],
    actual_staff: dict[int, set[int]],
    external_join_appointments: set[int],
    foreign_created_appointments: set[int],
    marked_appointments: set[int],
) -> tuple[str, str, str]:
    if appointment.starts_at <= timezone.now():
        return (
            AppointmentSeriesCancellationResult.Outcome.UNCHANGED,
            "not_future",
            "Занятие уже началось или осталось в прошлом; статус не изменен.",
        )
    if appointment.status == Appointment.Status.CANCELLED:
        return (
            AppointmentSeriesCancellationResult.Outcome.UNCHANGED,
            "already_cancelled",
            "Занятие уже было отменено; повторное изменение не требуется.",
        )
    if appointment.status in {
        Appointment.Status.COMPLETED,
        Appointment.Status.NO_SHOW,
        Appointment.Status.RESCHEDULED,
    }:
        return (
            AppointmentSeriesCancellationResult.Outcome.UNCHANGED,
            "terminal_fact",
            "Проведенный, отмеченный или перенесенный факт не изменяется.",
        )
    if appointment.status not in _CANCELLABLE_APPOINTMENT_STATUSES:
        return (
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "unsupported_status",
            "Статус занятия требует отдельного ручного решения.",
        )
    if source is None:
        return (
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "missing_materialization_fact",
            "Не найден канонический created-result, подтверждающий владение занятием.",
        )
    if source.provenance_kind == (
        AppointmentSeriesMaterializationResult.ProvenanceKind.LEGACY_UNKNOWN
    ):
        return (
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "legacy_unknown",
            "Legacy-происхождение занятия недостаточно точно для массовой отмены.",
        )
    revision = source.revision
    if appointment.series_id != source.series_id:
        return (
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "series_projection_changed",
            "Текущая проекция серии занятия не совпадает с каноническим результатом создания.",
        )
    if (
        source.scheduled_starts_at != appointment.starts_at
        or appointment.service_id != revision.service_id
        or appointment.room_id != revision.room_id
        or appointment.session_type != revision.session_type
        or appointment.duration_minutes != revision.duration_minutes
    ):
        return (
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "materialization_projection_changed",
            "Время, услуга, кабинет или тип занятия отличаются от зафиксированного результата materialization.",
        )
    if appointment.pk in foreign_created_appointments:
        return (
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "foreign_created_owner",
            "На занятие указывает результат создания другой серии; требуется ручная проверка владения.",
        )
    if appointment.pk in external_join_appointments:
        return (
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "external_join",
            "К занятию присоединено активное участие другой серии.",
        )
    if (
        appointment.attendance_status != Appointment.AttendanceStatus.UNKNOWN
        or appointment.specialist_marked_at is not None
        or appointment.pk in marked_appointments
    ):
        return (
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "attendance_recorded",
            "По занятию или его участникам уже зафиксирована отметка специалиста.",
        )
    if (
        actual_children.get(appointment.pk, set())
        != expected_children.get(source.revision_id, set())
        or actual_staff.get(appointment.pk, set())
        != expected_staff.get(source.revision_id, set())
    ):
        return (
            AppointmentSeriesCancellationResult.Outcome.MANUAL_REVIEW,
            "composition_changed",
            "Фактический состав получателей или специалистов отличается от редакции серии.",
        )
    return (
        AppointmentSeriesCancellationResult.Outcome.CANCELLED,
        "cancelled",
        "Будущее неначавшееся занятие безопасно отменено серией.",
    )


@transaction.atomic
def cancel_future_unstarted(
    series: AppointmentSeries,
    *,
    operation_key: UUID,
    actor: Any,
    reason: str,
) -> SeriesCancellationResult:
    role = require_operator_role(actor)
    reason = _normalized_reason(reason)
    locked = AppointmentSeries.objects.select_for_update().get(pk=series.pk)

    existing = _existing_event(operation_key)
    if existing is not None:
        event = _validate_existing_event(
            existing,
            locked,
            event_type=(
                AppointmentSeriesLifecycleEvent.EventType.CANCEL_FUTURE_UNSTARTED
            ),
            actor=actor,
            reason=reason,
        )
        return SeriesCancellationResult(
            series=locked,
            event=event,
            results=_cancellation_results(event),
            reused_event=True,
        )
    if locked.materialization_mode != (
        AppointmentSeries.MaterializationMode.CREATE_APPOINTMENTS
    ):
        raise ValidationError(
            "Join-серия не может массово отменять общее занятие; используйте снятие участия."
        )
    if locked.status not in {
        AppointmentSeries.Status.ACTIVE,
        AppointmentSeries.Status.CANCELLED,
    }:
        raise ValidationError(
            "Отменять будущие занятия можно только для активной или остановленной серии."
        )

    previous = _latest_series_event(locked)
    _assert_decision_priority(role, previous)
    status_from = locked.status
    event_number = (previous.event_number if previous else 0) + 1
    payload = _event_payload(
        series_id=locked.pk,
        event_type=AppointmentSeriesLifecycleEvent.EventType.CANCEL_FUTURE_UNSTARTED,
        event_number=event_number,
        status_from=status_from,
        status_to=AppointmentSeries.Status.CANCELLED,
        actor_id=actor.pk,
        actor_role_snapshot=role,
        reason=reason,
        supersedes_id=None,
    )
    try:
        with transaction.atomic():
            event = AppointmentSeriesLifecycleEvent.objects.create(
                series=locked,
                operation_key=operation_key,
                fingerprint=canonical_fingerprint(payload),
                event_type=(
                    AppointmentSeriesLifecycleEvent.EventType.CANCEL_FUTURE_UNSTARTED
                ),
                event_number=event_number,
                status_from=status_from,
                status_to=AppointmentSeries.Status.CANCELLED,
                actor=actor,
                actor_role_snapshot=role,
                reason=reason,
            )
    except IntegrityError:
        existing = _existing_event(operation_key)
        if existing is None:
            raise
        event = _validate_existing_event(
            existing,
            locked,
            event_type=(
                AppointmentSeriesLifecycleEvent.EventType.CANCEL_FUTURE_UNSTARTED
            ),
            actor=actor,
            reason=reason,
        )
        return SeriesCancellationResult(
            series=locked,
            event=event,
            results=_cancellation_results(event),
            reused_event=True,
        )

    if locked.status != AppointmentSeries.Status.CANCELLED:
        locked.status = AppointmentSeries.Status.CANCELLED
        locked.save(update_fields=["status", "updated_at"])
    _interrupt_unfinished_runs(locked)

    canonical_appointment_ids = set(
        AppointmentSeriesMaterializationResult.objects.filter(
            series=locked,
            outcome="created",
            appointment__starts_at__gt=event.occurred_at,
        ).values_list("appointment_id", flat=True)
    )
    legacy_appointment_ids = set(
        Appointment.objects.filter(
            series=locked,
            starts_at__gt=event.occurred_at,
        ).values_list("pk", flat=True)
    )
    appointments = list(
        Appointment.objects.select_for_update()
        .filter(pk__in=canonical_appointment_ids | legacy_appointment_ids)
        .order_by("pk")
    )
    (
        source_by_appointment,
        expected_children,
        expected_staff,
        actual_children,
        actual_staff,
        external_join_appointments,
        foreign_created_appointments,
        marked_appointments,
    ) = _composition_maps(locked, appointments)
    _lock_cancellation_financial_facts(appointments)

    results = []
    for appointment in appointments:
        status_from = appointment.status
        outcome, reason_code, result_reason = _cancellation_decision(
            appointment,
            source_by_appointment.get(appointment.pk),
            expected_children=expected_children,
            expected_staff=expected_staff,
            actual_children=actual_children,
            actual_staff=actual_staff,
            external_join_appointments=external_join_appointments,
            foreign_created_appointments=foreign_created_appointments,
            marked_appointments=marked_appointments,
        )
        if outcome == AppointmentSeriesCancellationResult.Outcome.CANCELLED:
            appointment_svc.set_locked_appointment_status(
                appointment,
                status=Appointment.Status.CANCELLED,
                note_lines=[f"Отмена будущего занятия серией: {reason}"],
            )
        results.append(
            AppointmentSeriesCancellationResult.objects.create(
                lifecycle_event=event,
                appointment=appointment,
                source_materialization_result=source_by_appointment.get(
                    appointment.pk
                ),
                outcome=outcome,
                status_from=status_from,
                status_to=appointment.status,
                reason_code=reason_code,
                reason=result_reason,
                processed_at=max(timezone.now(), event.occurred_at),
            )
        )
    return SeriesCancellationResult(
        series=locked,
        event=event,
        results=tuple(results),
        reused_event=False,
    )


@transaction.atomic
def withdraw_future_joined_participations(
    series: AppointmentSeries,
    *,
    operation_key: UUID,
    actor: Any,
    reason: str,
) -> SeriesCancellationResult:
    role = require_operator_role(actor)
    reason = _normalized_reason(reason)
    locked = AppointmentSeries.objects.select_for_update().get(pk=series.pk)

    existing = _existing_event(operation_key)
    if existing is not None:
        event = _validate_existing_event(
            existing,
            locked,
            event_type=(
                AppointmentSeriesLifecycleEvent.EventType.WITHDRAW_FUTURE_JOINED_PARTICIPATIONS
            ),
            actor=actor,
            reason=reason,
        )
        return SeriesCancellationResult(
            series=locked,
            event=event,
            results=_cancellation_results(event),
            reused_event=True,
        )
    if locked.materialization_mode != AppointmentSeries.MaterializationMode.JOIN_EXISTING:
        raise ValidationError(
            "Снимать отдельные участия можно только для join-серии."
        )
    if locked.status not in {
        AppointmentSeries.Status.ACTIVE,
        AppointmentSeries.Status.CANCELLED,
    }:
        raise ValidationError(
            "Снимать будущие участия можно только для активной или остановленной серии."
        )

    previous = _latest_series_event(locked)
    _assert_decision_priority(role, previous)
    status_from = locked.status
    event_number = (previous.event_number if previous else 0) + 1
    event_type = (
        AppointmentSeriesLifecycleEvent.EventType.WITHDRAW_FUTURE_JOINED_PARTICIPATIONS
    )
    payload = _event_payload(
        series_id=locked.pk,
        event_type=event_type,
        event_number=event_number,
        status_from=status_from,
        status_to=AppointmentSeries.Status.CANCELLED,
        actor_id=actor.pk,
        actor_role_snapshot=role,
        reason=reason,
        supersedes_id=None,
    )
    try:
        with transaction.atomic():
            event = AppointmentSeriesLifecycleEvent.objects.create(
                series=locked,
                operation_key=operation_key,
                fingerprint=canonical_fingerprint(payload),
                event_type=event_type,
                event_number=event_number,
                status_from=status_from,
                status_to=AppointmentSeries.Status.CANCELLED,
                actor=actor,
                actor_role_snapshot=role,
                reason=reason,
            )
    except IntegrityError:
        existing = _existing_event(operation_key)
        if existing is None:
            raise
        event = _validate_existing_event(
            existing,
            locked,
            event_type=event_type,
            actor=actor,
            reason=reason,
        )
        return SeriesCancellationResult(
            series=locked,
            event=event,
            results=_cancellation_results(event),
            reused_event=True,
        )

    if locked.status != AppointmentSeries.Status.CANCELLED:
        locked.status = AppointmentSeries.Status.CANCELLED
        locked.save(update_fields=["status", "updated_at"])
    _interrupt_unfinished_runs(locked)

    sources = list(
        AppointmentSeriesMaterializationResult.objects.select_related(
            "revision",
            "appointment",
            "appointment_participant",
        )
        .filter(
            series=locked,
            outcome=AppointmentSeriesOccurrence.Outcome.JOINED,
            appointment_participant__isnull=False,
        )
        .order_by("scheduled_starts_at", "attempt_number", "pk")
    )
    root_ids = {
        source.appointment_participant_id
        for source in sources
        if source.appointment_participant_id is not None
    }
    appointments = _lock_withdrawal_appointments(root_ids)
    participants = _locked_withdrawal_participants(appointments)
    _lock_cancellation_financial_facts(appointments)

    participants_by_id = {participant.pk: participant for participant in participants}
    successors_by_source_id: dict[int, list[AppointmentParticipant]] = {}
    for participant in participants:
        if participant.source_participant_id is not None:
            successors_by_source_id.setdefault(
                participant.source_participant_id,
                [],
            ).append(participant)
    foreign_owned_participant_ids = set(
        AppointmentSeriesMaterializationResult.objects.filter(
            outcome=AppointmentSeriesOccurrence.Outcome.JOINED,
            appointment_participant_id__in=participants_by_id,
        )
        .exclude(series=locked)
        .values_list("appointment_participant_id", flat=True)
    )

    results = []
    for source in sources:
        lineage, lineage_error = _participant_lineage(
            source,
            participants_by_id=participants_by_id,
            successors_by_source_id=successors_by_source_id,
        )
        if not lineage:
            raise SeriesLifecycleMismatch(
                "Канонический joined-result потерял защищенный корень участия."
            )
        target, outcome, reason_code, result_reason = _withdrawal_decision(
            source,
            lineage,
            lineage_error,
            event=event,
            foreign_owned_participant_ids=foreign_owned_participant_ids,
        )
        status_from = target.appointment_status
        if outcome == AppointmentSeriesCancellationResult.Outcome.CANCELLED:
            target.appointment_status = Appointment.Status.CANCELLED
            target.admin_note = "\n".join(
                part
                for part in [
                    target.admin_note,
                    f"Снятие будущего участия join-серией: {reason}",
                ]
                if part
            )
            target.save(
                update_fields=[
                    "appointment_status",
                    "admin_note",
                    "updated_at",
                ]
            )
        results.append(
            AppointmentSeriesCancellationResult.objects.create(
                lifecycle_event=event,
                appointment=target.appointment,
                appointment_participant=target,
                source_materialization_result=source,
                outcome=outcome,
                status_from=status_from,
                status_to=target.appointment_status,
                reason_code=reason_code,
                reason=result_reason,
                processed_at=max(timezone.now(), event.occurred_at),
            )
        )
    return SeriesCancellationResult(
        series=locked,
        event=event,
        results=tuple(results),
        reused_event=False,
    )
