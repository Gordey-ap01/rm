"""Shared schedule validation helpers for forms, views, and services."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from operations.models import (
    ACTIVE_APPOINTMENT_STATUSES,
    Appointment,
    AppointmentParticipant,
    AppointmentStaffAssignment,
    StaffAvailability,
    TimeOffRequest,
    room_usage_counts,
)

_APPOINTMENT_ROOM = object()


def appointment_group_conflicts(
    starts_at,
    ends_at,
    children,
    staff_members,
    room=None,
    exclude_pk=None,
    exclude_pks=None,
):
    children = list(children or [])
    staff_members = list(staff_members or [])
    excluded_ids = {int(pk) for pk in (exclude_pks or []) if pk}
    if exclude_pk:
        excluded_ids.add(int(exclude_pk))
    qs = Appointment.objects.filter(
        status__in=ACTIVE_APPOINTMENT_STATUSES,
        starts_at__lt=ends_at,
        ends_at__gt=starts_at,
    )
    if excluded_ids:
        qs = qs.exclude(pk__in=excluded_ids)

    conflicts = {}
    if children:
        participant_qs = AppointmentParticipant.objects.filter(
            appointment_status__in=ACTIVE_APPOINTMENT_STATUSES,
            child__in=children,
            starts_at_snapshot__lt=ends_at,
            ends_at_snapshot__gt=starts_at,
        ).select_related("appointment", "child")
        if excluded_ids:
            participant_qs = participant_qs.exclude(appointment_id__in=excluded_ids)
        participant_conflict = participant_qs.first()
        legacy_child_conflict = qs.filter(child__in=children).select_related("child").first()
        appointment_ids = list(participant_qs.values_list("appointment_id", flat=True))
        conflicts["child"] = Appointment.objects.filter(pk__in=appointment_ids) | qs.filter(
            child__in=children
        )
        conflicts["child_target"] = (
            participant_conflict.child
            if participant_conflict
            else legacy_child_conflict.child
            if legacy_child_conflict
            else None
        )
    if staff_members:
        assignment_qs = AppointmentStaffAssignment.objects.filter(
            appointment_status__in=ACTIVE_APPOINTMENT_STATUSES,
            staff_member__in=staff_members,
            starts_at_snapshot__lt=ends_at,
            ends_at_snapshot__gt=starts_at,
        ).select_related("appointment", "staff_member")
        if excluded_ids:
            assignment_qs = assignment_qs.exclude(appointment_id__in=excluded_ids)
        assignment_conflict = assignment_qs.first()
        legacy_staff_conflict = (
            qs.filter(staff_member__in=staff_members).select_related("staff_member").first()
        )
        appointment_ids = list(assignment_qs.values_list("appointment_id", flat=True))
        conflicts["staff"] = Appointment.objects.filter(pk__in=appointment_ids) | qs.filter(
            staff_member__in=staff_members
        )
        conflicts["staff_target"] = (
            assignment_conflict.staff_member
            if assignment_conflict
            else legacy_staff_conflict.staff_member
            if legacy_staff_conflict
            else None
        )
    if room:
        room_qs = qs.filter(room=room)
        staff_count, recipient_count = room_usage_counts(room_qs)
        staff_total = staff_count + len({staff.pk for staff in staff_members})
        recipient_total = recipient_count + len({child.pk for child in children})
        staff_over_limit = room.limit_staff_count and staff_total > room.effective_max_staff_count
        recipient_over_limit = (
            room.limit_recipient_count and recipient_total > room.effective_max_recipient_count
        )
        group_not_allowed = len(children) > 1 and not room.allow_group_sessions
        if staff_over_limit or recipient_over_limit:
            conflicts["room"] = room_qs
        else:
            conflicts["room"] = qs.none()
        conflicts["room_over_limit"] = bool(
            staff_over_limit or recipient_over_limit or group_not_allowed
        )
        conflicts["room_limit_reasons"] = {
            "staff": staff_over_limit,
            "recipients": recipient_over_limit,
            "group": group_not_allowed,
            "staff_total": staff_total,
            "recipient_total": recipient_total,
        }
    return conflicts


def appointment_validation_children(appointment: Appointment):
    if appointment.pk:
        participant_rows = list(
            appointment.participants.select_related("child").order_by("pk")
        )
        children = [
            participant.child
            for participant in participant_rows
            if participant.appointment_status in ACTIVE_APPOINTMENT_STATUSES
        ]
        if participant_rows:
            return children
    return [appointment.child] if appointment.child_id else []


def appointment_validation_staff_members(appointment: Appointment):
    if appointment.pk:
        staff_members = [
            assignment.staff_member
            for assignment in appointment.staff_assignments.select_related("staff_member").order_by(
                "pk"
            )
        ]
        if staff_members:
            return staff_members
    return [appointment.staff_member] if appointment.staff_member_id else []


def appointment_validation_conflicts(
    appointment: Appointment,
    starts_at,
    ends_at,
    *,
    room=_APPOINTMENT_ROOM,
):
    validation_room = appointment.room if room is _APPOINTMENT_ROOM else room
    return appointment_group_conflicts(
        starts_at,
        ends_at,
        appointment_validation_children(appointment),
        appointment_validation_staff_members(appointment),
        validation_room,
        exclude_pk=appointment.pk,
    )


def appointment_conflicts(starts_at, ends_at, child, staff_member, room=None, exclude_pk=None):
    children = [child] if child else []
    staff_members = [staff_member] if staff_member else []
    return appointment_group_conflicts(
        starts_at, ends_at, children, staff_members, room=room, exclude_pk=exclude_pk
    )


def conflict_messages(conflicts):
    messages = []
    if conflicts.get("child") and conflicts["child"].exists():
        messages.append("у получателя уже есть занятие в это время")
    if conflicts.get("staff") and conflicts["staff"].exists():
        messages.append("специалист уже занят в это время")
    if conflicts.get("room_over_limit") or (conflicts.get("room") and conflicts["room"].exists()):
        messages.append("кабинет превышает правила вместимости")
    return messages


def staff_unavailability_reason(staff_member, starts_at, ends_at):
    if not staff_member or not starts_at or not ends_at:
        return ""

    local_start = timezone.localtime(starts_at)
    local_end = timezone.localtime(ends_at)
    day = local_start.date()
    if local_end.date() != day:
        return "занятие должно помещаться в один рабочий день"

    if TimeOffRequest.objects.filter(
        staff_member=staff_member,
        status=TimeOffRequest.Status.APPROVED,
        starts_on__lte=day,
        ends_on__gte=day,
    ).exists():
        return "у специалиста согласован отпуск/отгул на эту дату"

    start_time = local_start.time().replace(second=0, microsecond=0)
    end_time = local_end.time().replace(second=0, microsecond=0)
    windows, _, uses_legacy_fallback = staff_working_windows(staff_member, day)
    if any(window_start <= start_time and end_time <= window_end for window_start, window_end in windows):
        return ""
    if uses_legacy_fallback:
        return "время вне базового рабочего окна 09:00-18:00"
    return "время вне рабочего графика специалиста"


def staff_working_windows(
    staff_member, day: date
) -> tuple[list[tuple[time, time]], bool, bool]:
    """Return windows, whether they are legacy, and whether they use the 09:00–18:00 fallback.

    ``effective_windows`` is deliberately imported here.  The schedule-change
    service also depends on the domain models used by this module, so a lazy
    import avoids coupling the validation module to service import order.
    """
    from operations.services.staff_schedules import effective_windows

    revision_windows = effective_windows(staff_member, day)
    if revision_windows is not None:
        return list(revision_windows), False, False

    legacy_windows = list(
        StaffAvailability.objects.filter(
            staff_member=staff_member,
            weekday=day.weekday(),
            is_active=True,
        )
        .order_by("starts_at")
        .values_list("starts_at", "ends_at")
    )
    if legacy_windows:
        return legacy_windows, True, False
    return [(time(9, 0), time(18, 0))], True, True


def _intersect_working_windows(
    window_sets: Iterable[Iterable[tuple[time, time]]],
) -> list[tuple[time, time]]:
    """Return shared same-day intervals for every assigned staff member."""
    common: list[tuple[time, time]] | None = None
    for windows in window_sets:
        current = list(windows)
        if common is None:
            common = current
            continue
        common = [
            (max(left_start, right_start), min(left_end, right_end))
            for left_start, left_end in common
            for right_start, right_end in current
            if max(left_start, right_start) < min(left_end, right_end)
        ]
    return common or []


def iter_available_slot_times(
    day: date,
    duration_minutes: int,
    *,
    staff_members: Iterable | None = None,
    slot_step_minutes: int = 30,
    start_hour: int | None = None,
    end_hour: int | None = None,
) -> Iterator[tuple[datetime, datetime]]:
    """Yield starts and ends inside every shared staff window on ``day``.

    An omitted bound lets an approved schedule extend the historical 09:00–18:00
    search range.  Explicit bounds still constrain the result.  With no staff
    members, and for the legacy fallback, the historical 09:00–18:00 range is
    retained.
    """
    if duration_minutes <= 0:
        raise ValueError("Длительность занятия должна быть положительной.")
    if slot_step_minutes <= 0:
        raise ValueError("Шаг поиска слотов должен быть положительным.")

    members = [staff for staff in (staff_members or []) if staff]
    if members:
        resolved_windows = [staff_working_windows(staff, day) for staff in members]
        windows = _intersect_working_windows(item[0] for item in resolved_windows)
        all_legacy = all(item[1] for item in resolved_windows)
    else:
        windows = [(time(9, 0), time(18, 0))]
        all_legacy = True

    lower_bound = time(start_hour if start_hour is not None else 0, 0)
    upper_bound = time(end_hour, 0) if end_hour is not None else time.max
    if all_legacy:
        lower_bound = max(lower_bound, time(9, 0))
        upper_bound = min(upper_bound, time(18, 0))

    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=slot_step_minutes)
    for window_start, window_end in windows:
        bounded_start = max(window_start, lower_bound)
        bounded_end = min(window_end, upper_bound)
        cursor = build_local_datetime(day, bounded_start)
        latest_end = build_local_datetime(day, bounded_end)
        while cursor + duration <= latest_end:
            yield cursor, cursor + duration
            cursor += step


def build_local_datetime(day, clock):
    value = datetime.combine(day, clock)
    return timezone.make_aware(value, timezone.get_current_timezone())
