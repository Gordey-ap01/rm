"""Authority-aware lifecycle transitions for appointment series."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction

from operations.models import (
    AppointmentSeries,
    AppointmentSeriesLifecycleEvent,
    normalize_immutable_reason,
)
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
    if (
        role == AppointmentSeriesLifecycleEvent.ActorRole.ADMINISTRATOR
        and previous is not None
        and previous.actor_role_snapshot
        == AppointmentSeriesLifecycleEvent.ActorRole.DIRECTOR
    ):
        raise PermissionDenied(
            "Администратор не может отменить последнее решение руководителя по серии."
        )
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
    unfinished_runs = list(
        locked.materialization_runs.select_for_update()
        .exclude(events__event_type="completed")
        .order_by("started_at", "pk")
    )
    for run in unfinished_runs:
        interrupt_run(
            run,
            reason="Запуск прерван явной остановкой materialization серии.",
        )
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
