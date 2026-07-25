"""Табель специалиста, грант-отчёт, массовый перенос."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from operations.forms import (
    FundingServiceQuotaQuickForm,
    FundingStaffAllocationQuickForm,
    GrantRecipientAllocationQuickForm,
    GrantReportFilterForm,
    PayrollPayoutRecordForm,
    PayrollSheetSendForm,
    TimeSheetFilterForm,
)
from operations.models import (
    Appointment,
    FundingServiceQuota,
    FundingStaffAllocation,
    GrantRecipientAllocation,
    PayrollAccrual,
    PayrollSheet,
    StaffMember,
)
from operations.services import (
    payroll as payroll_svc,
    reports as reports_svc,
    rescheduling_plans as plan_svc,
    scheduling as sched_svc,
)

from ._common import is_admin_user, is_director


def _grant_report_url(
    funding_source_id: int,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> str:
    today = timezone.localdate()
    query = urlencode(
        {
            "funding": funding_source_id,
            "date_from": (date_from or today.replace(month=1, day=1)).isoformat(),
            "date_to": (date_to or today).isoformat(),
        }
    )
    return f"{reverse('grant_report')}?{query}"


def _timesheet_date_label(value: date | None) -> str:
    if value is None:
        return "не выбрана"
    return value.strftime("%d.%m.%Y")


def _timesheet_period_label(date_from: date | None, date_to: date | None) -> str:
    if date_from and date_to:
        return f"{_timesheet_date_label(date_from)} - {_timesheet_date_label(date_to)}"
    return "Период не выбран"


def _timesheet_summary_items(
    sheet: reports_svc.Timesheet | None,
    *,
    selected_date_from: date | None,
    selected_date_to: date | None,
    payroll_accruals: list[PayrollAccrual],
    payroll_sheets: list[PayrollSheet],
) -> list[dict[str, str]]:
    period = _timesheet_period_label(selected_date_from, selected_date_to)
    if sheet is None:
        return [
            {
                "label": "Период",
                "value": period,
                "hint": "Выберите даты и нажмите «Показать».",
            },
            {
                "label": "Состояние",
                "value": "Не построен",
                "hint": "Табель появится после корректного периода.",
            },
        ]

    saved_amount = sum((accrual.amount for accrual in payroll_accruals), Decimal("0"))
    sheet_count = len(payroll_sheets)
    return [
        {
            "label": "Период",
            "value": period,
            "hint": f"Дней в табеле: {len(sheet.rows)}.",
        },
        {
            "label": "Занятия",
            "value": str(sheet.totals.total),
            "hint": (
                f"Проведено: {sheet.totals.completed}; отменено: {sheet.totals.cancelled}; "
                f"не явился: {sheet.totals.no_show}."
            ),
        },
        {
            "label": "К начислению",
            "value": str(sheet.totals.payable),
            "hint": f"Часов в расписании: {sheet.totals.hours}.",
        },
        {
            "label": "Сумма по расчету",
            "value": f"{sheet.totals.pay_amount} руб.",
            "hint": "Предварительная сумма по текущим ставкам и грантовым квотам.",
        },
        {
            "label": "Сохранено",
            "value": f"{saved_amount} руб.",
            "hint": f"Начислений в базе за период: {len(payroll_accruals)}.",
        },
        {
            "label": "Расчетные листы",
            "value": str(sheet_count),
            "hint": (
                f"Последний статус: {payroll_sheets[0].get_status_display()}."
                if payroll_sheets
                else "За период еще нет расчетных листов."
            ),
        },
    ]


def _timesheet_attention_items(
    sheet: reports_svc.Timesheet | None,
    *,
    payroll_accruals: list[PayrollAccrual],
    payroll_sheets: list[PayrollSheet],
) -> list[dict[str, str]]:
    if sheet is None:
        return [
            {
                "tone": "info",
                "title": "Табель не построен",
                "detail": "Проверьте период, чтобы увидеть занятия, начисления и расчетные листы.",
            }
        ]

    items: list[dict[str, str]] = []
    payable_without_rate = sum(1 for line in sheet.pay_lines if line.payable and not line.has_rate)
    draft_accruals = sum(
        1 for accrual in payroll_accruals if accrual.status == PayrollAccrual.Status.DRAFT
    )

    if not sheet.pay_lines:
        items.append(
            {
                "tone": "info",
                "title": "Нет занятий за период",
                "detail": "Можно сменить даты или перейти к массовому переносу расписания.",
            }
        )
    if payable_without_rate:
        items.append(
            {
                "tone": "warning",
                "title": "Есть занятия без ставки",
                "detail": (
                    f"{payable_without_rate} оплачиваемых строк не попадут в начисления, "
                    "пока не настроена ставка или грантовая квота."
                ),
            }
        )
    if sheet.totals.payable and not payroll_accruals:
        items.append(
            {
                "tone": "warning",
                "title": "Начисления еще не сохранены",
                "detail": "Сначала обновите начисления, затем создавайте расчетный лист.",
            }
        )
    elif draft_accruals:
        items.append(
            {
                "tone": "info",
                "title": "Есть черновики начислений",
                "detail": f"Черновых начислений за период: {draft_accruals}.",
            }
        )
    if payroll_sheets:
        latest = payroll_sheets[0]
        items.append(
            {
                "tone": "info",
                "title": "Расчетный лист уже создан",
                "detail": (
                    f"Последний лист: {latest.get_status_display()}, "
                    f"{latest.total_amount} руб."
                ),
            }
        )
    if not items:
        items.append(
            {
                "tone": "success",
                "title": "Табель готов к проверке",
                "detail": "Критичных предупреждений по ставкам и начислениям нет.",
            }
        )
    return items


def _payroll_sheet_timesheet_url(payroll_sheet: PayrollSheet) -> str:
    query = urlencode(
        {
            "date_from": payroll_sheet.date_from.isoformat(),
            "date_to": payroll_sheet.date_to.isoformat(),
        }
    )
    return f"{reverse('staff_timesheet', args=[payroll_sheet.staff_member_id])}?{query}"


def _payroll_sheet_lines(payroll_sheet: PayrollSheet) -> list:
    return list(payroll_sheet.lines.all())


def _payroll_sheet_summary_items(payroll_sheet: PayrollSheet) -> list[dict[str, str]]:
    lines = _payroll_sheet_lines(payroll_sheet)
    service_ids = {line.service_id for line in lines if line.service_id}
    funding_source_ids = {
        line.payroll_accrual.funding_source_id
        for line in lines
        if line.payroll_accrual.funding_source_id
    }
    total_minutes = sum(line.duration_minutes for line in lines)
    return [
        {
            "label": "Строк",
            "value": str(len(lines)),
            "hint": "занятий в расчетном листе",
        },
        {
            "label": "Сумма",
            "value": f"{payroll_sheet.total_amount} руб.",
            "hint": "итог к начислению",
        },
        {
            "label": "Минуты",
            "value": str(total_minutes),
            "hint": "общее время занятий",
        },
        {
            "label": "Услуги",
            "value": str(len(service_ids)),
            "hint": "направлений в листе",
        },
        {
            "label": "Источники",
            "value": str(len(funding_source_ids)),
            "hint": "гранты, фонды или личные оплаты",
        },
        {
            "label": "Статус",
            "value": payroll_sheet.get_status_display(),
            "hint": "текущее состояние листа",
        },
    ]


def _payroll_sheet_next_action(
    payroll_sheet: PayrollSheet, *, can_approve: bool
) -> dict[str, str]:
    line_count = len(_payroll_sheet_lines(payroll_sheet))
    if payroll_sheet.status == PayrollSheet.Status.DRAFT:
        if line_count:
            if not can_approve:
                return {
                    "tone": "info",
                    "label": "Следующее действие",
                    "title": "Проверить и передать руководителю",
                    "detail": (
                        "Сверьте услуги, источники и ставки. Лист останется черновиком "
                        "до утверждения руководителем."
                    ),
                    "href": "#payroll-sheet-lines",
                }
            return {
                "tone": "warning",
                "label": "Следующий шаг",
                "title": "Проверить и утвердить",
                "detail": (
                    "Сверьте услуги, источники и ставки. После утверждения строки "
                    "начислений будут зафиксированы."
                ),
                "href": "#payroll-sheet-approve",
            }
        return {
            "tone": "danger",
            "label": "Следующий шаг",
            "title": "Вернуться в табель",
            "detail": "В листе нет строк, поэтому его нельзя считать готовым к выплате.",
            "href": _payroll_sheet_timesheet_url(payroll_sheet),
        }

    if payroll_sheet.status == PayrollSheet.Status.APPROVED:
        if can_approve:
            return {
                "tone": "warning",
                "label": "Следующий шаг",
                "title": "Передать в выплату",
                "detail": "Укажите основание передачи во внутренний контур выплаты.",
                "href": "#payroll-sheet-send",
            }
        approved_at = (
            timezone.localtime(payroll_sheet.approved_at).strftime("%d.%m.%Y %H:%M")
            if payroll_sheet.approved_at
            else "дата не указана"
        )
        approved_by = (
            payroll_sheet.approved_by.get_username()
            if payroll_sheet.approved_by_id
            else "пользователь не указан"
        )
        return {
            "tone": "success",
            "label": "Статус",
            "title": "Лист утвержден",
            "detail": f"Утвердил: {approved_by}; дата: {approved_at}.",
            "href": "#payroll-sheet-lines",
        }

    if payroll_sheet.status == PayrollSheet.Status.SENT:
        if can_approve:
            return {
                "tone": "warning",
                "label": "Следующий шаг",
                "title": "Зафиксировать выплату",
                "detail": "Укажите дату, способ и реквизиты фактической выплаты.",
                "href": "#payroll-sheet-payout",
            }
        return {
            "tone": "info",
            "label": "Статус",
            "title": "Лист передан в выплату",
            "detail": "Ожидается фиксация фактической выплаты руководителем.",
            "href": "#payroll-sheet-history",
        }

    if payroll_sheet.status == PayrollSheet.Status.PAID:
        return {
            "tone": "success",
            "label": "Статус",
            "title": "Выплата отмечена",
            "detail": "Расчетный лист находится в финальном состоянии оплаты.",
            "href": "#payroll-sheet-lines",
        }

    if payroll_sheet.status == PayrollSheet.Status.CANCELLED:
        return {
            "tone": "danger",
            "label": "Статус",
            "title": "Лист отменен",
            "detail": "Проверьте табель и создайте новый лист, если начисления нужны повторно.",
            "href": _payroll_sheet_timesheet_url(payroll_sheet),
        }

    return {
        "tone": "info",
        "label": "Статус",
        "title": payroll_sheet.get_status_display(),
        "detail": "Проверьте строки листа и дальнейшие действия по выплате.",
        "href": "#payroll-sheet-lines",
    }


def _grant_report_summary_items(
    report: reports_svc.GrantReport | None,
) -> list[dict[str, str]]:
    if report is None:
        return [
            {
                "label": "Отчет",
                "value": "Не построен",
                "hint": "Выберите источник финансирования и период.",
            },
            {
                "label": "Контур",
                "value": "Гранты и фонды",
                "hint": "После построения будут видны балансы, квоты и выделения получателям.",
            },
        ]

    quota_planned = sum(row.planned_sessions for row in report.quota_rows)
    quota_allocated = sum(row.allocated_sessions for row in report.quota_rows)
    quota_charged = sum(row.charged_sessions for row in report.quota_rows)
    quota_remaining = sum(row.remaining_sessions for row in report.quota_rows)
    return [
        {
            "label": "Источник",
            "value": report.funding.name,
            "hint": f"{_timesheet_period_label(report.date_from, report.date_to)}.",
        },
        {
            "label": "Текущий остаток",
            "value": str(report.totals.current_balance),
            "hint": (
                f"Начальный: {report.totals.initial_amount}; "
                f"пополнения: {report.totals.topups}; списания: {report.totals.charges}."
            ),
        },
        {
            "label": "Занятия",
            "value": f"{report.totals.completed_count}/{report.totals.planned_count}",
            "hint": f"Факт/план по счетам; всего привязанных занятий: {report.totals.appointments_count}.",
        },
        {
            "label": "Квоты",
            "value": f"{quota_charged}/{quota_planned}",
            "hint": f"Распределено: {quota_allocated}; остаток по квотам: {quota_remaining}.",
        },
        {
            "label": "Получатели",
            "value": str(len(report.recipient_allocation_rows)),
            "hint": "Выделения грантовых занятий конкретным детям.",
        },
        {
            "label": "Льготы",
            "value": f"{len(report.certificates)} / {len(report.discounts)}",
            "hint": "Сертификаты / активные скидки в выбранном источнике.",
        },
    ]


def _grant_report_attention_items(
    report: reports_svc.GrantReport | None,
) -> list[dict[str, str]]:
    if report is None:
        return [
            {
                "tone": "info",
                "title": "Отчет не построен",
                "detail": "Укажите источник финансирования и период, чтобы увидеть контроль освоения.",
            }
        ]

    items: list[dict[str, str]] = []
    negative_accounts = sum(1 for row in report.rows if row.current_balance < 0)
    unallocated_sessions = sum(
        max(row.planned_sessions - row.allocated_sessions, 0)
        for row in report.quota_rows
        if row.quota is not None
    )
    overallocated_sessions = sum(
        max(row.allocated_sessions - row.planned_sessions, 0)
        for row in report.quota_rows
        if row.quota is not None
    )
    exhausted_quotas = sum(
        1
        for row in report.quota_rows
        if row.planned_sessions > 0 and row.remaining_sessions == 0
    )
    overdrawn_recipients = sum(
        1 for row in report.recipient_allocation_rows if row.remaining_sessions < 0
    )

    if negative_accounts:
        items.append(
            {
                "tone": "danger",
                "title": "Есть отрицательные остатки",
                "detail": f"Счетов с отрицательным балансом: {negative_accounts}.",
            }
        )
    if unallocated_sessions:
        items.append(
            {
                "tone": "warning",
                "title": "Нераспределенная квота",
                "detail": f"Не распределено между специалистами: {unallocated_sessions} занятий.",
            }
        )
    if overallocated_sessions:
        items.append(
            {
                "tone": "warning",
                "title": "Распределено сверх плана",
                "detail": f"Сверх плана по квотам распределено: {overallocated_sessions} занятий.",
            }
        )
    if overdrawn_recipients:
        items.append(
            {
                "tone": "danger",
                "title": "Получатели ушли в минус",
                "detail": f"Выделений с отрицательным остатком: {overdrawn_recipients}.",
            }
        )
    if not report.quota_rows:
        items.append(
            {
                "tone": "info",
                "title": "Квоты не заданы",
                "detail": "Можно задать квоту по услуге или сразу распределить занятия специалисту.",
            }
        )
    if not report.recipient_allocation_rows:
        items.append(
            {
                "tone": "info",
                "title": "Нет выделений получателям",
                "detail": "Если грант закрепляется за детьми, создайте выделение получателю.",
            }
        )
    if exhausted_quotas:
        items.append(
            {
                "tone": "info",
                "title": "Есть освоенные квоты",
                "detail": f"Квот с нулевым остатком: {exhausted_quotas}.",
            }
        )
    if not items:
        items.append(
            {
                "tone": "success",
                "title": "Критичных сигналов нет",
                "detail": "Квоты, остатки и выделения выглядят согласованно для выбранного периода.",
            }
        )
    return items


def _grant_service_quota_control_items(
    quota: FundingServiceQuota | None = None,
) -> list[dict[str, str]]:
    items = [
        {
            "title": "План на услугу",
            "detail": "Это общий лимит занятий по источнику финансирования и услуге.",
        },
        {
            "title": "Следующий шаг",
            "detail": "После сохранения распределите занятия между специалистами гранта.",
        },
        {
            "title": "Период",
            "detail": "Даты используются в грант-отчете и контроле освоения квоты.",
        },
    ]
    if quota:
        allocated_count = quota.staff_allocations.count()
        items.insert(
            1,
            {
                "title": "Связанные распределения",
                "detail": f"К этой квоте привязано распределений по специалистам: {allocated_count}.",
            },
        )
    return items


def _grant_staff_allocation_control_items(
    allocation: FundingStaffAllocation | None = None,
) -> list[dict[str, str]]:
    items = [
        {
            "title": "Квота или прямое назначение",
            "detail": "Можно выбрать квоту услуги или сразу указать источник, услугу и специалиста.",
        },
        {
            "title": "Количество специалиста",
            "detail": "Это план занятий конкретного специалиста по гранту или фонду.",
        },
        {
            "title": "Ставка специалисту",
            "detail": "Отдельная ставка применяется в табеле при списании занятия по грантовому источнику.",
        },
        {
            "title": "Смена специалиста",
            "detail": "При смене в проекте задайте новый период или отдельное распределение.",
        },
    ]
    if allocation and allocation.service_quota_id:
        items.insert(
            1,
            {
                "title": "Связь с квотой",
                "detail": "Источник и услуга берутся из выбранной квоты услуги.",
            },
        )
    return items


def _grant_recipient_allocation_control_items(
    allocation: GrantRecipientAllocation | None = None,
) -> list[dict[str, str]]:
    items = [
        {
            "title": "Получатель и услуга",
            "detail": "Грантовые занятия закрепляются за конкретным получателем и направлением.",
        },
        {
            "title": "Счет в занятиях",
            "detail": "Если счет не выбран, система создаст счет баланса в занятиях после сохранения.",
        },
        {
            "title": "Период действия",
            "detail": "Период используется в грант-отчете и контроле остатка выделения.",
        },
    ]
    if allocation and allocation.balance_account_id:
        items.insert(
            2,
            {
                "title": "Связанный счет",
                "detail": "Удаление выделения не удаляет уже созданный счет баланса.",
            },
        )
    return items


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
    form = TimeSheetFilterForm(
        (request.POST if request.method == "POST" else request.GET) or None,
        staff=staff,
        initial=initial,
    )
    sheet = None
    payroll_accruals = []
    payroll_sheets = []
    selected_date_from = None
    selected_date_to = None
    if form.is_valid():
        date_from = form.cleaned_data["date_from"]
        date_to = form.cleaned_data["date_to"]
        selected_date_from = date_from
        selected_date_to = date_to
        if request.method == "POST":
            action = request.POST.get("action")
            try:
                if action == "generate_accruals":
                    result = payroll_svc.generate_accruals_for_staff(
                        staff,
                        date_from=date_from,
                        date_to=date_to,
                        actor=request.user,
                    )
                    messages.success(
                        request,
                        "Начисления обновлены: "
                        f"создано {result.created}, обновлено {result.updated}, "
                        f"без списания {result.skipped_no_charge}, без ставки {result.skipped_no_rule}.",
                    )
                elif action == "create_payroll_sheet":
                    payroll_sheet = payroll_svc.create_payroll_sheet_for_staff(
                        staff,
                        date_from=date_from,
                        date_to=date_to,
                        actor=request.user,
                    )
                    messages.success(request, f"Создан расчетный лист №{payroll_sheet.pk}.")
                    return redirect("payroll_sheet_detail", pk=payroll_sheet.pk)
                else:
                    messages.error(request, "Неизвестное действие с табелем.")
            except ValueError as exc:
                messages.error(request, str(exc))
            query = urlencode({"date_from": date_from.isoformat(), "date_to": date_to.isoformat()})
            return redirect(f"{reverse('staff_timesheet', args=[staff.pk])}?{query}")
        try:
            sheet = reports_svc.timesheet(staff, date_from, date_to)
        except ValueError as exc:
            messages.error(request, str(exc))
        payroll_accruals = list(
            PayrollAccrual.objects.filter(
                staff_member=staff, work_date__gte=date_from, work_date__lte=date_to
            )
            .select_related("service", "funding_source", "appointment")
            .order_by("work_date", "starts_at_snapshot")
        )
        payroll_sheets = list(
            PayrollSheet.objects.filter(
                staff_member=staff, date_from__lte=date_to, date_to__gte=date_from
            ).order_by("-date_to", "-created_at")
        )

    if "csv" in request.GET and sheet is not None:
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="timesheet_{staff.pk}.csv"'
        response.write("\ufeff")  # BOM for Excel
        response.write(
            "Дата;Всего;Проведено;Отменено;Не явился;Часов;К начислению;Сумма начисления\n"
        )
        for row in sheet.rows:
            response.write(
                f"{row.date.isoformat()};{row.total};{row.completed};{row.cancelled};{row.no_show};"
                f"{row.hours};{row.payable};{row.pay_amount}\n"
            )
        response.write(
            f"Итого;{sheet.totals.total};{sheet.totals.completed};"
            f"{sheet.totals.cancelled};{sheet.totals.no_show};{sheet.totals.hours};"
            f"{sheet.totals.payable};{sheet.totals.pay_amount}\n"
        )
        return response

    return render(
        request,
        "operations/staff_timesheet.html",
        {
            "form": form,
            "sheet": sheet,
            "staff": staff,
            "payroll_accruals": payroll_accruals,
            "payroll_sheets": payroll_sheets,
            "selected_date_from": selected_date_from,
            "selected_date_to": selected_date_to,
            "timesheet_summary_items": _timesheet_summary_items(
                sheet,
                selected_date_from=selected_date_from,
                selected_date_to=selected_date_to,
                payroll_accruals=payroll_accruals,
                payroll_sheets=payroll_sheets,
            ),
            "timesheet_attention_items": _timesheet_attention_items(
                sheet,
                payroll_accruals=payroll_accruals,
                payroll_sheets=payroll_sheets,
            ),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def payroll_sheet_detail(request, pk: int):
    payroll_sheet = get_object_or_404(
        PayrollSheet.objects.select_related(
            "staff_member", "created_by", "approved_by", "payout"
        ).prefetch_related(
            "lines__service",
            "lines__appointment",
            "lines__payroll_accrual",
            "lines__payroll_accrual__funding_source",
            "lifecycle_events__actor",
        ),
        pk=pk,
    )
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "approve":
                if not is_director(request.user):
                    raise PermissionDenied("Утвердить расчетный лист может только руководитель.")
                payroll_svc.approve_payroll_sheet(payroll_sheet, actor=request.user)
                messages.success(request, "Расчетный лист утвержден.")
            elif action == "send":
                if not is_director(request.user):
                    raise PermissionDenied("Передать расчетный лист в выплату может только руководитель.")
                send_form = PayrollSheetSendForm(request.POST)
                if not send_form.is_valid():
                    messages.error(request, "Укажите основание передачи в выплату не короче 5 символов.")
                else:
                    payroll_svc.send_payroll_sheet(
                        payroll_sheet,
                        note=send_form.cleaned_data["note"],
                        actor=request.user,
                    )
                    messages.success(request, "Расчетный лист передан в выплату.")
            elif action == "record_payout":
                if not is_director(request.user):
                    raise PermissionDenied("Зафиксировать выплату может только руководитель.")
                payout_form = PayrollPayoutRecordForm(request.POST)
                if not payout_form.is_valid():
                    messages.error(request, "Проверьте дату, способ и сумму выплаты.")
                else:
                    payroll_svc.record_payroll_payout(
                        payroll_sheet,
                        amount=payout_form.cleaned_data["amount"],
                        method=payout_form.cleaned_data["method"],
                        paid_at=payout_form.cleaned_data["paid_at"],
                        reference=payout_form.cleaned_data["reference"],
                        note=payout_form.cleaned_data["note"],
                        actor=request.user,
                    )
                    messages.success(request, "Фактическая выплата зафиксирована.")
            else:
                messages.error(request, "Неизвестное действие с расчетным листом.")
        except ValueError as exc:
            messages.error(request, str(exc))
        return redirect("payroll_sheet_detail", pk=payroll_sheet.pk)

    can_approve_payroll_sheet = is_director(request.user)
    payroll_payout = getattr(payroll_sheet, "payout", None)
    return render(
        request,
        "operations/payroll_sheet_detail.html",
        {
            "payroll_sheet": payroll_sheet,
            "payroll_sheet_summary_items": _payroll_sheet_summary_items(payroll_sheet),
            "payroll_sheet_next_action": _payroll_sheet_next_action(
                payroll_sheet,
                can_approve=can_approve_payroll_sheet,
            ),
            "payroll_sheet_timesheet_url": _payroll_sheet_timesheet_url(payroll_sheet),
            "can_approve_payroll_sheet": can_approve_payroll_sheet,
            "payroll_sheet_send_form": PayrollSheetSendForm(),
            "payroll_payout_form": PayrollPayoutRecordForm(
                initial={"amount": payroll_sheet.total_amount}
            ),
            "payroll_payout": payroll_payout,
            "payroll_sheet_lifecycle_events": payroll_sheet.lifecycle_events.all(),
        },
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
        if report.quota_rows:
            response.write("\nКвоты по услугам\n")
            response.write("Услуга;План;Распределено;Факт списано;Остаток\n")
            for row in report.quota_rows:
                response.write(
                    f"{row.service};{row.planned_sessions};{row.allocated_sessions};"
                    f"{row.charged_sessions};{row.remaining_sessions}\n"
                )
                for staff_row in row.staff_rows:
                    response.write(
                        f"  {staff_row.staff_member};{staff_row.allocated_sessions};"
                        f"{staff_row.charged_sessions};{staff_row.remaining_sessions};"
                        f"{staff_row.session_pay_amount or ''}\n"
                    )
        if report.recipient_allocation_rows:
            response.write("\nВыделения получателям\n")
            response.write("Получатель;Услуга;Выделено;Списано;Остаток;Счет\n")
            for row in report.recipient_allocation_rows:
                response.write(
                    f"{row.child};{row.service};{row.allocated_sessions};"
                    f"{row.charged_sessions};{row.remaining_sessions};{row.balance_account}\n"
                )
        return response

    return render(
        request,
        "operations/grant_report.html",
        {
            "form": form,
            "report": report,
            "funding_id": pk,
            "grant_summary_items": _grant_report_summary_items(report),
            "grant_attention_items": _grant_report_attention_items(report),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def funding_service_quota_create(request):
    initial = {
        "funding_source": request.GET.get("funding") or None,
        "service": request.GET.get("service") or None,
    }
    initial = {key: value for key, value in initial.items() if value}
    if request.method == "POST":
        form = FundingServiceQuotaQuickForm(request.POST)
        if form.is_valid():
            quota = form.save()
            messages.success(
                request,
                "Квота по услуге сохранена. Теперь распределите занятия между специалистами.",
            )
            return redirect(f"{reverse('funding_staff_allocation_create')}?quota={quota.pk}")
    else:
        form = FundingServiceQuotaQuickForm(initial=initial)

    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Задать квоту по услуге",
            "subtitle": (
                "Укажите источник финансирования, услугу и общий план занятий. "
                "После сохранения можно распределить план по специалистам."
            ),
            "form_panel_title": "Параметры квоты",
            "form_intro": (
                "Общий план занятий по услуге задает рамку, в пределах которой руководитель "
                "распределяет занятия между специалистами."
            ),
            "form": form,
            "control_title": "Контроль квоты услуги",
            "object_form_control_items": _grant_service_quota_control_items(),
            "cancel_url": reverse("grant_report"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def funding_service_quota_edit(request, pk: int):
    quota = get_object_or_404(
        FundingServiceQuota.objects.select_related("funding_source", "service"),
        pk=pk,
    )
    if request.method == "POST":
        form = FundingServiceQuotaQuickForm(request.POST, instance=quota)
        if form.is_valid():
            quota = form.save()
            messages.success(request, "Квота по услуге обновлена.")
            return redirect(
                _grant_report_url(
                    quota.funding_source_id,
                    date_from=quota.starts_on,
                    date_to=quota.ends_on,
                )
            )
    else:
        form = FundingServiceQuotaQuickForm(instance=quota)

    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Редактировать квоту по услуге",
            "subtitle": str(quota),
            "form_panel_title": "Параметры квоты",
            "form_intro": (
                "Изменение плана влияет на контроль распределений по специалистам, "
                "но не меняет уже списанные занятия."
            ),
            "form": form,
            "control_title": "Контроль квоты услуги",
            "object_form_control_items": _grant_service_quota_control_items(quota),
            "cancel_url": _grant_report_url(
                quota.funding_source_id,
                date_from=quota.starts_on,
                date_to=quota.ends_on,
            ),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def funding_service_quota_delete(request, pk: int):
    quota = get_object_or_404(
        FundingServiceQuota.objects.select_related("funding_source", "service"),
        pk=pk,
    )
    redirect_url = _grant_report_url(
        quota.funding_source_id,
        date_from=quota.starts_on,
        date_to=quota.ends_on,
    )
    if request.method != "POST":
        return redirect(redirect_url)
    allocations_count = quota.staff_allocations.count()
    if allocations_count:
        messages.error(
            request,
            f"Нельзя удалить квоту: к ней привязано распределений по специалистам: {allocations_count}.",
        )
    else:
        quota.delete()
        messages.success(request, "Квота по услуге удалена.")
    return redirect(redirect_url)


@login_required
@user_passes_test(is_admin_user)
def funding_staff_allocation_create(request):
    initial = {
        "service_quota": request.GET.get("quota") or None,
        "funding_source": request.GET.get("funding") or None,
        "service": request.GET.get("service") or None,
        "staff_member": request.GET.get("staff") or None,
    }
    initial = {key: value for key, value in initial.items() if value}
    if request.method == "POST":
        form = FundingStaffAllocationQuickForm(request.POST)
        if form.is_valid():
            allocation = form.save()
            messages.success(request, "Распределение грантовой квоты сохранено.")
            return redirect(
                _grant_report_url(
                    allocation.funding_source_id,
                    date_from=allocation.starts_on,
                    date_to=allocation.ends_on,
                )
            )
    else:
        form = FundingStaffAllocationQuickForm(initial=initial)

    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Распределить грантовую квоту",
            "subtitle": (
                "Укажите специалиста, услугу и количество занятий. Если выбрана квота услуги, "
                "источник и услуга будут взяты из нее."
            ),
            "form_panel_title": "Параметры распределения",
            "form_intro": (
                "Распределение фиксирует, сколько занятий по гранту планируется у конкретного "
                "специалиста и какую ставку использовать в табеле."
            ),
            "form": form,
            "control_title": "Контроль распределения специалисту",
            "object_form_control_items": _grant_staff_allocation_control_items(),
            "cancel_url": reverse("grant_report"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def funding_staff_allocation_edit(request, pk: int):
    allocation = get_object_or_404(
        FundingStaffAllocation.objects.select_related(
            "service_quota", "funding_source", "service", "staff_member"
        ),
        pk=pk,
    )
    if request.method == "POST":
        form = FundingStaffAllocationQuickForm(request.POST, instance=allocation)
        if form.is_valid():
            allocation = form.save()
            messages.success(request, "Распределение грантовой квоты обновлено.")
            return redirect(
                _grant_report_url(
                    allocation.funding_source_id,
                    date_from=allocation.starts_on,
                    date_to=allocation.ends_on,
                )
            )
    else:
        form = FundingStaffAllocationQuickForm(instance=allocation)

    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Редактировать распределение квоты",
            "subtitle": str(allocation),
            "form_panel_title": "Параметры распределения",
            "form_intro": (
                "Изменяйте количество, период или ставку специалиста с учетом уже выполненных "
                "занятий по гранту."
            ),
            "form": form,
            "control_title": "Контроль распределения специалисту",
            "object_form_control_items": _grant_staff_allocation_control_items(allocation),
            "cancel_url": _grant_report_url(
                allocation.funding_source_id,
                date_from=allocation.starts_on,
                date_to=allocation.ends_on,
            ),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def funding_staff_allocation_delete(request, pk: int):
    allocation = get_object_or_404(
        FundingStaffAllocation.objects.select_related("funding_source", "service", "staff_member"),
        pk=pk,
    )
    redirect_url = _grant_report_url(
        allocation.funding_source_id,
        date_from=allocation.starts_on,
        date_to=allocation.ends_on,
    )
    if request.method != "POST":
        return redirect(redirect_url)
    allocation.delete()
    messages.success(request, "Распределение квоты удалено.")
    return redirect(redirect_url)


@login_required
@user_passes_test(is_admin_user)
def grant_recipient_allocation_create(request):
    initial = {
        "funding_source": request.GET.get("funding") or None,
        "service": request.GET.get("service") or None,
        "child": request.GET.get("child") or None,
    }
    initial = {key: value for key, value in initial.items() if value}
    if request.method == "POST":
        form = GrantRecipientAllocationQuickForm(request.POST)
        if form.is_valid():
            allocation = form.save()
            messages.success(request, "Грантовое выделение получателю сохранено.")
            return redirect(
                _grant_report_url(
                    allocation.funding_source_id,
                    date_from=allocation.valid_from,
                    date_to=allocation.valid_until,
                )
            )
    else:
        form = GrantRecipientAllocationQuickForm(initial=initial)

    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Выделить грантовые занятия получателю",
            "subtitle": (
                "Укажите получателя, услугу и количество занятий. Если счет не выбран, "
                "система создаст счет баланса в занятиях."
            ),
            "form_panel_title": "Параметры выделения",
            "form_intro": (
                "Выделение закрепляет часть гранта за получателем и создает основу для списаний "
                "из счета в занятиях."
            ),
            "form": form,
            "control_title": "Контроль выделения получателю",
            "object_form_control_items": _grant_recipient_allocation_control_items(),
            "cancel_url": reverse("grant_report"),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def grant_recipient_allocation_edit(request, pk: int):
    allocation = get_object_or_404(
        GrantRecipientAllocation.objects.select_related(
            "funding_source", "child", "service", "balance_account"
        ),
        pk=pk,
    )
    if request.method == "POST":
        form = GrantRecipientAllocationQuickForm(request.POST, instance=allocation)
        if form.is_valid():
            allocation = form.save()
            messages.success(request, "Грантовое выделение получателю обновлено.")
            return redirect(
                _grant_report_url(
                    allocation.funding_source_id,
                    date_from=allocation.valid_from,
                    date_to=allocation.valid_until,
                )
            )
    else:
        form = GrantRecipientAllocationQuickForm(instance=allocation)

    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Редактировать грантовое выделение получателю",
            "subtitle": str(allocation),
            "form_panel_title": "Параметры выделения",
            "form_intro": (
                "Проверьте получателя, услугу, количество занятий и связанный счет перед "
                "изменением выделения."
            ),
            "form": form,
            "control_title": "Контроль выделения получателю",
            "object_form_control_items": _grant_recipient_allocation_control_items(allocation),
            "cancel_url": _grant_report_url(
                allocation.funding_source_id,
                date_from=allocation.valid_from,
                date_to=allocation.valid_until,
            ),
        },
    )


@login_required
@user_passes_test(is_admin_user)
def grant_recipient_allocation_delete(request, pk: int):
    allocation = get_object_or_404(
        GrantRecipientAllocation.objects.select_related("funding_source"),
        pk=pk,
    )
    redirect_url = _grant_report_url(
        allocation.funding_source_id,
        date_from=allocation.valid_from,
        date_to=allocation.valid_until,
    )
    if request.method != "POST":
        return redirect(redirect_url)
    allocation.delete()
    messages.success(request, "Грантовое выделение получателю удалено. Счет баланса сохранен.")
    return redirect(redirect_url)


def _mass_reschedule_parse_date(value: object, fallback: date) -> date:
    if isinstance(value, date):
        return value
    if not value:
        return fallback
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return fallback


def _mass_reschedule_active_queryset(
    staff: StaffMember,
    *,
    date_from: date,
    date_to: date,
):
    if date_to < date_from:
        return Appointment.objects.none()

    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(date_from, datetime.min.time()), tz)
    end_dt = timezone.make_aware(datetime.combine(date_to, datetime.max.time()), tz)
    return Appointment.objects.filter(
        Q(staff_member=staff) | Q(staff_assignments__staff_member=staff),
        starts_at__gte=start_dt,
        starts_at__lte=end_dt,
        status__in=[
            Appointment.Status.PROPOSED,
            Appointment.Status.CONFIRMED,
            Appointment.Status.RESERVED,
        ],
    ).distinct()


def _mass_reschedule_summary_items(
    staff: StaffMember,
    *,
    date_from: date,
    date_to: date,
) -> list[dict[str, str]]:
    active_qs = _mass_reschedule_active_queryset(
        staff,
        date_from=date_from,
        date_to=date_to,
    )
    active_count = active_qs.count()
    service_count = active_qs.values("service_id").distinct().count()
    days_count = max((date_to - date_from).days + 1, 0)
    return [
        {
            "label": "Период",
            "value": f"{date_from:%d.%m.%Y}-{date_to:%d.%m.%Y}",
            "hint": f"дней в диапазоне: {days_count}",
        },
        {
            "label": "Активных занятий",
            "value": str(active_count),
            "hint": "proposed, reserved и confirmed",
        },
        {
            "label": "Услуги",
            "value": str(service_count),
            "hint": "направлений в выбранном периоде",
        },
        {
            "label": "Специалист",
            "value": staff.get_status_display(),
            "hint": "текущий статус профиля",
        },
    ]


def _mass_reschedule_next_action(
    staff: StaffMember,
    *,
    date_from: date,
    date_to: date,
) -> dict[str, str]:
    if date_to < date_from:
        return {
            "tone": "danger",
            "label": "Следующий шаг",
            "title": "Исправить период",
            "detail": "Дата окончания не может быть раньше даты начала.",
            "href": "#mass-reschedule-form",
        }

    active_count = _mass_reschedule_active_queryset(
        staff,
        date_from=date_from,
        date_to=date_to,
    ).count()
    if active_count:
        return {
            "tone": "warning",
            "label": "Следующий шаг",
            "title": "Сохранить план отсутствия",
            "detail": (
                f"Активных занятий в периоде: {active_count}. "
                "План сохранит их для ручного разбора без изменения расписания."
            ),
            "href": "#mass-reschedule-form",
        }

    return {
        "tone": "info",
        "label": "Следующий шаг",
        "title": "Активных занятий нет",
        "detail": "Можно изменить период или вернуться в рабочую очередь.",
        "href": "#mass-reschedule-form",
    }


def _redirect_or_hx(request, url: str):
    if request.headers.get("HX-Request") == "true":
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


@login_required
@user_passes_test(is_admin_user)
def staff_mass_reschedule(request, pk: int):
    staff = get_object_or_404(StaffMember, pk=pk)
    today = timezone.localdate()
    source = request.POST if request.method == "POST" else request.GET
    initial_date_from = _mass_reschedule_parse_date(
        source.get("date_from"),
        today,
    )
    initial_date_to = _mass_reschedule_parse_date(
        source.get("date_to"),
        today + timedelta(days=14),
    )
    initial = {
        "date_from": initial_date_from,
        "date_to": initial_date_to,
    }
    if request.method == "POST":
        date_from = datetime.fromisoformat(request.POST["date_from"]).date()
        date_to = datetime.fromisoformat(request.POST["date_to"]).date()
        reason = request.POST.get("reason", "").strip()
        if not reason:
            messages.error(request, "Укажите причину массовой отмены.")
        else:
            if request.POST.get("action") == "create_plan":
                try:
                    plan = plan_svc.create_staff_absence_plan(
                        staff,
                        date_from=date_from,
                        date_to=date_to,
                        reason=reason,
                        actor=request.user,
                    )
                except (ValueError, ValidationError) as exc:
                    if isinstance(exc, ValidationError):
                        messages.error(request, "; ".join(exc.messages))
                    else:
                        messages.error(request, str(exc))
                else:
                    if plan.steps.exists():
                        messages.success(
                            request,
                            f"План отсутствия сохранен: {plan.steps.count()} занятий требуют решения.",
                        )
                    else:
                        messages.warning(
                            request,
                            "План отсутствия создан, но активных занятий в периоде не найдено.",
                        )
                    return _redirect_or_hx(
                        request,
                        reverse("appointment_reschedule_plan_detail", args=[plan.pk]),
                    )
                return _redirect_or_hx(
                    request,
                    f"{reverse('staff_mass_reschedule', args=[staff.pk])}"
                    f"?date_from={date_from.isoformat()}&date_to={date_to.isoformat()}",
                )
            try:
                result = sched_svc.mass_reschedule(
                    staff,
                    date_from=date_from,
                    date_to=date_to,
                    reason=reason,
                    actor=request.user,
                )
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    f"Отменено {len(result.cancelled)} занятий, "
                    f"создано {len(result.confirmations)} уведомлений.",
                )
                return _redirect_or_hx(request, reverse("specialist_home"))
    return render(
        request,
        "operations/staff_mass_reschedule.html",
        {
            "staff": staff,
            "initial": initial,
            "cancel_url": reverse("work_queue"),
            "mass_reschedule_summary_items": _mass_reschedule_summary_items(
                staff,
                date_from=initial_date_from,
                date_to=initial_date_to,
            ),
            "mass_reschedule_next_action": _mass_reschedule_next_action(
                staff,
                date_from=initial_date_from,
                date_to=initial_date_to,
            ),
        },
    )
