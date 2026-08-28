"""Version and execution journal helpers for appointment series."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from operations.models import (
    AppointmentSeries,
    AppointmentSeriesMaterializationResult,
    AppointmentSeriesMaterializationRun,
    AppointmentSeriesMaterializationRunEvent,
    AppointmentSeriesOccurrence,
    AppointmentSeriesParticipant,
    AppointmentSeriesRevision,
    AppointmentSeriesRevisionParticipant,
    AppointmentSeriesRevisionStaffAssignment,
    AppointmentSeriesStaffAssignment,
)
from operations.services.authority import AuthorityRole, authority_role


class SeriesRevisionMismatch(ValidationError):
    """The mutable root projection no longer matches its immutable revision."""


def canonical_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_operator_role(actor: Any) -> str:
    role = authority_role(actor)
    if role not in {AuthorityRole.DIRECTOR, AuthorityRole.ADMINISTRATOR}:
        raise PermissionDenied("Сериями занятий управляет администратор или руководитель.")
    return role.value


def _series_composition(
    series: AppointmentSeries,
) -> tuple[list[AppointmentSeriesParticipant], list[AppointmentSeriesStaffAssignment]]:
    participants = list(
        series.default_participants.select_related(
            "child",
            "program_block__program",
            "billing_account",
        ).order_by("position", "pk")
    )
    assignments = list(
        series.default_staff_assignments.select_related("staff_member").order_by("pk")
    )
    return participants, assignments


def _validate_composition(
    series: AppointmentSeries,
    participants: list[AppointmentSeriesParticipant],
    assignments: list[AppointmentSeriesStaffAssignment],
    *,
    allow_single_legacy_group: bool = False,
) -> None:
    if not participants:
        raise ValidationError("У серии должен быть хотя бы один получатель.")
    positions = [participant.position for participant in participants]
    if len(positions) != len(set(positions)):
        raise ValidationError("Позиции получателей серии не должны повторяться.")

    if series.materialization_mode == AppointmentSeries.MaterializationMode.JOIN_EXISTING:
        if series.session_type != "group" or len(participants) != 1 or assignments:
            raise ValidationError(
                "Операция присоединения хранит одного получателя без собственного состава "
                "специалистов."
            )
        return

    primary_count = sum(
        assignment.role == AppointmentSeriesStaffAssignment.Role.PRIMARY
        for assignment in assignments
    )
    if primary_count != 1:
        raise ValidationError("В серии должен быть ровно один основной специалист.")
    if series.session_type == "individual" and (
        len(participants) != 1 or len(assignments) != 1
    ):
        raise ValidationError(
            "Индивидуальная серия содержит одного получателя и одного специалиста."
        )
    if (
        series.session_type == "group"
        and len(participants) < 2
        and not allow_single_legacy_group
    ):
        raise ValidationError("Групповая серия должна содержать минимум двух получателей.")


def _revision_payload(
    series: AppointmentSeries,
    participants: Iterable[AppointmentSeriesParticipant],
    assignments: Iterable[AppointmentSeriesStaffAssignment],
    *,
    revision_number: int,
) -> dict[str, Any]:
    return {
        "series_id": series.pk,
        "revision_number": revision_number,
        "title": series.title,
        "service_id": series.service_id,
        "room_id": series.room_id,
        "start_date": series.start_date,
        "end_date": series.end_date,
        "days_of_week": series.days_of_week,
        "time": series.time,
        "duration_minutes": series.duration_minutes,
        "session_type": series.session_type,
        "materialization_mode": series.materialization_mode,
        "default_appointment_status": series.default_appointment_status,
        "allow_unpaid_reserve": series.allow_unpaid_reserve,
        "allow_outside_availability": series.allow_outside_availability,
        "override_reason": series.override_reason,
        "participants": [
            (
                item.child_id,
                item.program_block_id,
                item.billing_account_id,
                item.position,
            )
            for item in participants
        ],
        "staff": [
            (
                item.staff_member_id,
                item.role,
                item.override_availability,
                item.override_reason,
            )
            for item in assignments
        ],
    }


def assert_current_projection(
    series: AppointmentSeries,
    revision: AppointmentSeriesRevision,
) -> None:
    if series.current_revision_id != revision.pk or revision.series_id != series.pk:
        raise SeriesRevisionMismatch(
            "Текущая редакция серии изменилась во время материализации."
        )
    participants, assignments = _series_composition(series)
    _validate_composition(
        series,
        participants,
        assignments,
        allow_single_legacy_group=(
            revision.provenance_kind
            != AppointmentSeriesRevision.ProvenanceKind.NATIVE
        ),
    )
    payload = _revision_payload(
        series,
        participants,
        assignments,
        revision_number=revision.revision_number,
    )
    if canonical_fingerprint(payload) != revision.fingerprint:
        raise SeriesRevisionMismatch(
            "Текущая проекция серии не совпадает с immutable-редакцией."
        )


@transaction.atomic
def ensure_initial_revision(
    series: AppointmentSeries,
    *,
    actor: Any,
) -> tuple[AppointmentSeriesRevision, bool]:
    role = require_operator_role(actor)
    locked = AppointmentSeries.objects.select_for_update().get(pk=series.pk)
    if locked.current_revision_id:
        revision = AppointmentSeriesRevision.objects.get(pk=locked.current_revision_id)
        assert_current_projection(locked, revision)
        return revision, False
    if AppointmentSeriesOccurrence.objects.filter(series=locked).exists():
        raise ValidationError(
            "Существующая история серии должна пройти legacy backfill до нового запуска."
        )

    participants, assignments = _series_composition(locked)
    _validate_composition(locked, participants, assignments)
    payload = _revision_payload(
        locked,
        participants,
        assignments,
        revision_number=1,
    )
    now = timezone.now()
    revision = AppointmentSeriesRevision.objects.create(
        series=locked,
        revision_number=1,
        event_type=AppointmentSeriesRevision.EventType.CREATED,
        provenance_kind=AppointmentSeriesRevision.ProvenanceKind.NATIVE,
        effective_from=locked.start_date,
        title=locked.title,
        service=locked.service,
        room=locked.room,
        start_date=locked.start_date,
        end_date=locked.end_date,
        days_of_week=locked.days_of_week,
        time=locked.time,
        duration_minutes=locked.duration_minutes,
        session_type=locked.session_type,
        materialization_mode=locked.materialization_mode,
        default_appointment_status=locked.default_appointment_status,
        allow_unpaid_reserve=locked.allow_unpaid_reserve,
        allow_outside_availability=locked.allow_outside_availability,
        override_reason=locked.override_reason,
        fingerprint=canonical_fingerprint(payload),
        actor=actor,
        actor_role_snapshot=role,
        reason="Первичная редакция при создании серии.",
        decided_at=now,
    )
    for participant in participants:
        AppointmentSeriesRevisionParticipant.objects.create(
            revision=revision,
            child=participant.child,
            program_block=participant.program_block,
            billing_account=participant.billing_account,
            position=participant.position,
        )
    for assignment in assignments:
        AppointmentSeriesRevisionStaffAssignment.objects.create(
            revision=revision,
            staff_member=assignment.staff_member,
            role=assignment.role,
            override_availability=assignment.override_availability,
            override_reason=assignment.override_reason,
        )
    AppointmentSeries.objects.filter(pk=locked.pk).update(
        current_revision=revision,
        updated_at=now,
    )
    return revision, True


def _run_payload(
    series: AppointmentSeries,
    revision: AppointmentSeriesRevision,
    *,
    actor: Any,
    reason: str,
    expected_result_count: int,
    target_appointment_ids: Iterable[int] = (),
) -> dict[str, Any]:
    return {
        "series_id": series.pk,
        "revision_id": revision.pk,
        "revision_fingerprint": revision.fingerprint,
        "mode": AppointmentSeriesMaterializationRun.Mode.INITIAL,
        "date_from": revision.start_date,
        "date_to": revision.end_date,
        "expected_result_count": expected_result_count,
        "actor_id": actor.pk,
        "reason": reason,
        "target_appointment_ids": sorted(set(target_appointment_ids)),
    }


@transaction.atomic
def get_or_create_initial_run(
    series: AppointmentSeries,
    revision: AppointmentSeriesRevision,
    *,
    operation_key: UUID,
    actor: Any,
    expected_result_count: int,
    target_appointment_ids: Iterable[int] = (),
) -> tuple[AppointmentSeriesMaterializationRun, bool]:
    role = require_operator_role(actor)
    reason = "Первичная материализация серии."
    payload = _run_payload(
        series,
        revision,
        actor=actor,
        reason=reason,
        expected_result_count=expected_result_count,
        target_appointment_ids=target_appointment_ids,
    )
    fingerprint = canonical_fingerprint(payload)
    locked = AppointmentSeries.objects.select_for_update().get(pk=series.pk)
    if locked.status != AppointmentSeries.Status.ACTIVE:
        raise ValidationError("Создавать занятия можно только для активной серии.")
    if locked.current_revision_id != revision.pk or revision.series_id != locked.pk:
        raise ValidationError("Первый запуск должен использовать текущую редакцию серии.")

    existing = AppointmentSeriesMaterializationRun.objects.filter(
        operation_key=operation_key
    ).first()
    if existing:
        if (
            existing.series_id == locked.pk
            and existing.revision_id == revision.pk
            and existing.mode == AppointmentSeriesMaterializationRun.Mode.LEGACY_IMPORT
        ):
            if existing.expected_result_count != expected_result_count:
                raise ValidationError(
                    "Legacy-запуск не покрывает все даты серии; нужен отдельный "
                    "запуск недостающих дат."
                )
            if not existing.events.filter(
                event_type=AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED
            ).exists():
                raise ValidationError("Legacy-запуск серии не имеет завершенного журнала.")
            return existing, False
        if (
            existing.series_id != locked.pk
            or existing.revision_id != revision.pk
            or existing.fingerprint != fingerprint
        ):
            raise ValidationError("Ключ запуска уже использован для другой операции.")
        return existing, False
    if AppointmentSeriesMaterializationRun.objects.filter(
        revision=revision,
        mode__in=[
            AppointmentSeriesMaterializationRun.Mode.INITIAL,
            AppointmentSeriesMaterializationRun.Mode.LEGACY_IMPORT,
        ],
    ).exists():
        raise ValidationError("Первая материализация этой редакции уже зарегистрирована.")

    try:
        with transaction.atomic():
            run = AppointmentSeriesMaterializationRun.objects.create(
                series=locked,
                revision=revision,
                operation_key=operation_key,
                fingerprint=fingerprint,
                mode=AppointmentSeriesMaterializationRun.Mode.INITIAL,
                date_from=revision.start_date,
                date_to=revision.end_date,
                expected_result_count=expected_result_count,
                actor=actor,
                actor_role_snapshot=role,
                reason=reason,
            )
    except IntegrityError as exc:
        existing = AppointmentSeriesMaterializationRun.objects.filter(
            operation_key=operation_key
        ).first()
        if existing and existing.fingerprint == fingerprint:
            return existing, False
        raise ValidationError("Первый запуск серии уже создан конкурентным запросом.") from exc
    return run, True


@transaction.atomic
def record_compatibility_result(
    run: AppointmentSeriesMaterializationRun,
    occurrence: AppointmentSeriesOccurrence,
) -> AppointmentSeriesMaterializationResult:
    locked_run = AppointmentSeriesMaterializationRun.objects.select_for_update().get(
        pk=run.pk
    )
    existing = AppointmentSeriesMaterializationResult.objects.filter(
        run=locked_run,
        scheduled_starts_at=occurrence.scheduled_starts_at,
    ).first()
    if existing:
        if (
            existing.compatibility_occurrence_id != occurrence.pk
            or existing.series_id != occurrence.series_id
            or existing.scheduled_starts_at != occurrence.scheduled_starts_at
            or existing.appointment_id != occurrence.appointment_id
            or existing.appointment_participant_id
            != occurrence.appointment_participant_id
            or existing.outcome != occurrence.outcome
            or existing.reason_code != occurrence.reason_code
            or existing.reason != occurrence.reason
        ):
            raise ValidationError(
                "Существующий результат запуска не совпадает с immutable occurrence."
            )
        return existing
    if locked_run.events.filter(
        event_type=AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED
    ).exists():
        raise ValidationError("Завершенный запуск не принимает новые результаты.")
    return AppointmentSeriesMaterializationResult.objects.create(
        series_id=locked_run.series_id,
        revision_id=locked_run.revision_id,
        run=locked_run,
        scheduled_starts_at=occurrence.scheduled_starts_at,
        scheduled_date=timezone.localtime(occurrence.scheduled_starts_at).date(),
        attempt_number=1,
        provenance_kind=AppointmentSeriesMaterializationResult.ProvenanceKind.NATIVE,
        appointment=occurrence.appointment,
        appointment_participant=occurrence.appointment_participant,
        outcome=occurrence.outcome,
        reason_code=occurrence.reason_code,
        reason=occurrence.reason,
        compatibility_occurrence=occurrence,
    )


@transaction.atomic
def complete_run(
    run: AppointmentSeriesMaterializationRun,
) -> AppointmentSeriesMaterializationRunEvent:
    locked_run = AppointmentSeriesMaterializationRun.objects.select_for_update().get(
        pk=run.pk
    )
    completed = locked_run.events.filter(
        event_type=AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED
    ).first()
    if completed:
        return completed
    outcomes = Counter(locked_run.results.values_list("outcome", flat=True))
    result_count = sum(outcomes.values())
    if result_count != locked_run.expected_result_count:
        raise ValidationError(
            "Запуск нельзя завершить: сохранено "
            f"{result_count} из {locked_run.expected_result_count} результатов."
        )
    return AppointmentSeriesMaterializationRunEvent.objects.create(
        run=locked_run,
        event_number=(
            locked_run.events.order_by("-event_number")
            .values_list("event_number", flat=True)
            .first()
            or 0
        )
        + 1,
        event_type=AppointmentSeriesMaterializationRunEvent.EventType.COMPLETED,
        result_count=result_count,
        created_count=outcomes[AppointmentSeriesOccurrence.Outcome.CREATED],
        joined_count=outcomes[AppointmentSeriesOccurrence.Outcome.JOINED],
        skipped_count=outcomes[AppointmentSeriesOccurrence.Outcome.SKIPPED],
        unchanged_count=outcomes[AppointmentSeriesOccurrence.Outcome.UNCHANGED],
    )
