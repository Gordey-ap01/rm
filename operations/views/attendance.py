"""Operator-facing attendance and schedule decision commands."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render

from operations.forms import ManualAttendanceDecisionForm, ManualScheduleDecisionForm
from operations.models import Appointment
from operations.services import (
    appointments as appointment_svc,
    schedule_decisions as schedule_decisions_svc,
)

from ._common import is_admin_user, safe_next_url
from .appointments import appointment_detail_context


def _detail_error_response(request, appointment, *, attendance_form=None, schedule_form=None):
    return render(
        request,
        "operations/appointment_detail.html",
        appointment_detail_context(
            appointment,
            actor=request.user,
            attendance_form=attendance_form,
            schedule_form=schedule_form,
        ),
        status=400,
    )


@login_required
@user_passes_test(is_admin_user)
def appointment_attendance_decide(request, pk: int):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    appointment = get_object_or_404(Appointment, pk=pk)
    fallback = redirect("appointment_detail", pk=appointment.pk).url
    form = ManualAttendanceDecisionForm(request.POST, appointment=appointment)
    if not form.is_valid():
        messages.error(request, "Проверьте факт занятия, основание и отметки участников.")
        return _detail_error_response(request, appointment, attendance_form=form)

    try:
        appointment_svc.record_attendance(
            appointment,
            action=form.cleaned_data["action"],
            actor=request.user,
            note=form.cleaned_data["note"],
            participant_statuses=form.participant_statuses(),
            reason=form.cleaned_data["reason"],
            operation_key=form.cleaned_data["operation_key"],
        )
    except (PermissionDenied, appointment_svc.AppointmentStateConflict, ValueError) as exc:
        messages.error(request, str(exc))
        return _detail_error_response(request, appointment, attendance_form=form)

    messages.success(request, "Ручная отметка проведения сохранена. Списание и выплата решаются отдельно.")
    return redirect(safe_next_url(request, fallback))


@login_required
@user_passes_test(is_admin_user)
def appointment_schedule_decide(request, pk: int):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    appointment = get_object_or_404(Appointment, pk=pk)
    fallback = redirect("appointment_detail", pk=appointment.pk).url
    form = ManualScheduleDecisionForm(request.POST, appointment=appointment)
    if not form.is_valid():
        messages.error(request, "Выберите назначенного специалиста, решение и основание.")
        return _detail_error_response(request, appointment, schedule_form=form)

    try:
        record = schedule_decisions_svc.resolve_manually(
            appointment,
            staff_member=form.cleaned_data["staff_member"],
            action=form.cleaned_data["action"],
            reason=form.cleaned_data["reason"],
            actor=request.user,
            operation_key=form.cleaned_data["operation_key"],
            expected_schedule=form.cleaned_data["expected_schedule"],
        )
    except (PermissionDenied, appointment_svc.AppointmentStateConflict, ValueError) as exc:
        messages.error(request, str(exc))
        return _detail_error_response(request, appointment, schedule_form=form)

    messages.success(request, f"{record.get_decision_display()}: ручное решение по расписанию сохранено.")
    return redirect(safe_next_url(request, fallback))
