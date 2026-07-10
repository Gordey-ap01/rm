from __future__ import annotations

import datetime as dtmod

from django.http import HttpResponseForbidden
from django.utils import timezone
from ninja import NinjaAPI, Schema, Status
from ninja.security import django_auth

from operations.models import (
    ACTIVE_APPOINTMENT_STATUSES,
    Appointment,
    AppointmentParticipant,
    AppointmentStaffAssignment,
    Discount,
    Room,
    Service,
    StaffMember,
    TimeOffRequest,
    room_usage_counts,
)
from operations.services.scheduling import is_within_availability

api = NinjaAPI(auth=django_auth, urls_namespace="api", version="1.0.0")


class AppointmentEventOut(Schema):
    id: int
    title: str
    start: str
    end: str
    backgroundColor: str
    borderColor: str
    textColor: str
    extendedProps: dict


class AppointmentMoveIn(Schema):
    starts_at: str
    ends_at: str


class StaffOut(Schema):
    id: int
    full_name: str
    color: str


class RoomOut(Schema):
    id: int
    name: str
    color: str


class ErrorOut(Schema):
    detail: str


def admin_api_forbidden(request):
    if request.user.is_staff:
        return None
    return HttpResponseForbidden("Доступ к API календаря разрешен только администраторам.")


def move_conflict_messages(appointment: Appointment, starts_at, ends_at) -> list[str]:
    actual_participant_ids = set(appointment.participants.values_list("child_id", flat=True))
    participant_ids = set(actual_participant_ids)
    if appointment.child_id:
        participant_ids.add(appointment.child_id)
    participant_ids.discard(None)
    actual_participant_ids.discard(None)

    staff_assignments = list(appointment.staff_assignments.select_related("staff_member"))
    actual_staff_ids = {assignment.staff_member_id for assignment in staff_assignments}
    staff_ids = set(actual_staff_ids)
    if appointment.staff_member_id:
        staff_ids.add(appointment.staff_member_id)
    staff_ids.discard(None)
    actual_staff_ids.discard(None)

    messages: list[str] = []
    if participant_ids:
        participant_conflict = (
            AppointmentParticipant.objects.filter(
                appointment_status__in=ACTIVE_APPOINTMENT_STATUSES,
                child_id__in=participant_ids,
                starts_at_snapshot__lt=ends_at,
                ends_at_snapshot__gt=starts_at,
            )
            .exclude(appointment_id=appointment.pk)
            .select_related("child", "appointment")
            .first()
        )
        legacy_child_conflict = (
            Appointment.objects.filter(
                status__in=ACTIVE_APPOINTMENT_STATUSES,
                child_id__in=participant_ids,
                starts_at__lt=ends_at,
                ends_at__gt=starts_at,
            )
            .exclude(pk=appointment.pk)
            .select_related("child")
            .first()
        )
        if participant_conflict or legacy_child_conflict:
            child = (
                participant_conflict.child
                if participant_conflict
                else legacy_child_conflict.child
            )
            messages.append(f"получатель уже занят в это время: {child}")

    if staff_ids:
        staff_conflict = (
            AppointmentStaffAssignment.objects.filter(
                appointment_status__in=ACTIVE_APPOINTMENT_STATUSES,
                staff_member_id__in=staff_ids,
                starts_at_snapshot__lt=ends_at,
                ends_at_snapshot__gt=starts_at,
            )
            .exclude(appointment_id=appointment.pk)
            .select_related("staff_member", "appointment")
            .first()
        )
        legacy_staff_conflict = (
            Appointment.objects.filter(
                status__in=ACTIVE_APPOINTMENT_STATUSES,
                staff_member_id__in=staff_ids,
                starts_at__lt=ends_at,
                ends_at__gt=starts_at,
            )
            .exclude(pk=appointment.pk)
            .select_related("staff_member")
            .first()
        )
        if staff_conflict or legacy_staff_conflict:
            staff = (
                staff_conflict.staff_member
                if staff_conflict
                else legacy_staff_conflict.staff_member
            )
            messages.append(f"специалист уже занят в это время: {staff}")

        checked_staff_ids = set()
        if staff_assignments:
            for assignment in staff_assignments:
                checked_staff_ids.add(assignment.staff_member_id)
                unavailable = is_within_availability(
                    assignment.staff_member,
                    starts_at,
                    ends_at,
                )
                if unavailable:
                    messages.append(f"недоступность специалиста {assignment.staff_member}: {unavailable}")
        if (
            appointment.staff_member_id
            and appointment.staff_member_id not in checked_staff_ids
        ):
            unavailable = is_within_availability(
                appointment.staff_member,
                starts_at,
                ends_at,
            )
            if unavailable:
                messages.append(f"недоступность специалиста {appointment.staff_member}: {unavailable}")

    if appointment.room_id:
        room_qs = Appointment.objects.filter(
            status__in=ACTIVE_APPOINTMENT_STATUSES,
            room_id=appointment.room_id,
            starts_at__lt=ends_at,
            ends_at__gt=starts_at,
        ).exclude(pk=appointment.pk)
        room_staff_occupancy, room_recipient_occupancy = room_usage_counts(room_qs)

        room = appointment.room
        incoming_staff = len(actual_staff_ids) or (1 if appointment.staff_member_id else 0)
        incoming_recipients = len(actual_participant_ids) or (1 if appointment.child_id else 0)
        if (
            room.limit_staff_count
            and room_staff_occupancy + incoming_staff > room.effective_max_staff_count
        ):
            messages.append("кабинет уже занят по лимиту специалистов")
        if (
            room.limit_recipient_count
            and room_recipient_occupancy + incoming_recipients > room.effective_max_recipient_count
        ):
            messages.append("кабинет уже занят по лимиту получателей")

    return messages


@api.get("/appointments/", response=list[AppointmentEventOut])
def list_appointments(request, start: str = "", end: str = ""):
    forbidden = admin_api_forbidden(request)
    if forbidden:
        return forbidden

    qs = Appointment.objects.filter(status__in=ACTIVE_APPOINTMENT_STATUSES).select_related(
        "child", "staff_member", "service", "room", "billing_account", "program_block", "program_block__program"
    ).prefetch_related(
        "participants__child",
        "participants__billing_account",
        "staff_assignments__staff_member",
    )
    if start:
        qs = qs.filter(ends_at__gte=start)
    if end:
        qs = qs.filter(starts_at__lte=end)
    results = []
    for a in qs:
        local_start = timezone.localtime(a.starts_at)
        local_end = timezone.localtime(a.ends_at)
        status_colors = {
            "confirmed": "#16a34a",
            "proposed": "#ea580c",
            "completed": "#6b7280",
            "reserved": "#a855f7",
            "draft": "#9ca3af",
            "cancelled": "#ef4444",
            "no_show": "#dc2626",
            "rescheduled": "#f59e0b",
        }
        program_color = a.program_block.color if a.program_block_id and a.program_block.color else ""
        status_color = status_colors.get(a.status, "#3b82f6")
        participants = list(a.participants.all())
        staff_assignments = list(a.staff_assignments.all())
        billing_account = None
        if len(participants) == 1:
            billing_account = participants[0].billing_account
        elif not participants:
            billing_account = a.billing_account
        account_color = billing_account.color if billing_account and billing_account.color else ""
        primary_child = participants[0].child if participants else a.child
        primary_staff = staff_assignments[0].staff_member if staff_assignments else a.staff_member
        staff_color = primary_staff.color if primary_staff and primary_staff.color else "#3b82f6"
        child_color = primary_child.color if primary_child and primary_child.color else "#00a443"
        participant_names = [participant.child.full_name for participant in participants]
        if not participant_names and a.child_id:
            participant_names = [a.child.full_name]
        staff_names = [assignment.staff_member.full_name for assignment in staff_assignments]
        staff_ids = [assignment.staff_member_id for assignment in staff_assignments]
        if not staff_names and a.staff_member_id:
            staff_names = [a.staff_member.full_name]
            staff_ids = [a.staff_member_id]
        participant_count = len(participant_names)
        staff_count = len(staff_names)
        event_child_id = primary_child.pk if participant_count == 1 and primary_child else a.child_id
        event_staff_id = primary_staff.pk if staff_count == 1 and primary_staff else a.staff_member_id
        child_label = ", ".join(participant_names)
        staff_label = ", ".join(staff_names)
        is_group_event = a.session_type == Appointment.SessionType.GROUP or participant_count > 1
        if is_group_event:
            group_label = a.title.strip() if a.title else f"Группа ({participant_count})"
            event_title = f"{group_label} / {a.service.name}"
        else:
            event_title = f"{child_label or a.child.full_name} / {a.service.name}"
        results.append(
            AppointmentEventOut(
                id=a.pk,
                title=event_title,
                start=local_start.isoformat(),
                end=local_end.isoformat(),
                backgroundColor="#ffffff",
                borderColor=staff_color,
                textColor="#1f2937",
                extendedProps={
                    "status": a.status,
                    "statusColor": status_color,
                    "sessionType": a.session_type,
                    "child": child_label or a.child.full_name,
                    "participants": participant_names,
                    "participantCount": participant_count,
                    "service": a.service.name,
                    "staff": staff_label,
                    "staffMembers": staff_names,
                    "staffCount": staff_count,
                    "staffId": event_staff_id,
                    "staffIds": staff_ids,
                    "staffColor": staff_color,
                    "childColor": child_color,
                    "accountColor": account_color,
                    "programColor": program_color,
                    "room": a.room.name if a.room else "",
                    "roomId": a.room_id,
                    "childId": event_child_id,
                    "serviceId": a.service_id,
                    "programBlock": str(a.program_block) if a.program_block_id else "",
                    "sequenceNumber": a.sequence_number,
                    "billingAccountId": billing_account.id if billing_account else None,
                },
            )
        )
    return results


@api.patch("/appointments/{pk}/move/", response={200: dict, 400: ErrorOut, 404: ErrorOut})
def move_appointment(request, pk: int, payload: AppointmentMoveIn):
    forbidden = admin_api_forbidden(request)
    if forbidden:
        return forbidden

    try:
        appointment = Appointment.objects.select_related("room", "staff_member").get(pk=pk)
    except Appointment.DoesNotExist:
        return Status(404, ErrorOut(detail="Appointment not found"))

    try:
        new_start = dtmod.datetime.fromisoformat(payload.starts_at)
        new_end = dtmod.datetime.fromisoformat(payload.ends_at)
    except ValueError as e:
        return Status(400, ErrorOut(detail=f"Invalid date format: {e}"))

    if new_end <= new_start:
        return Status(400, ErrorOut(detail="ends_at must be after starts_at"))

    conflict_messages = move_conflict_messages(appointment, new_start, new_end)
    if conflict_messages:
        return Status(
            400,
            ErrorOut(detail="Конфликт расписания: " + "; ".join(conflict_messages) + "."),
        )

    appointment.starts_at = new_start
    appointment.ends_at = new_end
    appointment.staff_availability_override = False
    appointment.staff_availability_override_reason = ""
    try:
        appointment.save(validate_schedule=True, sync_legacy=False)
    except Exception as e:
        return Status(400, ErrorOut(detail=str(e)))
    now = timezone.now()
    appointment.participants.update(
        starts_at_snapshot=appointment.starts_at,
        ends_at_snapshot=appointment.ends_at,
        appointment_status=appointment.status,
        updated_at=now,
    )
    appointment.staff_assignments.update(
        starts_at_snapshot=appointment.starts_at,
        ends_at_snapshot=appointment.ends_at,
        appointment_status=appointment.status,
        override_availability=False,
        override_reason="",
        updated_at=now,
    )
    return {"ok": True}


@api.get("/staff/", response=list[StaffOut])
def list_staff(request):
    forbidden = admin_api_forbidden(request)
    if forbidden:
        return forbidden

    return [
        StaffOut(id=s.pk, full_name=s.full_name, color=s.color)
        for s in StaffMember.objects.filter(status=StaffMember.Status.ACTIVE).order_by("full_name")
    ]


@api.get("/rooms/", response=list[RoomOut])
def list_rooms(request):
    forbidden = admin_api_forbidden(request)
    if forbidden:
        return forbidden

    return [RoomOut(id=r.pk, name=r.name, color=r.color) for r in Room.objects.order_by("name")]


class UnavailableSlotOut(Schema):
    id: str
    title: str
    start: str
    end: str
    display: str = "background"
    backgroundColor: str = "#fecaca"
    classNames: list[str] = ["fc-non-business"]


@api.get("/unavailability/", response=list[UnavailableSlotOut])
def list_unavailability(request, start: str = "", end: str = ""):
    forbidden = admin_api_forbidden(request)
    if forbidden:
        return forbidden

    from django.utils import timezone as tz_utils

    results: list[UnavailableSlotOut] = []
    tz = tz_utils.get_current_timezone()
    start_date = dtmod.datetime.fromisoformat(start).date() if start else tz_utils.localdate()
    end_date = dtmod.datetime.fromisoformat(end).date() if end else start_date + dtmod.timedelta(days=30)

    time_offs = TimeOffRequest.objects.filter(
        status=TimeOffRequest.Status.APPROVED,
        starts_on__lte=end_date,
        ends_on__gte=start_date,
    ).select_related("staff_member")
    for to in time_offs:
        label = to.get_request_type_display()
        slot_start = dtmod.datetime.combine(max(to.starts_on, start_date), dtmod.time.min, tz)
        slot_end = dtmod.datetime.combine(min(to.ends_on, end_date), dtmod.time.max, tz)
        results.append(
            UnavailableSlotOut(
                id=f"to-{to.pk}",
                title=f"{to.staff_member.full_name}: {label}",
                start=slot_start.isoformat(),
                end=slot_end.isoformat(),
            )
        )
    return results


@api.get("/services/", response=list[dict])
def list_services(request):
    forbidden = admin_api_forbidden(request)
    if forbidden:
        return forbidden

    return [{"id": s.pk, "name": s.name, "color": s.color} for s in Service.objects.filter(is_active=True).order_by("name")]


@api.get("/discounts/", response=list[dict])
def list_discounts(request):
    forbidden = admin_api_forbidden(request)
    if forbidden:
        return forbidden

    return [
        {
            "id": d.pk,
            "childId": d.child_id,
            "percentage": str(d.percentage),
            "serviceId": d.service_id,
            "isActive": d.is_active,
        }
        for d in Discount.objects.filter(is_active=True)
    ]


@api.get("/certificates/", response=list[dict])
def list_certificates(request):
    forbidden = admin_api_forbidden(request)
    if forbidden:
        return forbidden

    from operations.models import Certificate
    return [
        {
            "id": c.pk,
            "childId": c.child_id,
            "certificateType": c.certificate_type,
            "number": c.number,
            "totalAmount": str(c.total_amount),
            "remainingAmount": str(c.remaining_amount),
        }
        for c in Certificate.objects.all()
    ]
