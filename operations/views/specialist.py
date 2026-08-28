"""Кабинет специалиста: расписание, отметки, графики, заявки на отпуск."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseForbidden, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from operations.forms import (
    StaffAvailabilityForm,
    TimeOffDecisionForm,
    TimeOffRequestForm,
)
from operations.models import (
    Appointment,
    AppointmentStaffAssignment,
    StaffAvailability,
    StaffMember,
    TimeOffRequest,
)
from operations.services import appointments as appointment_svc, time_off_decisions as time_off_svc

from ._common import is_admin_user, safe_next_url


@dataclass(frozen=True)
class LegacyStaffAssignmentDisplay:
    appointment: Appointment
    staff_member: StaffMember
    starts_at_snapshot: datetime
    ends_at_snapshot: datetime
    role: str = AppointmentStaffAssignment.Role.PRIMARY

    def get_role_display(self) -> str:
        return AppointmentStaffAssignment.Role.PRIMARY.label


def specialist_action_staff(request):
    if is_admin_user(request.user) and request.POST.get("staff_id"):
        return get_object_or_404(StaffMember, pk=request.POST["staff_id"])
    return getattr(request.user, "staff_profile", None)


def specialist_home_redirect(request, staff):
    url = reverse("specialist_home")
    if is_admin_user(request.user) and staff:
        return f"{url}?{urlencode({'staff_id': staff.pk})}"
    return url


def has_mobile_access(request, staff) -> bool:
    return is_admin_user(request.user) or bool(staff and staff.can_use_mobile)


def deny_mobile_access():
    return HttpResponseForbidden("Доступ к мобильному кабинету специалиста отключен.")


def specialist_week_summary_items(
    *,
    schedule_assignments: list[AppointmentStaffAssignment | LegacyStaffAssignmentDisplay],
    today,
    marked_count: int,
    pending_time_off_count: int,
) -> list[dict[str, str]]:
    today_count = sum(
        1
        for assignment in schedule_assignments
        if timezone.localtime(assignment.starts_at_snapshot).date() == today
    )
    group_count = sum(
        1
        for assignment in schedule_assignments
        if assignment.appointment.session_type == Appointment.SessionType.GROUP
    )
    return [
        {
            "label": "Сегодня",
            "value": str(today_count),
            "hint": "занятий в текущем дне",
        },
        {
            "label": "Неделя",
            "value": str(len(schedule_assignments)),
            "hint": "назначений специалиста",
        },
        {
            "label": "Отмечено",
            "value": str(marked_count),
            "hint": "занятий с фактом посещения",
        },
        {
            "label": "Группы",
            "value": str(group_count),
            "hint": "групповых занятий",
        },
        {
            "label": "Заявки",
            "value": str(pending_time_off_count),
            "hint": "ожидают итогового решения",
        },
    ]


def _assignment_needs_marking(
    assignment: AppointmentStaffAssignment | LegacyStaffAssignmentDisplay,
    *,
    now,
) -> bool:
    appointment = assignment.appointment
    if assignment.ends_at_snapshot >= now:
        return False
    if appointment.status not in [Appointment.Status.CONFIRMED, Appointment.Status.PROPOSED]:
        return False
    participants = list(appointment.participants.all())
    if participants:
        return any(
            participant.attendance_status == Appointment.AttendanceStatus.UNKNOWN
            for participant in participants
        )
    return appointment.attendance_status == Appointment.AttendanceStatus.UNKNOWN


def specialist_next_action(
    *,
    schedule_assignments: list[AppointmentStaffAssignment | LegacyStaffAssignmentDisplay],
    today,
    now,
) -> dict[str, str]:
    for assignment in schedule_assignments:
        if _assignment_needs_marking(assignment, now=now):
            appointment = assignment.appointment
            return {
                "tone": "warning",
                "label": "Следующее действие",
                "title": "Отметить прошедшее занятие",
                "detail": (
                    f"{timezone.localtime(assignment.starts_at_snapshot):%d.%m %H:%M} · "
                    f"{appointment.service}"
                ),
                "href": f"#appointment-{appointment.pk}",
            }

    for assignment in schedule_assignments:
        if timezone.localtime(assignment.starts_at_snapshot).date() == today:
            appointment = assignment.appointment
            return {
                "tone": "info",
                "label": "Следующее действие",
                "title": "Ближайшее занятие сегодня",
                "detail": f"{timezone.localtime(assignment.starts_at_snapshot):%H:%M} · {appointment.service}",
                "href": f"#appointment-{appointment.pk}",
            }

    for assignment in schedule_assignments:
        if assignment.starts_at_snapshot >= now:
            appointment = assignment.appointment
            return {
                "tone": "info",
                "label": "Следующее действие",
                "title": "Следующее занятие в расписании",
                "detail": (
                    f"{timezone.localtime(assignment.starts_at_snapshot):%d.%m %H:%M} · "
                    f"{appointment.service}"
                ),
                "href": f"#appointment-{appointment.pk}",
            }

    return {
        "tone": "success",
        "label": "Следующее действие",
        "title": "Критичных задач нет",
        "detail": "Можно проверить график или отправить заявку на отгул.",
        "href": "#staff-availability",
    }


@login_required
def specialist_home(request):
    staff = getattr(request.user, "staff_profile", None)
    staff_members = StaffMember.objects.filter(status=StaffMember.Status.ACTIVE).order_by(
        "full_name"
    )
    if is_admin_user(request.user):
        if request.GET.get("staff_id"):
            staff = get_object_or_404(StaffMember, pk=request.GET["staff_id"])
        elif not staff:
            staff = staff_members.first()
    if not staff:
        messages.error(request, "У пользователя нет профиля специалиста.")
        return redirect("dashboard")
    if not has_mobile_access(request, staff):
        return deny_mobile_access()

    today = timezone.localdate()
    week_end = today + timedelta(days=7)
    tz = timezone.get_current_timezone()
    week_start_dt = timezone.make_aware(datetime.combine(today, time.min), tz)
    week_end_dt = timezone.make_aware(datetime.combine(week_end, time.min), tz)
    assignments = list(
        AppointmentStaffAssignment.objects.filter(
            staff_member=staff,
            starts_at_snapshot__gte=week_start_dt,
            starts_at_snapshot__lt=week_end_dt,
        )
        .select_related(
            "appointment",
            "appointment__child",
            "appointment__service",
            "appointment__room",
            "staff_member",
        )
        .prefetch_related(
            "appointment__participants__child", "appointment__staff_assignments__staff_member"
        )
        .order_by("starts_at_snapshot")
    )
    legacy_assignments = [
        LegacyStaffAssignmentDisplay(
            appointment=appointment,
            staff_member=staff,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
        )
        for appointment in Appointment.objects.filter(
            staff_member=staff,
            starts_at__gte=week_start_dt,
            starts_at__lt=week_end_dt,
        )
        .exclude(staff_assignments__staff_member=staff)
        .select_related("child", "service", "room", "staff_member")
        .prefetch_related("participants__child", "staff_assignments__staff_member")
        .distinct()
    ]
    schedule_assignments = sorted(
        [*assignments, *legacy_assignments],
        key=lambda assignment: (assignment.starts_at_snapshot, assignment.appointment.pk),
    )
    appointments = [assignment.appointment for assignment in schedule_assignments]
    appointments_by_day = defaultdict(list)
    assignments_by_day = defaultdict(list)
    for assignment in schedule_assignments:
        day = timezone.localtime(assignment.starts_at_snapshot).date()
        appointments_by_day[day].append(assignment.appointment)
        assignments_by_day[day].append(assignment)

    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    week_days = [
        {
            "date": today + timedelta(days=offset),
            "label": day_names[(today + timedelta(days=offset)).weekday()],
            "appointments": appointments_by_day.get(today + timedelta(days=offset), []),
            "assignments": assignments_by_day.get(today + timedelta(days=offset), []),
        }
        for offset in range(7)
    ]
    summary = sum(
        1
        for assignment in schedule_assignments
        if assignment.appointment.status == Appointment.Status.COMPLETED
        or any(
            participant.attendance_status == Appointment.AttendanceStatus.ATTENDED
            for participant in assignment.appointment.participants.all()
        )
    )
    availability_windows = staff.availability_windows.order_by("weekday", "starts_at")
    time_off_requests = time_off_svc.decorate_rows(
        time_off_svc.with_current_decision(
            staff.time_off_requests.select_related("decided_by").order_by("-created_at")[:10]
        ),
        actor=request.user,
    )
    pending_time_off_count = sum(
        1
        for item in time_off_requests
        if item.status == TimeOffRequest.Status.PENDING
        or item.awaits_director_review
    )
    return render(
        request,
        "operations/specialist_home.html",
        {
            "staff": staff,
            "appointments": appointments,
            "staff_members": staff_members,
            "can_manage_specialists": is_admin_user(request.user),
            "week_days": week_days,
            "summary": summary,
            "specialist_summary_items": specialist_week_summary_items(
                schedule_assignments=schedule_assignments,
                today=today,
                marked_count=summary,
                pending_time_off_count=pending_time_off_count,
            ),
            "specialist_next_action": specialist_next_action(
                schedule_assignments=schedule_assignments,
                today=today,
                now=timezone.now(),
            ),
            "today": today,
            "week_end": week_end,
            "availability_windows": availability_windows,
            "time_off_requests": time_off_requests,
            "availability_form": StaffAvailabilityForm(),
            "time_off_form": TimeOffRequestForm(initial={"starts_on": today, "ends_on": today}),
        },
    )


@login_required
def mark_appointment(request, pk: int):
    appointment = get_object_or_404(Appointment, pk=pk)
    staff = getattr(request.user, "staff_profile", None)
    if is_admin_user(request.user) and request.POST.get("staff_id"):
        staff = get_object_or_404(StaffMember, pk=request.POST["staff_id"])
    if not has_mobile_access(request, staff):
        return deny_mobile_access()
    is_assigned = bool(staff) and (
        appointment.staff_member_id == staff.pk
        or AppointmentStaffAssignment.objects.filter(
            appointment=appointment, staff_member=staff
        ).exists()
    )
    if not is_admin_user(request.user) and not is_assigned:
        messages.error(request, "Нет доступа к этому занятию.")
        return redirect("specialist_home")

    if request.method == "POST":
        action = request.POST.get("action")
        note = request.POST.get("specialist_note", "").strip()
        if action not in {"completed", "not_completed"}:
            messages.error(request, "Неизвестное действие отметки.")
            return redirect(specialist_home_redirect(request, staff))

        valid_attendance_statuses = set(Appointment.AttendanceStatus.values)
        posted_statuses = {}
        for key, value in request.POST.items():
            if not key.startswith("participant_status_") or value not in valid_attendance_statuses:
                continue
            try:
                participant_id = int(key.removeprefix("participant_status_"))
            except ValueError:
                continue
            posted_statuses[participant_id] = value
        try:
            appointment_svc.record_attendance(
                appointment,
                action=action,
                actor=request.user,
                note=note,
                participant_statuses=posted_statuses,
            )
        except appointment_svc.AppointmentStateConflict as exc:
            messages.error(request, str(exc))
            return redirect(specialist_home_redirect(request, staff))
        except ValueError:
            messages.error(request, "Отметка не сохранена: состав занятия изменился.")
            return redirect(specialist_home_redirect(request, staff))
        messages.success(
            request,
            "Отметка специалиста сохранена. Решение по списанию остается за администратором.",
        )
    return redirect(specialist_home_redirect(request, staff))


@login_required
def staff_availability_create(request):
    staff = specialist_action_staff(request)
    if not staff:
        messages.error(request, "У пользователя нет профиля специалиста.")
        return redirect("specialist_home")
    if not has_mobile_access(request, staff):
        return deny_mobile_access()

    if request.method == "POST":
        form = StaffAvailabilityForm(request.POST)
        if form.is_valid():
            availability = form.save(commit=False)
            availability.staff_member = staff
            availability.save()
            messages.success(request, "Рабочее окно добавлено.")
        else:
            messages.error(request, "Рабочее окно не сохранено. Проверьте время.")
    return redirect(specialist_home_redirect(request, staff))


@login_required
def staff_availability_toggle(request, pk: int):
    availability = get_object_or_404(
        StaffAvailability.objects.select_related("staff_member"), pk=pk
    )
    staff = getattr(request.user, "staff_profile", None)
    if not is_admin_user(request.user) and availability.staff_member != staff:
        messages.error(request, "Нет доступа к этому графику.")
        return redirect("specialist_home")
    if not has_mobile_access(request, availability.staff_member):
        return deny_mobile_access()
    if request.method == "POST":
        availability.is_active = not availability.is_active
        availability.save(update_fields=["is_active", "updated_at"])
        messages.success(request, "Рабочее окно обновлено.")
    return redirect(specialist_home_redirect(request, availability.staff_member))


@login_required
def time_off_request_create(request):
    staff = specialist_action_staff(request)
    if not staff:
        messages.error(request, "У пользователя нет профиля специалиста.")
        return redirect("specialist_home")
    if not has_mobile_access(request, staff):
        return deny_mobile_access()

    if request.method == "POST":
        form = TimeOffRequestForm(request.POST)
        if form.is_valid():
            time_off = form.save(commit=False)
            time_off.staff_member = staff
            time_off.save()
            messages.success(request, "Заявка отправлена администратору.")
        else:
            messages.error(request, "Заявка не сохранена. Проверьте даты.")
    return redirect(specialist_home_redirect(request, staff))


@login_required
@user_passes_test(is_admin_user)
def time_off_request_decide(request, pk: int):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    time_off = get_object_or_404(
        TimeOffRequest.objects.select_related("staff_member", "decided_by"),
        pk=pk,
    )
    fallback = reverse("work_queue")
    form = TimeOffDecisionForm(
        {
            "action": request.POST.get("action", ""),
            "reason": request.POST.get("reason")
            or request.POST.get("admin_note", ""),
        }
    )
    if not form.is_valid():
        messages.error(request, "Укажите решение и основание не короче 5 символов.")
        return redirect(safe_next_url(request, fallback))

    try:
        record = time_off_svc.resolve_manually(
            time_off,
            action=form.cleaned_data["action"],
            reason=form.cleaned_data["reason"],
            actor=request.user,
        )
    except (PermissionDenied, ValueError) as exc:
        messages.error(request, str(exc))
    else:
        if record.awaits_director_review:
            messages.warning(
                request,
                "Решение действует. Заявка оставлена на контроль руководителя.",
            )
        else:
            messages.success(
                request,
                f"{record.get_decision_display()}: {record.get_source_display()}.",
            )
    return redirect(safe_next_url(request, fallback))
