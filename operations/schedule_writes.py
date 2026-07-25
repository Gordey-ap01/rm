"""Transactional guards for schedule write paths.

Room capacity is configured per room and cannot be expressed by a static
PostgreSQL exclusion constraint. Writers therefore serialize on the affected
room rows and re-check capacity while the lock is held.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction

from operations.models import ACTIVE_APPOINTMENT_STATUSES, Appointment, Room
from operations.schedule_validation import appointment_group_conflicts


@dataclass(frozen=True)
class ScheduleWriteLock:
    """Rows locked for one schedule write transaction."""

    appointment: Appointment | None
    rooms_by_id: dict[int, Room]

    def room_for(self, room_id: int | None) -> Room | None:
        return self.rooms_by_id.get(room_id) if room_id else None


def room_limit_message(room: Room, conflicts: dict[str, Any]) -> str:
    reasons = conflicts.get("room_limit_reasons") or {}
    parts = []
    if reasons.get("staff"):
        parts.append(
            f"специалистов {reasons.get('staff_total')} "
            f"при лимите {room.effective_max_staff_count}"
        )
    if reasons.get("recipients"):
        parts.append(
            f"получателей {reasons.get('recipient_total')} "
            f"при лимите {room.effective_max_recipient_count}"
        )
    if reasons.get("group"):
        parts.append("кабинет не отмечен как разрешенный для групповых занятий")
    return "; ".join(parts) or "кабинет превышает правила вместимости"


@contextmanager
def lock_schedule_write(
    *,
    appointment_id: int | None = None,
    room_ids: Iterable[int | None] = (),
) -> Iterator[ScheduleWriteLock]:
    """Lock an existing appointment and all affected rooms in stable order.

    `select_for_update()` is intentionally scoped to Room rows: it serializes
    configurable room capacity without unnecessarily blocking writes to other
    rooms. The parent appointment lock prevents competing moves of one record.
    """

    with transaction.atomic():
        appointment = None
        if appointment_id:
            # ``room`` is optional.  Loading it in this locking query creates a
            # LEFT JOIN, which PostgreSQL cannot lock on its nullable side.
            # The foreign-key value is already present on ``Appointment``.
            appointment = Appointment.objects.select_for_update().get(pk=appointment_id)

        ids = {int(room_id) for room_id in room_ids if room_id}
        if appointment and appointment.room_id:
            ids.add(appointment.room_id)
        rooms = Room.objects.select_for_update().filter(pk__in=ids).order_by("pk")
        rooms_by_id = {room.pk: room for room in rooms}
        yield ScheduleWriteLock(appointment=appointment, rooms_by_id=rooms_by_id)


def ensure_room_capacity(
    *,
    starts_at,
    ends_at,
    children: Iterable[Any],
    staff_members: Iterable[Any],
    room: Room | None,
    status: str,
    exclude_pk: int | None = None,
    allow_override: bool = False,
) -> dict[str, Any]:
    """Re-check room capacity after `lock_schedule_write()` acquired the lock."""

    if not room or status not in ACTIVE_APPOINTMENT_STATUSES:
        return {}

    conflicts = appointment_group_conflicts(
        starts_at,
        ends_at,
        children,
        staff_members,
        room,
        exclude_pk=exclude_pk,
    )
    if conflicts.get("room_over_limit") and not allow_override:
        raise ValidationError(
            "Ограничение кабинета: " + room_limit_message(room, conflicts) + "."
        )
    return conflicts
