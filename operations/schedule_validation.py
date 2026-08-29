"""Shared schedule validation helpers for forms, views, and services."""

from __future__ import annotations

from datetime import datetime, time

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

    windows = list(
        StaffAvailability.objects.filter(
            staff_member=staff_member,
            weekday=day.weekday(),
            is_active=True,
        ).order_by("starts_at")
    )

    start_time = local_start.time().replace(second=0, microsecond=0)
    end_time = local_end.time().replace(second=0, microsecond=0)
    if not windows:
        if time(9, 0) <= start_time and end_time <= time(18, 0):
            return ""
        return "время вне базового рабочего окна 09:00-18:00"

    if any(window.starts_at <= start_time and end_time <= window.ends_at for window in windows):
        return ""
    return "время вне рабочего графика специалиста"


def build_local_datetime(day, clock):
    value = datetime.combine(day, clock)
    return timezone.make_aware(value, timezone.get_current_timezone())
