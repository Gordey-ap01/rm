"""Табель специалиста, грант-отчёт, массовый перенос."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from operations.forms import (
    FundingPayrollBudgetForm,
    FundingServiceQuotaQuickForm,
    FundingStaffAllocationQuickForm,
    GrantCompensationCloseForm,
    GrantFixedCompensationForm,
    GrantPlanCloseForm,
    GrantRecipientAllocationQuickForm,
    GrantReportFilterForm,
    PayrollPayoutRecordForm,
    PayrollSheetSendForm,
    TimeSheetFilterForm,
)
from operations.models import (
    Appointment,
    FundingPayrollBudget,
    FundingPayrollBudgetRevision,
    FundingServiceQuota,
    FundingServiceQuotaRevision,
    FundingSource,
    FundingStaffAllocation,
    FundingStaffAllocationRevision,
    GrantFixedCompensation,
    GrantFixedCompensationRevision,
    GrantRecipientAllocation,
    PayrollAccrual,
    PayrollSheet,
    StaffMember,
)
from operations.services import (
    grant_compensation as grant_compensation_svc,
    grant_plans as grant_plans_svc,
    payroll as payroll_svc,
    reports as reports_svc,
    rescheduling_plans as plan_svc,
    scheduling as sched_svc,
)

from ._common import admin_required, director_required, is_admin_user, is_director


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


def _parse_model_pk(raw_value: object) -> int | None:
    text = str(raw_value or "").strip()
    if not text or len(text) > 19 or not text.isascii() or not text.isdigit():
        return None
    value = int(text)
    return value if 0 < value <= 9_223_372_036_854_775_807 else None

def _require_active_grant_source(funding: FundingSource) -> None:
    if funding.archived_at is not None:
        raise PermissionDenied(
            "Архивный источник доступен только для чтения. "
            "Для изменения сначала восстановите источник финансирования."
        )


def _add_grant_plan_form_errors(form, exc: ValidationError) -> None:
    if hasattr(exc, "message_dict"):
        for field_name, field_messages in exc.message_dict.items():
            target = field_name if field_name in form.fields else None
            for message in field_messages:
                form.add_error(target, message)
        return
    for message in exc.messages:
        form.add_error(None, message)


def _grant_plan_default_close_on(
    starts_on: date | None,
    ends_on: date | None,
) -> date:
    close_on = min(ends_on, timezone.localdate()) if ends_on else timezone.localdate()
    return max(starts_on, close_on) if starts_on else close_on


def _csv_text(value: object) -> str:
    text = "" if value is None else str(value)
    stripped = text.lstrip()
    if stripped.startswith(("=", "+", "-", "@", "\t", "\r")):
        return f"'{text}"
    return text


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
                    f"Последний лист: {latest.get_status_display()}, " f"{latest.total_amount} руб."
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


def _payroll_sheet_next_action(payroll_sheet: PayrollSheet, *, can_approve: bool) -> dict[str, str]:
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

    formal_quota_service_ids = {
        row.service.pk for row in report.quota_rows if row.quota is not None
    }
    summary_quota_rows = [
        row
        for row in report.quota_rows
        if row.quota is not None or row.service.pk not in formal_quota_service_ids
    ]
    quota_planned = sum(row.planned_sessions for row in summary_quota_rows)
    quota_allocated = sum(row.allocated_sessions for row in summary_quota_rows)
    quota_charged = sum(row.charged_sessions for row in summary_quota_rows)
    quota_remaining = sum(row.remaining_sessions for row in summary_quota_rows)
    items = [
        {
            "label": "Источник",
            "value": report.funding.name,
            "hint": f"{_timesheet_period_label(report.date_from, report.date_to)}.",
        },
    ]
    for unit_total in report.unit_totals:
        items.append(
            {
                "label": f"Остаток на конец: {unit_total.unit_label.lower()}",
                "value": str(unit_total.closing_balance),
                "hint": (
                    f"На начало: {unit_total.opening_balance}; "
                    f"поступления: {unit_total.inflows}; расход: {unit_total.outflows}. "
                    f"На сегодня: {unit_total.current_balance}."
                ),
            }
        )
    items.extend(
        [
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
    )
    return items


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
    negative_accounts = sum(1 for row in report.rows if row.closing_balance < 0)
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
        1 for row in report.quota_rows if row.planned_sessions > 0 and row.remaining_sessions == 0
    )
    overrun_sessions = sum(
        abs(row.remaining_sessions) for row in report.quota_rows if row.remaining_sessions < 0
    )
    overdrawn_recipients = sum(
        1 for row in report.recipient_allocation_rows if row.remaining_sessions < 0
    )

    if report.quota_missing_debit_count:
        items.append(
            {
                "tone": "danger",
                "title": "Есть списания без проводки",
                "detail": (
                    "Решение «Списать» не подтверждено ledger-проводкой для "
                    f"{report.quota_missing_debit_count} занятий. В факт квоты они не включены."
                ),
            }
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
    if overrun_sessions:
        items.append(
            {
                "tone": "danger",
                "title": "Факт превышает квоту",
                "detail": f"Сверх плана списано: {overrun_sessions} занятий.",
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


def _payroll_budget_control_items(
    budget: FundingPayrollBudget | None = None,
) -> list[dict[str, str]]:
    items = [
        {
            "title": "Период бюджета",
            "detail": "Для одного источника периоды бюджетов оплаты труда не пересекаются.",
        },
        {
            "title": "Жесткий лимит",
            "detail": "Режим «запрещать превышение» нельзя обойти даже решением руководителя.",
        },
        {
            "title": "История решений",
            "detail": "Каждое изменение создает новую редакцию с автором и основанием.",
        },
    ]
    if budget:
        active_positions = budget.fixed_compensations.filter(
            lifecycle_status=GrantFixedCompensation.LifecycleStatus.ACTIVE
        ).count()
        items.insert(
            1,
            {
                "title": "Активные позиции",
                "detail": (
                    f"В бюджете активных фиксированных позиций: {active_positions}. "
                    "Перед закрытием бюджета их нужно закрыть."
                ),
            },
        )
    return items


def _fixed_compensation_control_items(
    fixed: GrantFixedCompensation | None = None,
) -> list[dict[str, str]]:
    items = [
        {
            "title": "Фиксированная оплата услуги",
            "detail": "На том же периоде она несовместима со сдельной ставкой сотрудника по услуге.",
        },
        {
            "title": "Проектная роль",
            "detail": "Дополнительная проектная роль не заменяет оплату за проведенные занятия.",
        },
        {
            "title": "Дата начисления",
            "detail": "Дата начисления должна входить и в период позиции, и в период бюджета.",
        },
        {
            "title": "История решений",
            "detail": "Сотрудник, вид оплаты и предмет позиции после создания не меняются.",
        },
    ]
    if fixed:
        items.insert(
            0,
            {
                "title": "Редакция позиции",
                "detail": (
                    "Можно изменить период, дату начисления, сумму и примечание. "
                    "Идентичность позиции сохранится."
                ),
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
                    raise PermissionDenied(
                        "Передать расчетный лист в выплату может только руководитель."
                    )
                send_form = PayrollSheetSendForm(request.POST)
                if not send_form.is_valid():
                    messages.error(
                        request, "Укажите основание передачи в выплату не короче 5 символов."
                    )
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


@admin_required
def grant_report(request, pk: int | None = None):
    filter_data = request.GET.copy()
    selected_funding = None
    if pk is not None:
        funding = get_object_or_404(FundingSource.all_objects, pk=pk)
        selected_funding = funding
        today = timezone.localdate()
        filter_data["funding"] = str(funding.pk)
        filter_data.setdefault(
            "date_from",
            (funding.starts_on or today.replace(month=1, day=1)).isoformat(),
        )
        filter_data.setdefault(
            "date_to",
            (funding.ends_on or today.replace(month=12, day=31)).isoformat(),
        )
    form = GrantReportFilterForm(filter_data or None)
    report = None
    payroll_budgets: list[FundingPayrollBudget] = []
    form_is_valid = form.is_valid()
    if selected_funding is None and form.is_bound:
        selected_funding = form.cleaned_data.get("funding")
    if form_is_valid:
        selected_funding = form.cleaned_data["funding"]
        period_from = form.cleaned_data["date_from"]
        period_to = form.cleaned_data["date_to"]
        try:
            report = reports_svc.grant_report(
                selected_funding,
                period_from,
                period_to,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        fixed_positions = (
            GrantFixedCompensation.objects.filter(
                period_from__lte=period_to,
                period_to__gte=period_from,
            )
            .select_related("staff_member", "service", "current_revision")
            .order_by("staff_member__full_name", "period_from", "pk")
        )
        payroll_budgets = list(
            FundingPayrollBudget.objects.filter(
                funding_source=selected_funding,
                starts_on__lte=period_to,
                ends_on__gte=period_from,
            )
            .select_related("funding_source", "current_revision")
            .prefetch_related(
                Prefetch(
                    "fixed_compensations",
                    queryset=fixed_positions,
                    to_attr="report_fixed_compensations",
                )
            )
            .order_by("starts_on", "pk")
        )

    if "csv" in request.GET and report is not None:
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="grant_{report.funding.pk}.csv"'
        response.write("\ufeff")
        writer = csv.writer(response, delimiter=";", lineterminator="\n")
        writer.writerow(
            [
                "Счёт",
                "Единица",
                "На начало",
                "Поступления",
                "Расход",
                "На конец периода",
                "На сегодня",
                "Занятий",
            ]
        )
        for row in report.rows:
            writer.writerow(
                [
                    _csv_text(row.account),
                    row.account.get_unit_display(),
                    row.opening_balance,
                    row.inflows,
                    row.outflows,
                    row.closing_balance,
                    row.current_balance,
                    row.appointments_count,
                ]
            )
        for total in report.unit_totals:
            writer.writerow(
                [
                    "Итого",
                    total.unit_label,
                    total.opening_balance,
                    total.inflows,
                    total.outflows,
                    total.closing_balance,
                    total.current_balance,
                    total.appointments_count,
                ]
            )
        if report.quota_rows:
            writer.writerow([])
            writer.writerow(["Квоты по услугам"])
            writer.writerow(["Услуга", "План", "Распределено", "Факт списано", "Остаток"])
            for row in report.quota_rows:
                writer.writerow(
                    [
                        _csv_text(row.service),
                        row.planned_sessions,
                        row.allocated_sessions,
                        row.charged_sessions,
                        row.remaining_sessions,
                    ]
                )
                for staff_row in row.staff_rows:
                    writer.writerow(
                        [
                            _csv_text(f"  {staff_row.staff_member}"),
                            staff_row.allocated_sessions,
                            staff_row.charged_sessions,
                            staff_row.remaining_sessions,
                            (
                                staff_row.session_pay_amount
                                if staff_row.session_pay_amount is not None
                                else ""
                            ),
                        ]
                    )
        if report.recipient_allocation_rows:
            writer.writerow([])
            writer.writerow(["Выделения получателям"])
            writer.writerow(["Получатель", "Услуга", "Выделено", "Списано", "Остаток", "Счет"])
            for row in report.recipient_allocation_rows:
                writer.writerow(
                    [
                        _csv_text(row.child),
                        _csv_text(row.service),
                        row.allocated_sessions,
                        row.charged_sessions,
                        row.remaining_sessions,
                        _csv_text(row.balance_account),
                    ]
                )
        return response

    can_manage_grants = bool(
        is_director(request.user)
        and (selected_funding is None or selected_funding.archived_at is None)
    )
    return render(
        request,
        "operations/grant_report.html",
        {
            "form": form,
            "report": report,
            "funding_id": pk,
            "grant_summary_items": _grant_report_summary_items(report),
            "grant_attention_items": _grant_report_attention_items(report),
            "payroll_budgets": payroll_budgets,
            "can_manage_grants": can_manage_grants,
        },
    )


@director_required
def funding_service_quota_create(request):
    initial = {
        "funding_source": request.GET.get("funding") or None,
        "service": request.GET.get("service") or None,
    }
    initial = {key: value for key, value in initial.items() if value}
    if request.method == "POST":
        form = FundingServiceQuotaQuickForm(request.POST)
        if form.is_valid():
            try:
                quota = grant_plans_svc.create_service_quota(
                    funding_source=form.cleaned_data["funding_source"],
                    service=form.cleaned_data["service"],
                    planned_sessions=form.cleaned_data["planned_sessions"],
                    starts_on=form.cleaned_data["starts_on"],
                    ends_on=form.cleaned_data["ends_on"],
                    note=form.cleaned_data["note"],
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                )
            except ValidationError as exc:
                _add_grant_plan_form_errors(form, exc)
            else:
                messages.success(
                    request,
                    "Квота создана. Теперь распределите занятия между специалистами.",
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


@director_required
def funding_service_quota_edit(request, pk: int):
    quota = get_object_or_404(
        FundingServiceQuota.objects.select_related("funding_source", "service"),
        pk=pk,
    )
    _require_active_grant_source(quota.funding_source)
    if request.method == "POST":
        form = FundingServiceQuotaQuickForm(request.POST, instance=quota)
        if form.is_valid():
            try:
                quota = grant_plans_svc.revise_service_quota(
                    quota,
                    planned_sessions=form.cleaned_data["planned_sessions"],
                    starts_on=form.cleaned_data["starts_on"],
                    ends_on=form.cleaned_data["ends_on"],
                    note=form.cleaned_data["note"],
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                    expected_revision_id=form.cleaned_data["expected_revision_id"],
                )
            except ValidationError as exc:
                _add_grant_plan_form_errors(form, exc)
            else:
                messages.success(request, "Создана новая редакция квоты по услуге.")
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
            "title": "Создать редакцию квоты",
            "subtitle": str(quota),
            "form_panel_title": "Параметры квоты",
            "form_intro": (
                "Предыдущая редакция останется в истории. Уже списанные занятия "
                "нельзя исключить новым периодом или количеством."
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


@director_required
def funding_service_quota_delete(request, pk: int):
    quota = get_object_or_404(
        FundingServiceQuota.objects.select_related("funding_source", "service"),
        pk=pk,
    )
    _require_active_grant_source(quota.funding_source)
    redirect_url = _grant_report_url(
        quota.funding_source_id,
        date_from=quota.starts_on,
        date_to=quota.ends_on,
    )
    initial = {
        "close_on": _grant_plan_default_close_on(quota.starts_on, quota.ends_on),
        "expected_revision_id": quota.current_revision_id,
    }
    if request.method == "POST":
        form = GrantPlanCloseForm(request.POST)
        if form.is_valid():
            try:
                grant_plans_svc.close_service_quota(
                    quota,
                    close_on=form.cleaned_data["close_on"],
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                    expected_revision_id=form.cleaned_data["expected_revision_id"],
                )
            except ValidationError as exc:
                _add_grant_plan_form_errors(form, exc)
            else:
                messages.success(request, "Квота закрыта без удаления истории.")
                return redirect(redirect_url)
    else:
        form = GrantPlanCloseForm(initial=initial)
    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Закрыть квоту по услуге",
            "subtitle": str(quota),
            "form_panel_title": "Дата и основание закрытия",
            "form_intro": (
                "Физического удаления не будет. Сначала закройте связанные активные "
                "распределения специалистам."
            ),
            "form": form,
            "cancel_url": redirect_url,
        },
    )


@admin_required
def funding_service_quota_history(request, pk: int):
    quota = get_object_or_404(
        FundingServiceQuota.objects.select_related("funding_source", "service"),
        pk=pk,
    )
    revisions = list(
        FundingServiceQuotaRevision.objects.filter(service_quota=quota)
        .select_related("actor")
        .order_by("-revision_number")
    )
    return render(
        request,
        "operations/grant_plan_history.html",
        {
            "title": "История квоты по услуге",
            "subtitle": str(quota),
            "history_kind": "quota",
            "revisions": revisions,
            "current_revision_id": quota.current_revision_id,
            "back_url": _grant_report_url(
                quota.funding_source_id,
                date_from=quota.starts_on,
                date_to=quota.ends_on,
            ),
        },
    )


@director_required
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
            try:
                allocation = grant_plans_svc.create_staff_allocation(
                    service_quota=form.cleaned_data["service_quota"],
                    funding_source=form.cleaned_data["funding_source"],
                    service=form.cleaned_data["service"],
                    staff_member=form.cleaned_data["staff_member"],
                    allocated_sessions=form.cleaned_data["allocated_sessions"],
                    session_pay_amount=form.cleaned_data["session_pay_amount"],
                    starts_on=form.cleaned_data["starts_on"],
                    ends_on=form.cleaned_data["ends_on"],
                    note=form.cleaned_data["note"],
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                )
            except ValidationError as exc:
                _add_grant_plan_form_errors(form, exc)
            else:
                messages.success(request, "Распределение грантовой квоты создано.")
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


@director_required
def funding_staff_allocation_edit(request, pk: int):
    allocation = get_object_or_404(
        FundingStaffAllocation.objects.select_related(
            "service_quota", "funding_source", "service", "staff_member"
        ),
        pk=pk,
    )
    _require_active_grant_source(allocation.funding_source)
    if request.method == "POST":
        form = FundingStaffAllocationQuickForm(request.POST, instance=allocation)
        if form.is_valid():
            try:
                allocation = grant_plans_svc.revise_staff_allocation(
                    allocation,
                    allocated_sessions=form.cleaned_data["allocated_sessions"],
                    session_pay_amount=form.cleaned_data["session_pay_amount"],
                    starts_on=form.cleaned_data["starts_on"],
                    ends_on=form.cleaned_data["ends_on"],
                    note=form.cleaned_data["note"],
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                    expected_revision_id=form.cleaned_data["expected_revision_id"],
                )
            except ValidationError as exc:
                _add_grant_plan_form_errors(form, exc)
            else:
                messages.success(request, "Создана новая редакция распределения.")
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
            "title": "Создать редакцию распределения",
            "subtitle": str(allocation),
            "form_panel_title": "Параметры распределения",
            "form_intro": (
                "Предыдущие значения останутся в истории. Фактически списанные занятия "
                "нельзя исключить или распределить повторно."
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


@director_required
def funding_staff_allocation_delete(request, pk: int):
    allocation = get_object_or_404(
        FundingStaffAllocation.objects.select_related("funding_source", "service", "staff_member"),
        pk=pk,
    )
    _require_active_grant_source(allocation.funding_source)
    redirect_url = _grant_report_url(
        allocation.funding_source_id,
        date_from=allocation.starts_on,
        date_to=allocation.ends_on,
    )
    initial = {
        "close_on": _grant_plan_default_close_on(
            allocation.starts_on,
            allocation.ends_on,
        ),
        "expected_revision_id": allocation.current_revision_id,
    }
    if request.method == "POST":
        form = GrantPlanCloseForm(request.POST)
        if form.is_valid():
            try:
                grant_plans_svc.close_staff_allocation(
                    allocation,
                    close_on=form.cleaned_data["close_on"],
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                    expected_revision_id=form.cleaned_data["expected_revision_id"],
                )
            except ValidationError as exc:
                _add_grant_plan_form_errors(form, exc)
            else:
                messages.success(request, "Распределение закрыто без удаления истории.")
                return redirect(redirect_url)
    else:
        form = GrantPlanCloseForm(initial=initial)
    return render(
        request,
        "operations/object_form.html",
        {
            "title": "Закрыть распределение специалисту",
            "subtitle": str(allocation),
            "form_panel_title": "Дата и основание закрытия",
            "form_intro": (
                "Количество и ставка сохранятся в истории. Для передачи остатка "
                "сначала создайте редакцию количества, затем новую позицию специалиста."
            ),
            "form": form,
            "cancel_url": redirect_url,
        },
    )


@admin_required
def funding_staff_allocation_history(request, pk: int):
    allocation = get_object_or_404(
        FundingStaffAllocation.objects.select_related(
            "service_quota",
            "funding_source",
            "service",
            "staff_member",
        ),
        pk=pk,
    )
    revisions = list(
        FundingStaffAllocationRevision.objects.filter(staff_allocation=allocation)
        .select_related("actor")
        .order_by("-revision_number")
    )
    return render(
        request,
        "operations/grant_plan_history.html",
        {
            "title": "История распределения специалисту",
            "subtitle": str(allocation),
            "history_kind": "staff",
            "revisions": revisions,
            "current_revision_id": allocation.current_revision_id,
            "back_url": _grant_report_url(
                allocation.funding_source_id,
                date_from=allocation.starts_on,
                date_to=allocation.ends_on,
            ),
        },
    )


@director_required
def funding_payroll_budget_create(request):
    initial = {"funding_source": request.GET.get("funding") or None}
    if request.method == "POST":
        form = FundingPayrollBudgetForm(request.POST)
        if form.is_valid():
            try:
                budget = grant_compensation_svc.create_payroll_budget(
                    funding_source=form.cleaned_data["funding_source"],
                    starts_on=form.cleaned_data["starts_on"],
                    ends_on=form.cleaned_data["ends_on"],
                    planned_amount=form.cleaned_data["planned_amount"],
                    enforcement_mode=form.cleaned_data["enforcement_mode"],
                    note=form.cleaned_data["note"],
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                )
            except ValidationError as exc:
                _add_grant_plan_form_errors(form, exc)
            else:
                messages.success(request, "Бюджет оплаты труда создан.")
                return redirect(
                    _grant_report_url(
                        budget.funding_source_id,
                        date_from=budget.starts_on,
                        date_to=budget.ends_on,
                    )
                )
    else:
        form = FundingPayrollBudgetForm(initial=initial)
    return render(
        request,
        "operations/grant_compensation_form.html",
        {
            "title": "Создать бюджет оплаты труда",
            "subtitle": "Утвердите лимит оплаты труда по источнику и точному периоду.",
            "form_panel_title": "Параметры бюджета",
            "form_intro": (
                "Период не должен пересекаться с другим бюджетом этого источника. "
                "Решение будет зафиксировано как первая редакция."
            ),
            "form": form,
            "control_title": "Контроль бюджета",
            "object_form_control_items": _payroll_budget_control_items(),
            "submit_label": "Создать бюджет",
            "cancel_url": reverse("grant_report"),
        },
    )


@director_required
def funding_payroll_budget_edit(request, pk: int):
    budget = get_object_or_404(
        FundingPayrollBudget.objects.select_related("funding_source", "current_revision"),
        pk=pk,
    )
    _require_active_grant_source(budget.funding_source)
    if budget.lifecycle_status == FundingPayrollBudget.LifecycleStatus.CLOSED:
        raise PermissionDenied("Закрытый бюджет доступен только для чтения.")
    if request.method == "POST":
        form = FundingPayrollBudgetForm(request.POST, instance=budget)
        if form.is_valid():
            try:
                budget = grant_compensation_svc.revise_payroll_budget(
                    budget,
                    starts_on=form.cleaned_data["starts_on"],
                    ends_on=form.cleaned_data["ends_on"],
                    planned_amount=form.cleaned_data["planned_amount"],
                    enforcement_mode=form.cleaned_data["enforcement_mode"],
                    note=form.cleaned_data["note"],
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                    expected_revision_id=form.cleaned_data["expected_revision_id"],
                )
            except ValidationError as exc:
                _add_grant_plan_form_errors(form, exc)
            else:
                messages.success(request, "Создана новая редакция бюджета оплаты труда.")
                return redirect(
                    _grant_report_url(
                        budget.funding_source_id,
                        date_from=budget.starts_on,
                        date_to=budget.ends_on,
                    )
                )
    else:
        form = FundingPayrollBudgetForm(instance=budget)
    return render(
        request,
        "operations/grant_compensation_form.html",
        {
            "title": "Создать редакцию бюджета",
            "subtitle": str(budget),
            "form_panel_title": "Новые параметры бюджета",
            "form_intro": (
                "Источник финансирования останется неизменным. Предыдущая редакция "
                "сохранится в истории решений."
            ),
            "form": form,
            "control_title": "Контроль бюджета",
            "object_form_control_items": _payroll_budget_control_items(budget),
            "submit_label": "Сохранить редакцию",
            "cancel_url": _grant_report_url(
                budget.funding_source_id,
                date_from=budget.starts_on,
                date_to=budget.ends_on,
            ),
        },
    )


@director_required
def funding_payroll_budget_close(request, pk: int):
    budget = get_object_or_404(
        FundingPayrollBudget.objects.select_related("funding_source", "current_revision"),
        pk=pk,
    )
    _require_active_grant_source(budget.funding_source)
    if budget.lifecycle_status == FundingPayrollBudget.LifecycleStatus.CLOSED:
        raise PermissionDenied("Бюджет уже закрыт.")
    redirect_url = _grant_report_url(
        budget.funding_source_id,
        date_from=budget.starts_on,
        date_to=budget.ends_on,
    )
    initial = {"expected_revision_id": budget.current_revision_id}
    if request.method == "POST":
        form = GrantCompensationCloseForm(request.POST)
        if form.is_valid():
            try:
                grant_compensation_svc.close_payroll_budget(
                    budget,
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                    expected_revision_id=form.cleaned_data["expected_revision_id"],
                )
            except ValidationError as exc:
                _add_grant_plan_form_errors(form, exc)
            else:
                messages.success(request, "Бюджет закрыт без удаления истории.")
                return redirect(redirect_url)
    else:
        form = GrantCompensationCloseForm(initial=initial)
    return render(
        request,
        "operations/grant_compensation_form.html",
        {
            "title": "Закрыть бюджет оплаты труда",
            "subtitle": str(budget),
            "form_panel_title": "Основание закрытия",
            "form_intro": (
                "Период и сумма сохранятся для истории и последующего сопоставления. "
                "Сначала закройте все активные фиксированные позиции бюджета."
            ),
            "form": form,
            "control_title": "Перед закрытием",
            "object_form_control_items": _payroll_budget_control_items(budget),
            "submit_label": "Закрыть бюджет",
            "submit_tone": "danger",
            "cancel_url": redirect_url,
        },
    )


@admin_required
def funding_payroll_budget_history(request, pk: int):
    budget = get_object_or_404(
        FundingPayrollBudget.objects.select_related("funding_source"),
        pk=pk,
    )
    revisions = list(
        FundingPayrollBudgetRevision.objects.filter(payroll_budget=budget)
        .select_related("actor")
        .order_by("-revision_number")
    )
    return render(
        request,
        "operations/grant_compensation_history.html",
        {
            "title": "История бюджета оплаты труда",
            "subtitle": str(budget),
            "history_kind": "payroll_budget",
            "revisions": revisions,
            "current_revision_id": budget.current_revision_id,
            "back_url": _grant_report_url(
                budget.funding_source_id,
                date_from=budget.starts_on,
                date_to=budget.ends_on,
            ),
        },
    )


@director_required
def grant_fixed_compensation_create(request):
    raw_funding_id = request.GET.get("funding", "")
    funding_source_id = _parse_model_pk(raw_funding_id)
    initial = {
        "payroll_budget": request.GET.get("budget") or None,
        "staff_member": request.GET.get("staff") or None,
        "service": request.GET.get("service") or None,
    }
    initial = {key: value for key, value in initial.items() if value}
    if request.method == "POST":
        raw_budget_id = _parse_model_pk(request.POST.get("payroll_budget", ""))
        if raw_budget_id is not None:
            posted_source_id = (
                FundingPayrollBudget.objects.filter(pk=raw_budget_id)
                .values_list("funding_source_id", flat=True)
                .first()
            )
            funding_source_id = posted_source_id or funding_source_id
        form = GrantFixedCompensationForm(
            request.POST,
            funding_source_id=funding_source_id,
        )
        if form.is_valid():
            try:
                fixed = grant_compensation_svc.create_fixed_compensation(
                    payroll_budget=form.cleaned_data["payroll_budget"],
                    staff_member=form.cleaned_data["staff_member"],
                    compensation_scope=form.cleaned_data["compensation_scope"],
                    service=form.cleaned_data["service"],
                    assignment_label=form.cleaned_data["assignment_label"],
                    period_from=form.cleaned_data["period_from"],
                    period_to=form.cleaned_data["period_to"],
                    accrual_on=form.cleaned_data["accrual_on"],
                    amount=form.cleaned_data["amount"],
                    note=form.cleaned_data["note"],
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                )
            except ValidationError as exc:
                _add_grant_plan_form_errors(form, exc)
            else:
                messages.success(request, "Фиксированная позиция оплаты труда создана.")
                return redirect(
                    _grant_report_url(
                        fixed.payroll_budget.funding_source_id,
                        date_from=fixed.period_from,
                        date_to=fixed.period_to,
                    )
                )
    else:
        form = GrantFixedCompensationForm(
            initial=initial,
            funding_source_id=funding_source_id,
        )
    return render(
        request,
        "operations/grant_compensation_form.html",
        {
            "title": "Создать фиксированную позицию",
            "subtitle": "Закрепите фиксированную сумму за услугой или проектной ролью сотрудника.",
            "form_panel_title": "Параметры позиции",
            "form_intro": (
                "Выберите один вид оплаты. Позиция должна целиком входить в период "
                "утвержденного бюджета."
            ),
            "form": form,
            "control_title": "Контроль позиции",
            "object_form_control_items": _fixed_compensation_control_items(),
            "submit_label": "Создать позицию",
            "scope_controls": True,
            "cancel_url": (
                _grant_report_url(funding_source_id)
                if funding_source_id
                else reverse("grant_report")
            ),
        },
    )


@director_required
def grant_fixed_compensation_edit(request, pk: int):
    fixed = get_object_or_404(
        GrantFixedCompensation.objects.select_related(
            "payroll_budget",
            "payroll_budget__funding_source",
            "staff_member",
            "service",
            "current_revision",
        ),
        pk=pk,
    )
    _require_active_grant_source(fixed.payroll_budget.funding_source)
    if fixed.lifecycle_status == GrantFixedCompensation.LifecycleStatus.CLOSED:
        raise PermissionDenied("Закрытая фиксированная позиция доступна только для чтения.")
    if request.method == "POST":
        form = GrantFixedCompensationForm(request.POST, instance=fixed)
        if form.is_valid():
            try:
                fixed = grant_compensation_svc.revise_fixed_compensation(
                    fixed,
                    period_from=form.cleaned_data["period_from"],
                    period_to=form.cleaned_data["period_to"],
                    accrual_on=form.cleaned_data["accrual_on"],
                    amount=form.cleaned_data["amount"],
                    note=form.cleaned_data["note"],
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                    expected_revision_id=form.cleaned_data["expected_revision_id"],
                )
            except ValidationError as exc:
                _add_grant_plan_form_errors(form, exc)
            else:
                messages.success(request, "Создана новая редакция фиксированной позиции.")
                return redirect(
                    _grant_report_url(
                        fixed.payroll_budget.funding_source_id,
                        date_from=fixed.period_from,
                        date_to=fixed.period_to,
                    )
                )
    else:
        form = GrantFixedCompensationForm(instance=fixed)
    return render(
        request,
        "operations/grant_compensation_form.html",
        {
            "title": "Создать редакцию фиксированной позиции",
            "subtitle": str(fixed),
            "form_panel_title": "Новые параметры позиции",
            "form_intro": (
                "Идентичность позиции заблокирована. Измените период, дату начисления, "
                "сумму или примечание и зафиксируйте основание."
            ),
            "form": form,
            "control_title": "Контроль позиции",
            "object_form_control_items": _fixed_compensation_control_items(fixed),
            "submit_label": "Сохранить редакцию",
            "scope_controls": True,
            "cancel_url": _grant_report_url(
                fixed.payroll_budget.funding_source_id,
                date_from=fixed.period_from,
                date_to=fixed.period_to,
            ),
        },
    )


@director_required
def grant_fixed_compensation_close(request, pk: int):
    fixed = get_object_or_404(
        GrantFixedCompensation.objects.select_related(
            "payroll_budget",
            "payroll_budget__funding_source",
            "staff_member",
            "service",
            "current_revision",
        ),
        pk=pk,
    )
    _require_active_grant_source(fixed.payroll_budget.funding_source)
    if fixed.lifecycle_status == GrantFixedCompensation.LifecycleStatus.CLOSED:
        raise PermissionDenied("Фиксированная позиция уже закрыта.")
    redirect_url = _grant_report_url(
        fixed.payroll_budget.funding_source_id,
        date_from=fixed.period_from,
        date_to=fixed.period_to,
    )
    initial = {"expected_revision_id": fixed.current_revision_id}
    if request.method == "POST":
        form = GrantCompensationCloseForm(request.POST)
        if form.is_valid():
            try:
                grant_compensation_svc.close_fixed_compensation(
                    fixed,
                    actor=request.user,
                    reason=form.cleaned_data["reason"],
                    expected_revision_id=form.cleaned_data["expected_revision_id"],
                )
            except ValidationError as exc:
                _add_grant_plan_form_errors(form, exc)
            else:
                messages.success(request, "Фиксированная позиция закрыта без удаления истории.")
                return redirect(redirect_url)
    else:
        form = GrantCompensationCloseForm(initial=initial)
    return render(
        request,
        "operations/grant_compensation_form.html",
        {
            "title": "Закрыть фиксированную позицию",
            "subtitle": str(fixed),
            "form_panel_title": "Основание закрытия",
            "form_intro": (
                "Сумма, период и дата начисления сохранятся. Закрытие создаст новую "
                "неизменяемую редакцию, физического удаления не будет."
            ),
            "form": form,
            "control_title": "Перед закрытием",
            "object_form_control_items": _fixed_compensation_control_items(fixed),
            "submit_label": "Закрыть позицию",
            "submit_tone": "danger",
            "cancel_url": redirect_url,
        },
    )


@admin_required
def grant_fixed_compensation_history(request, pk: int):
    fixed = get_object_or_404(
        GrantFixedCompensation.objects.select_related(
            "payroll_budget",
            "payroll_budget__funding_source",
            "staff_member",
            "service",
        ),
        pk=pk,
    )
    revisions = list(
        GrantFixedCompensationRevision.objects.filter(fixed_compensation=fixed)
        .select_related("actor", "service", "budget_revision_at_decision")
        .order_by("-revision_number")
    )
    return render(
        request,
        "operations/grant_compensation_history.html",
        {
            "title": "История фиксированной позиции",
            "subtitle": str(fixed),
            "history_kind": "fixed_compensation",
            "revisions": revisions,
            "current_revision_id": fixed.current_revision_id,
            "back_url": _grant_report_url(
                fixed.payroll_budget.funding_source_id,
                date_from=fixed.period_from,
                date_to=fixed.period_to,
            ),
        },
    )


@director_required
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


@director_required
def grant_recipient_allocation_edit(request, pk: int):
    allocation = get_object_or_404(
        GrantRecipientAllocation.objects.select_related(
            "funding_source", "child", "service", "balance_account"
        ),
        pk=pk,
    )
    _require_active_grant_source(allocation.funding_source)
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


@director_required
def grant_recipient_allocation_delete(request, pk: int):
    allocation = get_object_or_404(
        GrantRecipientAllocation.objects.select_related("funding_source"),
        pk=pk,
    )
    _require_active_grant_source(allocation.funding_source)
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
