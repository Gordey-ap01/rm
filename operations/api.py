from __future__ import annotations

import datetime as dtmod

from django.utils import timezone
from ninja import NinjaAPI, Schema
from ninja.security import django_auth

from operations.models import (
    ACTIVE_APPOINTMENT_STATUSES,
    Appointment,
    Discount,
    Room,
    Service,
    StaffMember,
    TimeOffRequest,
)

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


@api.get("/appointments/", response=list[AppointmentEventOut])
def list_appointments(request, start: str = "", end: str = ""):
    qs = Appointment.objects.filter(status__in=ACTIVE_APPOINTMENT_STATUSES).select_related(
        "child", "staff_member", "service", "room"
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
        staff_color = a.staff_member.color if a.staff_member and a.staff_member.color else "#3b82f6"
        status_color = status_colors.get(a.status, "#3b82f6")
        results.append(
            AppointmentEventOut(
                id=a.pk,
                title=f"{a.child.full_name} / {a.service.name}",
                start=local_start.isoformat(),
                end=local_end.isoformat(),
                backgroundColor="#ffffff",
                borderColor=staff_color,
                textColor="#1f2937",
                extendedProps={
                    "status": a.status,
                    "statusColor": status_color,
                    "child": a.child.full_name,
                    "service": a.service.name,
                    "staff": a.staff_member.full_name if a.staff_member else "",
                    "staffId": a.staff_member_id,
                    "staffColor": staff_color,
                    "room": a.room.name if a.room else "",
                    "roomId": a.room_id,
                    "childId": a.child_id,
                    "serviceId": a.service_id,
                },
            )
        )
    return results


@api.patch("/appointments/{pk}/move/", response={200: dict, 400: ErrorOut, 404: ErrorOut})
def move_appointment(request, pk: int, payload: AppointmentMoveIn):
    try:
        appointment = Appointment.objects.get(pk=pk)
    except Appointment.DoesNotExist:
        return 404, ErrorOut(detail="Appointment not found")

    try:
        new_start = dtmod.datetime.fromisoformat(payload.starts_at)
        new_end = dtmod.datetime.fromisoformat(payload.ends_at)
    except ValueError as e:
        return 400, ErrorOut(detail=f"Invalid date format: {e}")

    if new_end <= new_start:
        return 400, ErrorOut(detail="ends_at must be after starts_at")

    appointment.starts_at = new_start
    appointment.ends_at = new_end
    try:
        appointment.save(validate_schedule=True)
    except Exception as e:
        return 400, ErrorOut(detail=str(e))
    return {"ok": True}


@api.get("/staff/", response=list[StaffOut])
def list_staff(request):
    return [
        StaffOut(id=s.pk, full_name=s.full_name, color=s.color)
        for s in StaffMember.objects.filter(status=StaffMember.Status.ACTIVE).order_by("full_name")
    ]


@api.get("/rooms/", response=list[RoomOut])
def list_rooms(request):
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
    return [{"id": s.pk, "name": s.name, "color": s.color} for s in Service.objects.filter(is_active=True).order_by("name")]


@api.get("/discounts/", response=list[dict])
def list_discounts(request):
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
