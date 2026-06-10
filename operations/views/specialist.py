"""Кабинет специалиста: расписание, отметки, графики, заявки на отпуск."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from operations.forms import StaffAvailabilityForm, TimeOffRequestForm
from operations.models import Appointment, StaffAvailability, StaffMember, TimeOffRequest


def specialist_action_staff(request):
    if request.user.is_staff and request.POST.get("staff_id"):
        return get_object_or_404(StaffMember, pk=request.POST["staff_id"])
    return getattr(request.user, "staff_profile", None)


def specialist_home_redirect(request, staff):
    url = reverse("specialist_home")
    if request.user.is_staff and staff:
        return f"{url}?{urlencode({'staff_id': staff.pk})}"
    return url


@login_required
def specialist_home(request):
    staff = getattr(request.user, "staff_profile", None)
    staff_members = StaffMember.objects.filter(status=StaffMember.Status.ACTIVE).order_by("full_name")
    if request.user.is_staff:
        if request.GET.get("staff_id"):
            staff = get_object_or_404(StaffMember, pk=request.GET["staff_id"])
        elif not staff:
            staff = staff_members.first()
    if not staff:
        messages.error(request, "У пользователя нет профиля специалиста.")
        return redirect("dashboard")

    today = timezone.localdate()
    week_end = today + timedelta(days=7)
    appointments = (
        Appointment.objects.filter(staff_member=staff, starts_at__date__gte=today, starts_at__date__lt=week_end)
        .select_related("child", "service", "room")
        .order_by("starts_at")
    )
    appointments_by_day = defaultdict(list)
    for appointment in appointments:
        appointments_by_day[timezone.localtime(appointment.starts_at).date()].append(appointment)

    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    week_days = [
        {
            "date": today + timedelta(days=offset),
            "label": day_names[(today + timedelta(days=offset)).weekday()],
            "appointments": appointments_by_day.get(today + timedelta(days=offset), []),
        }
        for offset in range(7)
    ]
    summary = appointments.filter(Q(status=Appointment.Status.COMPLETED) | Q(attendance_status=Appointment.AttendanceStatus.ATTENDED)).count()
    availability_windows = staff.availability_windows.order_by("weekday", "starts_at")
    time_off_requests = staff.time_off_requests.order_by("-created_at")[:10]
    return render(
        request,
        "operations/specialist_home.html",
        {
            "staff": staff,
            "appointments": appointments,
            "staff_members": staff_members,
            "week_days": week_days,
            "summary": summary,
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
    if not request.user.is_staff and appointment.staff_member != staff:
        messages.error(request, "Нет доступа к этому занятию.")
        return redirect("specialist_home")

    if request.method == "POST":
        action = request.POST.get("action")
        note = request.POST.get("specialist_note", "").strip()
        if action == "completed":
            appointment.status = Appointment.Status.COMPLETED
            appointment.attendance_status = Appointment.AttendanceStatus.ATTENDED
            appointment.specialist_marked_at = timezone.now()
        elif action == "not_completed":
            appointment.status = Appointment.Status.NO_SHOW
            appointment.attendance_status = Appointment.AttendanceStatus.MISSED
            appointment.specialist_marked_at = timezone.now()
        if note:
            appointment.specialist_note = note
        appointment.save(update_fields=["status", "attendance_status", "specialist_marked_at", "specialist_note", "updated_at"])
        messages.success(request, "Отметка специалиста сохранена. Решение по списанию остается за администратором.")
    return redirect("specialist_home")


@login_required
def staff_availability_create(request):
    staff = specialist_action_staff(request)
    if not staff:
        messages.error(request, "У пользователя нет профиля специалиста.")
        return redirect("specialist_home")

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
    availability = get_object_or_404(StaffAvailability.objects.select_related("staff_member"), pk=pk)
    staff = getattr(request.user, "staff_profile", None)
    if not request.user.is_staff and availability.staff_member != staff:
        messages.error(request, "Нет доступа к этому графику.")
        return redirect("specialist_home")
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
def time_off_request_decide(request, pk: int):
    time_off = get_object_or_404(TimeOffRequest.objects.select_related("staff_member"), pk=pk)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "approve":
            time_off.status = TimeOffRequest.Status.APPROVED
            messages.success(request, "Заявка согласована.")
        elif action == "reject":
            time_off.status = TimeOffRequest.Status.REJECTED
            messages.success(request, "Заявка отклонена.")
        time_off.admin_note = request.POST.get("admin_note", "").strip()
        time_off.decided_by = request.user
        time_off.decided_at = timezone.now()
        time_off.save(update_fields=["status", "admin_note", "decided_by", "decided_at", "updated_at"])
    return redirect("work_queue")
