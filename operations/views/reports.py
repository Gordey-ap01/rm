"""Табель специалиста, грант-отчёт, массовый перенос."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from operations.forms import GrantReportFilterForm, TimeSheetFilterForm
from operations.models import StaffMember
from operations.services import reports as reports_svc, scheduling as sched_svc

from ._common import is_admin_user


@login_required
@user_passes_test(is_admin_user)
def staff_timesheet(request, pk: int):
    staff = get_object_or_404(StaffMember, pk=pk)
    today = timezone.localdate()
    default_from = today - timedelta(days=14)
    default_to = today
    initial = {
        "date_from": request.GET.get("date_from") or default_from,
        "date_to": request.GET.get("date_to") or default_to,
    }
    form = TimeSheetFilterForm(request.GET or None, staff=staff, initial=initial)
    sheet = None
    if form.is_valid():
        try:
            sheet = reports_svc.timesheet(staff, form.cleaned_data["date_from"], form.cleaned_data["date_to"])
        except ValueError as exc:
            messages.error(request, str(exc))

    if "csv" in request.GET and sheet is not None:
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="timesheet_{staff.pk}.csv"'
        response.write("\ufeff")  # BOM for Excel
        response.write("Дата;Всего;Проведено;Отменено;Не явился;Часов\n")
        for row in sheet.rows:
            response.write(
                f"{row.date.isoformat()};{row.total};{row.completed};{row.cancelled};{row.no_show};{row.hours}\n"
            )
        response.write(
            f"Итого;;{sheet.totals.total};{sheet.totals.completed};"
            f"{sheet.totals.cancelled};{sheet.totals.no_show};{sheet.totals.hours}\n"
        )
        return response

    return render(
        request,
        "operations/staff_timesheet.html",
        {"form": form, "sheet": sheet, "staff": staff},
    )


@login_required
@user_passes_test(is_admin_user)
def grant_report(request, pk: int | None = None):
    form = GrantReportFilterForm(request.GET or None)
    report = None
    if form.is_valid():
        try:
            report = reports_svc.grant_report(
                form.cleaned_data["funding"],
                form.cleaned_data["date_from"],
                form.cleaned_data["date_to"],
            )
        except ValueError as exc:
            messages.error(request, str(exc))

    if "csv" in request.GET and report is not None:
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="grant_{report.funding.pk}.csv"'
        response.write("\ufeff")
        response.write("Счёт;Начальный;Пополнения;Списания;Текущий;Занятий\n")
        for row in report.rows:
            response.write(
                f"{row.account};{row.initial_amount};{row.topups};{row.charges};"
                f"{row.current_balance};{row.appointments_count}\n"
            )
        return response

    return render(
        request,
        "operations/grant_report.html",
        {"form": form, "report": report, "funding_id": pk},
    )


@login_required
@user_passes_test(is_admin_user)
def staff_mass_reschedule(request, pk: int):
    staff = get_object_or_404(StaffMember, pk=pk)
    today = timezone.localdate()
    initial = {
        "date_from": request.GET.get("date_from") or today,
        "date_to": request.GET.get("date_to") or today + timedelta(days=14),
    }
    if request.method == "POST":
        date_from = datetime.fromisoformat(request.POST["date_from"]).date()
        date_to = datetime.fromisoformat(request.POST["date_to"]).date()
        reason = request.POST.get("reason", "").strip()
        if not reason:
            messages.error(request, "Укажите причину массовой отмены.")
        else:
            try:
                result = sched_svc.mass_reschedule(
                    staff, date_from=date_from, date_to=date_to,
                    reason=reason, actor=request.user,
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    f"Отменено {len(result.cancelled)} занятий, "
                    f"создано {len(result.confirmations)} уведомлений.",
                )
                return redirect("specialist_home")
    return render(
        request,
        "operations/staff_mass_reschedule.html",
        {"staff": staff, "initial": initial, "cancel_url": reverse("work_queue")},
    )
