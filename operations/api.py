from __future__ import annotations

import datetime as dtmod

from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponseForbidden
from django.utils import timezone
from ninja import NinjaAPI, Schema, Status
from ninja.security import django_auth

from operations import schedule_writes as schedule_write_svc
from operations.models import (
    ACTIVE_APPOINTMENT_STATUSES,
    Appointment,
    Discount,
    Room,
    Service,
    StaffMember,
    TimeOffRequest,
)
from operations.schedule_validation import (
    appointment_validation_children,
    appointment_validation_conflicts,
    appointment_validation_staff_members,
    staff_unavailability_reason,
)
from operations.services import appointments as appointment_svc

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
    messages: list[str] = []
    conflicts = appointment_validation_conflicts(appointment, starts_at, ends_at)

    if conflicts.get("child") and conflicts["child"].exists():
        child = conflicts.get("child_target")
        messages.append(
            f"получатель уже занят в это время: {child}"
            if child
            else "получатель уже занят в это время"
        )
    if conflicts.get("staff") and conflicts["staff"].exists():
        staff = conflicts.get("staff_target")
        messages.append(
            f"специалист уже занят в это время: {staff}"
            if staff
            else "специалист уже занят в это время"
        )

    for staff in appointment_validation_staff_members(appointment):
        unavailable = staff_unavailability_reason(staff, starts_at, ends_at)
        if unavailable:
            messages.append(f"недоступность специалиста {staff}: {unavailable}")

    if appointment.room_id and conflicts.get("room_over_limit"):
        reasons = conflicts.get("room_limit_reasons") or {}
        if reasons.get("staff"):
            messages.append("кабинет уже занят по лимиту специалистов")
        if reasons.get("recipients"):
            messages.append("кабинет уже занят по лимиту получателей")
        if reasons.get("group"):
            messages.append("кабинет не разрешает групповые занятия")

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

    try:
        with transaction.atomic():
            expected_series_id, locked_series = (
                appointment_svc.lock_series_root_for_appointment(appointment.pk)
            )
            with schedule_write_svc.lock_schedule_write(
                appointment_id=appointment.pk,
                room_ids=[appointment.room_id],
            ) as locked:
                appointment = locked.appointment
                if appointment is None:  # Defensive: appointment_id is present above.
                    return Status(404, ErrorOut(detail="Appointment not found"))
                appointment_svc.require_locked_series_projection(
                    appointment,
                    expected_series_id=expected_series_id,
                    locked_series=locked_series,
                    action="перенести занятие",
                )
                appointment_svc.require_open_appointment(
                    appointment,
                    action="перенести занятие",
                )
                schedule_write_svc.ensure_room_capacity(
                    starts_at=new_start,
                    ends_at=new_end,
                    children=appointment_validation_children(appointment),
                    staff_members=appointment_validation_staff_members(appointment),
                    room=locked.room_for(appointment.room_id),
                    status=appointment.status,
                    exclude_pk=appointment.pk,
                )
                appointment.starts_at = new_start
                appointment.ends_at = new_end
                appointment.staff_availability_override = False
                appointment.staff_availability_override_reason = ""
                appointment.save(validate_schedule=True, sync_legacy=False)
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
    except appointment_svc.AppointmentStateConflict as exc:
        return Status(400, ErrorOut(detail=str(exc)))
    except ValidationError as exc:
        return Status(400, ErrorOut(detail="; ".join(exc.messages)))
    except Exception:
        return Status(
            400,
            ErrorOut(
                detail="Не удалось перенести занятие. Обновите календарь и повторите действие."
            ),
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
