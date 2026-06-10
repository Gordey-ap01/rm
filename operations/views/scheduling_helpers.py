"""Помощник поиска свободных слотов (используется в appointments.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from operations.forms import (
    appointment_conflicts,
    build_local_datetime,
    conflict_messages,
    staff_unavailability_reason,
)
from operations.models import StaffMember


def suggested_transfer_slots(appointment, days: int = 7, limit: int = 12) -> list[dict]:
    """Возвращает список вариантов переноса занятия с учётом конфликтов и графика."""
    start_day = max(timezone.localdate(), timezone.localtime(appointment.starts_at).date())
    duration = appointment.duration_minutes
    staff_members = StaffMember.objects.filter(status=StaffMember.Status.ACTIVE).order_by("full_name")
    slots: list[dict] = []

    for day_offset in range(days):
        day = start_day + timedelta(days=day_offset)
        for staff_member in staff_members:
            for minute in range(9 * 60, (18 * 60) - duration + 1, 30):
                hour, clock_minute = divmod(minute, 60)
                starts_at = build_local_datetime(day, datetime.strptime(f"{hour:02d}:{clock_minute:02d}", "%H:%M").time())
                ends_at = starts_at + timedelta(minutes=duration)
                if starts_at == appointment.starts_at and staff_member == appointment.staff_member:
                    continue
                conflicts = appointment_conflicts(
                    starts_at,
                    ends_at,
                    appointment.child,
                    staff_member,
                    appointment.room,
                    exclude_pk=appointment.pk,
                )
                if conflict_messages(conflicts) or staff_unavailability_reason(staff_member, starts_at, ends_at):
                    continue
                params = {
                    "date": day.isoformat(),
                    "time": f"{hour:02d}:{clock_minute:02d}",
                    "staff_id": staff_member.id,
                }
                if appointment.room_id:
                    params["room_id"] = appointment.room_id
                slots.append(
                    {
                        "date": day,
                        "time": f"{hour:02d}:{clock_minute:02d}",
                        "staff": staff_member,
                        "room": appointment.room,
                        "move_url": f"{reverse('appointment_move', args=[appointment.pk])}?{urlencode(params)}",
                    }
                )
                if len(slots) >= limit:
                    return slots
    return slots
