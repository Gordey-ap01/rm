"""Помощник поиска свободных слотов (используется в appointments.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlencode

from django.urls import reverse
from django.utils import timezone

from operations.models import Appointment, StaffMember
from operations.schedule_validation import (
    appointment_group_conflicts,
    build_local_datetime,
    conflict_messages,
    staff_unavailability_reason,
)


def _appointment_children(appointment: Appointment):
    participants = list(appointment.participants.select_related("child").order_by("pk"))
    if participants:
        children = [participant.child for participant in participants]
        if appointment.child_id and all(child.pk != appointment.child_id for child in children):
            children.insert(0, appointment.child)
        return children
    return [appointment.child]


def _appointment_staff_for_move(appointment: Appointment, selected_staff: StaffMember):
    assignments = list(appointment.staff_assignments.select_related("staff_member").order_by("pk"))
    if not assignments:
        return [selected_staff]
    members = []
    seen = set()
    primary_replaced = False
    for assignment in assignments:
        staff = assignment.staff_member
        if assignment.role == assignment.Role.PRIMARY and not primary_replaced:
            staff = selected_staff
            primary_replaced = True
        if staff.pk not in seen:
            members.append(staff)
            seen.add(staff.pk)
    if selected_staff.pk not in seen:
        members.insert(0, selected_staff)
    return members


def _all_staff_available(staff_members, starts_at, ends_at) -> bool:
    return not any(
        staff_unavailability_reason(staff_member, starts_at, ends_at)
        for staff_member in staff_members
    )


def suggested_transfer_slots(appointment, days: int = 7, limit: int = 12) -> list[dict]:
    """Возвращает список вариантов переноса занятия с учётом конфликтов и графика."""
    start_day = max(timezone.localdate(), timezone.localtime(appointment.starts_at).date())
    duration = appointment.duration_minutes
    staff_members = StaffMember.objects.filter(status=StaffMember.Status.ACTIVE).order_by(
        "full_name"
    )
    children = _appointment_children(appointment)
    slots: list[dict] = []

    for day_offset in range(days):
        day = start_day + timedelta(days=day_offset)
        for staff_member in staff_members:
            for minute in range(9 * 60, (18 * 60) - duration + 1, 30):
                hour, clock_minute = divmod(minute, 60)
                starts_at = build_local_datetime(
                    day, datetime.strptime(f"{hour:02d}:{clock_minute:02d}", "%H:%M").time()
                )
                ends_at = starts_at + timedelta(minutes=duration)
                if starts_at == appointment.starts_at:
                    continue
                move_staff_members = _appointment_staff_for_move(appointment, staff_member)
                conflicts = appointment_group_conflicts(
                    starts_at,
                    ends_at,
                    children,
                    move_staff_members,
                    appointment.room,
                    exclude_pk=appointment.pk,
                )
                if conflict_messages(conflicts) or not _all_staff_available(
                    move_staff_members, starts_at, ends_at
                ):
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


def suggested_shift_candidates(appointment, days: int = 7, limit: int = 8) -> list[dict]:
    """Возвращает занятые окна, которые администратор может освободить вручную."""
    start_day = max(timezone.localdate(), timezone.localtime(appointment.starts_at).date())
    duration = appointment.duration_minutes
    staff_members = StaffMember.objects.filter(status=StaffMember.Status.ACTIVE).order_by(
        "full_name"
    )
    children = _appointment_children(appointment)
    candidates: list[dict] = []

    for day_offset in range(days):
        day = start_day + timedelta(days=day_offset)
        for staff_member in staff_members:
            for minute in range(9 * 60, (18 * 60) - duration + 1, 30):
                hour, clock_minute = divmod(minute, 60)
                starts_at = build_local_datetime(
                    day, datetime.strptime(f"{hour:02d}:{clock_minute:02d}", "%H:%M").time()
                )
                ends_at = starts_at + timedelta(minutes=duration)
                if starts_at == appointment.starts_at:
                    continue
                move_staff_members = _appointment_staff_for_move(appointment, staff_member)
                if not _all_staff_available(move_staff_members, starts_at, ends_at):
                    continue

                conflicts = appointment_group_conflicts(
                    starts_at,
                    ends_at,
                    children,
                    move_staff_members,
                    appointment.room,
                    exclude_pk=appointment.pk,
                )
                messages = conflict_messages(conflicts)
                if not messages:
                    continue

                conflict_ids: set[int] = set()
                for key in ("child", "staff", "room"):
                    conflict_qs = conflicts.get(key)
                    if conflict_qs is not None:
                        conflict_ids.update(conflict_qs.values_list("pk", flat=True))
                conflict_appointments = list(
                    Appointment.objects.filter(pk__in=conflict_ids)
                    .select_related("child", "staff_member", "service", "room")
                    .order_by("starts_at", "pk")[:3]
                )
                if not conflict_appointments:
                    continue

                params = {
                    "date": day.isoformat(),
                    "time": f"{hour:02d}:{clock_minute:02d}",
                    "staff_id": staff_member.id,
                }
                if appointment.room_id:
                    params["room_id"] = appointment.room_id
                candidates.append(
                    {
                        "date": day,
                        "time": f"{hour:02d}:{clock_minute:02d}",
                        "staff": staff_member,
                        "room": appointment.room,
                        "messages": messages,
                        "conflicts": conflict_appointments,
                        "move_url": f"{reverse('appointment_move', args=[appointment.pk])}?{urlencode(params)}",
                    }
                )
                if len(candidates) >= limit:
                    return candidates
    return candidates
