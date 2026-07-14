"""Дашборд и очередь задач администратора."""

from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from operations.forms import default_charge_amount
from operations.models import (
    ACTION_REQUIRED_BILLING_STATUSES,
    Appointment,
    AppointmentConfirmation,
    AppointmentRescheduleChain,
    AppointmentRescheduleStep,
    AppointmentStaffAssignment,
    BalanceAccount,
    FinancialIntegrityCheckRun,
    FinancialIntegrityFinding,
    FinancialIntegrityFindingEvent,
    TimeOffRequest,
)
from operations.services import (
    financial_integrity_checks as financial_integrity_checks_svc,
    financial_integrity_events as financial_integrity_events_svc,
    financial_integrity_triage as financial_integrity_triage_svc,
    rescheduling_plans as plan_svc,
)

from ._common import is_admin_user, safe_next_url


def needs_billing_queryset():
    return (
        Appointment.objects.all()
        .filter(
            Q(status__in=ACTION_REQUIRED_BILLING_STATUSES)
            | ~Q(attendance_status=Appointment.AttendanceStatus.UNKNOWN)
        )
        .annotate(
            participant_count=Count("participants", distinct=True),
            undecided_participant_count=Count(
                "participants",
                filter=Q(participants__billing_decision=Appointment.BillingDecision.UNDECIDED),
                distinct=True,
            ),
        )
        .filter(
            Q(
                participant_count=0,
                billing_decision=Appointment.BillingDecision.UNDECIDED,
            )
            | Q(participant_count__gt=0, undecided_participant_count__gt=0)
        )
        .select_related("child", "staff_member", "service", "room", "billing_account")
        .prefetch_related(
            "participants__child",
            "participants__billing_account",
            "staff_assignments__staff_member",
        )
        .order_by("-starts_at")
    )


def needs_attendance_queryset(now=None):
    now = now or timezone.now()
    return (
        Appointment.objects.filter(ends_at__lt=now)
        .annotate(
            participant_count=Count("participants", distinct=True),
            unknown_participant_count=Count(
                "participants",
                filter=Q(participants__attendance_status=Appointment.AttendanceStatus.UNKNOWN),
                distinct=True,
            ),
        )
        .filter(
            Q(
                status__in=[Appointment.Status.CONFIRMED, Appointment.Status.PROPOSED],
                attendance_status=Appointment.AttendanceStatus.UNKNOWN,
            )
            | Q(participant_count__gt=0, unknown_participant_count__gt=0)
        )
        .select_related("child", "staff_member", "service", "room")
        .prefetch_related("participants__child", "staff_assignments__staff_member")
        .order_by("starts_at")
    )


def needs_transfer_queryset():
    return (
        Appointment.objects.filter(
            status__in=[Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW],
            rescheduled_to__isnull=True,
        )
        .select_related("child", "staff_member", "service", "room")
        .prefetch_related("participants__child", "staff_assignments__staff_member")
        .order_by("-starts_at")
    )


def reschedule_plan_focus_url(focus: str) -> str:
    return f"{reverse('reschedule_plan_list')}?focus={focus}"


TERMINAL_RESCHEDULE_PLAN_STATUSES = plan_svc.TERMINAL_PLAN_STATUSES
FINANCIAL_INTEGRITY_DISPLAY_LIMIT = 40
FINANCIAL_INTEGRITY_EVENT_DISPLAY_LIMIT = 20
FINANCIAL_INTEGRITY_ACTIVE_STATUSES = (
    FinancialIntegrityFinding.Status.OPEN,
    FinancialIntegrityFinding.Status.ACKNOWLEDGED,
)
FINANCIAL_INTEGRITY_EVENT_TONES = {
    FinancialIntegrityFindingEvent.EventType.CREATED: "warning",
    FinancialIntegrityFindingEvent.EventType.ACKNOWLEDGED: "info",
    FinancialIntegrityFindingEvent.EventType.RETURNED_TO_OPEN: "warning",
    FinancialIntegrityFindingEvent.EventType.IGNORED: "muted",
    FinancialIntegrityFindingEvent.EventType.REOPENED: "danger",
    FinancialIntegrityFindingEvent.EventType.RESOLVED: "success",
    FinancialIntegrityFindingEvent.EventType.SCOPED_RECHECK: "info",
    FinancialIntegrityFindingEvent.EventType.NOTE_ADDED: "info",
}


def confirmation_attention_filter() -> Q:
    return (
        Q(status=AppointmentConfirmation.Status.DECLINED)
        | Q(delivery_status=AppointmentConfirmation.DeliveryStatus.FAILED)
        | Q(status=AppointmentConfirmation.Status.PENDING)
    )


def confirmation_attention_queryset():
    return AppointmentConfirmation.objects.filter(confirmation_attention_filter()).filter(
        Q(reschedule_step__isnull=True)
        | ~Q(reschedule_step__plan__status__in=TERMINAL_RESCHEDULE_PLAN_STATUSES)
    )


def reschedule_chain_attention_priority():
    return Case(
        When(status=AppointmentRescheduleChain.Status.FAILED, then=Value(0)),
        When(status=AppointmentRescheduleChain.Status.STALE, then=Value(1)),
        When(status=AppointmentRescheduleChain.Status.READY, then=Value(2)),
        default=Value(9),
        output_field=IntegerField(),
    )


def reschedule_chain_attention_queryset():
    return (
        AppointmentRescheduleChain.objects.select_related(
            "plan",
            "plan__root_appointment",
            "plan__root_appointment__child",
            "plan__root_appointment__service",
            "plan__staff_member",
            "created_by",
        )
        .annotate(
            attention_priority=reschedule_chain_attention_priority(),
            step_count=Count("steps", distinct=True),
            dependency_count=Count("dependencies", distinct=True),
        )
        .filter(
            status__in=[
                AppointmentRescheduleChain.Status.READY,
                AppointmentRescheduleChain.Status.STALE,
                AppointmentRescheduleChain.Status.FAILED,
            ]
        )
        .exclude(
            plan__status__in=TERMINAL_RESCHEDULE_PLAN_STATUSES
        )
        .order_by("attention_priority", "-updated_at", "-pk")
    )


def reschedule_chain_attention_counts(queryset=None) -> dict[str, int]:
    if queryset is None:
        queryset = reschedule_chain_attention_queryset()
    return {
        "ready": queryset.filter(status=AppointmentRescheduleChain.Status.READY).count(),
        "stale": queryset.filter(status=AppointmentRescheduleChain.Status.STALE).count(),
        "failed": queryset.filter(status=AppointmentRescheduleChain.Status.FAILED).count(),
    }


def reschedule_step_open_statuses():
    step_status = AppointmentRescheduleStep.Status
    return [
        step_status.PENDING,
        step_status.VALID,
        step_status.STALE,
        step_status.FAILED,
    ]


def reschedule_step_attention_filter() -> Q:
    step_status = AppointmentRescheduleStep.Status
    confirmation_status = AppointmentRescheduleStep.ConfirmationStatus
    action_type = AppointmentRescheduleStep.ActionType
    open_step_statuses = reschedule_step_open_statuses()
    return (
        Q(status=step_status.FAILED)
        | Q(action_type=action_type.REVIEW_CONFLICT, status__in=open_step_statuses)
        | Q(status=step_status.STALE)
        | Q(confirmation_status=confirmation_status.DECLINED)
        | Q(confirmation_status=confirmation_status.WAITING)
        | (
            Q(status=step_status.VALID)
            & Q(
                confirmation_status__in=[
                    confirmation_status.NOT_REQUESTED,
                    confirmation_status.APPROVED,
                ]
            )
        )
    )


def reschedule_step_attention_priority():
    step_status = AppointmentRescheduleStep.Status
    confirmation_status = AppointmentRescheduleStep.ConfirmationStatus
    action_type = AppointmentRescheduleStep.ActionType
    open_step_statuses = reschedule_step_open_statuses()
    return Case(
        When(status=step_status.FAILED, then=Value(0)),
        When(
            action_type=action_type.REVIEW_CONFLICT,
            status__in=open_step_statuses,
            then=Value(1),
        ),
        When(status=step_status.STALE, then=Value(2)),
        When(confirmation_status=confirmation_status.DECLINED, then=Value(3)),
        When(confirmation_status=confirmation_status.WAITING, then=Value(4)),
        When(
            status=step_status.VALID,
            confirmation_status__in=[
                confirmation_status.NOT_REQUESTED,
                confirmation_status.APPROVED,
            ],
            then=Value(5),
        ),
        default=Value(9),
        output_field=IntegerField(),
    )


def reschedule_step_attention_queryset():
    return (
        AppointmentRescheduleStep.objects.select_related(
            "plan",
            "plan__root_appointment",
            "plan__root_appointment__child",
            "plan__root_appointment__service",
            "plan__root_appointment__staff_member",
            "plan__staff_member",
            "source_appointment",
            "source_appointment__child",
            "source_appointment__service",
            "source_appointment__staff_member",
            "blocking_appointment",
            "blocking_appointment__child",
            "proposed_primary_staff",
            "proposed_room",
        )
        .filter(reschedule_step_attention_filter(), chain__isnull=True)
        .exclude(
            plan__status__in=TERMINAL_RESCHEDULE_PLAN_STATUSES
        )
        .annotate(attention_priority=reschedule_step_attention_priority())
        .order_by("attention_priority", "-plan__created_at", "position", "pk")
    )


def reschedule_step_attention_counts(queryset=None) -> dict[str, int]:
    if queryset is None:
        queryset = reschedule_step_attention_queryset()
    step_status = AppointmentRescheduleStep.Status
    confirmation_status = AppointmentRescheduleStep.ConfirmationStatus
    action_type = AppointmentRescheduleStep.ActionType
    open_step_statuses = reschedule_step_open_statuses()
    return {
        "total": queryset.count(),
        "ready": queryset.filter(
            status=step_status.VALID,
            confirmation_status__in=[
                confirmation_status.NOT_REQUESTED,
                confirmation_status.APPROVED,
            ],
        ).count(),
        "waiting": queryset.filter(confirmation_status=confirmation_status.WAITING).count(),
        "declined": queryset.filter(confirmation_status=confirmation_status.DECLINED).count(),
        "stale": queryset.filter(status=step_status.STALE).count(),
        "failed": queryset.filter(status=step_status.FAILED).count(),
        "manual_review": queryset.filter(
            action_type=action_type.REVIEW_CONFLICT,
            status__in=open_step_statuses,
        ).count(),
    }


def reschedule_step_attention_tone(
    *,
    total_count: int,
    failed_count: int,
    stale_count: int,
    ready_count: int,
) -> str:
    if not total_count:
        return "success"
    nonready_count = total_count - ready_count
    if failed_count:
        return "danger"
    if stale_count or nonready_count:
        return "warning"
    return "info"


def low_balance_accounts():
    return [
        account
        for account in BalanceAccount.objects.select_related("child", "funding_source", "service")
        if account.is_low_balance
    ]


def financial_integrity_active_findings_queryset():
    severity_order = Case(
        When(severity=FinancialIntegrityFinding.Severity.ERROR, then=Value(0)),
        When(severity=FinancialIntegrityFinding.Severity.WARNING, then=Value(1)),
        When(severity=FinancialIntegrityFinding.Severity.INFO, then=Value(2)),
        default=Value(3),
        output_field=IntegerField(),
    )
    status_order = Case(
        When(status=FinancialIntegrityFinding.Status.OPEN, then=Value(0)),
        When(status=FinancialIntegrityFinding.Status.ACKNOWLEDGED, then=Value(1)),
        default=Value(2),
        output_field=IntegerField(),
    )
    return (
        FinancialIntegrityFinding.objects.filter(status__in=FINANCIAL_INTEGRITY_ACTIVE_STATUSES)
        .select_related(
            "appointment",
            "appointment__service",
            "appointment_participant",
            "appointment_participant__child",
            "ledger_entry",
            "account",
            "funding_source",
            "last_seen_run",
        )
        .annotate(severity_order=severity_order, status_order=status_order)
        .order_by("severity_order", "status_order", "-last_seen_at", "-pk")
    )


def financial_integrity_finding_tone(finding: FinancialIntegrityFinding) -> str:
    if finding.severity == FinancialIntegrityFinding.Severity.ERROR:
        return "danger"
    if finding.severity == FinancialIntegrityFinding.Severity.WARNING:
        return "warning"
    return "info"


def financial_integrity_summary(findings_queryset) -> dict[str, int | str]:
    counts = findings_queryset.aggregate(
        total=Count("pk"),
        errors=Count(
            "pk",
            filter=Q(severity=FinancialIntegrityFinding.Severity.ERROR),
        ),
        warnings=Count(
            "pk",
            filter=Q(severity=FinancialIntegrityFinding.Severity.WARNING),
        ),
        info=Count(
            "pk",
            filter=Q(severity=FinancialIntegrityFinding.Severity.INFO),
        ),
    )
    error_count = counts["errors"]
    warning_count = counts["warnings"]
    info_count = counts["info"]
    tone = "success"
    if error_count:
        tone = "danger"
    elif warning_count:
        tone = "warning"
    elif info_count:
        tone = "info"
    return {
        "total": counts["total"],
        "errors": error_count,
        "warnings": warning_count,
        "info": info_count,
        "tone": tone,
    }


def financial_integrity_latest_run():
    return (
        FinancialIntegrityCheckRun.objects.select_related("requested_by")
        .order_by("-started_at", "-pk")
        .first()
    )


def financial_integrity_run_tone(run: FinancialIntegrityCheckRun | None) -> str:
    if run is None:
        return "info"
    if run.status == FinancialIntegrityCheckRun.Status.FAILED:
        return "danger"
    if run.status == FinancialIntegrityCheckRun.Status.RUNNING:
        return "info"
    if run.issue_count:
        return "warning"
    return "success"


def financial_integrity_finding_rows(findings_queryset) -> list[dict[str, object]]:
    return [
        {
            "finding": finding,
            "tone": financial_integrity_finding_tone(finding),
            "severity_label": finding.get_severity_display(),
            "status_label": finding.get_status_display(),
        }
        for finding in findings_queryset[:FINANCIAL_INTEGRITY_DISPLAY_LIMIT]
    ]


def financial_integrity_status_label(status: str) -> str:
    if not status:
        return ""
    try:
        return FinancialIntegrityFinding.Status(status).label
    except ValueError:
        return status


def financial_integrity_event_actor_label(event: FinancialIntegrityFindingEvent) -> str:
    if event.actor_id:
        return event.actor.get_full_name() or event.actor.get_username() or str(event.actor)
    if event.run_id:
        return "Автопроверка"
    return "Система"


def financial_integrity_event_rows(
    finding: FinancialIntegrityFinding,
) -> list[dict[str, object]]:
    events = (
        FinancialIntegrityFindingEvent.objects.filter(finding=finding)
        .select_related("run", "actor")
        .order_by("-event_at", "-pk")[:FINANCIAL_INTEGRITY_EVENT_DISPLAY_LIMIT]
    )
    return [
        {
            "event": event,
            "tone": FINANCIAL_INTEGRITY_EVENT_TONES.get(event.event_type, "info"),
            "actor_label": financial_integrity_event_actor_label(event),
            "status_from_label": financial_integrity_status_label(event.status_from),
            "status_to_label": financial_integrity_status_label(event.status_to),
        }
        for event in events
    ]


def financial_integrity_finding_triage_fallback_url() -> str:
    return f"{reverse('work_queue')}#queue-financial-integrity"


def financial_integrity_finding_detail_queryset():
    return FinancialIntegrityFinding.objects.select_related(
        "appointment",
        "appointment__service",
        "appointment_participant",
        "appointment_participant__child",
        "ledger_entry",
        "account",
        "account__child",
        "account__funding_source",
        "funding_source",
        "first_seen_run",
        "last_seen_run",
        "resolved_run",
        "triaged_by",
    )


def financial_integrity_finding_detail_url(finding: FinancialIntegrityFinding) -> str:
    return reverse("financial_integrity_finding_detail", args=[finding.pk])


def financial_integrity_finding_source_items(
    finding: FinancialIntegrityFinding,
) -> list[dict[str, str]]:
    items = []
    if finding.appointment_id:
        items.append(
            {
                "label": "Занятие",
                "value": str(finding.appointment),
                "href": reverse("appointment_detail", args=[finding.appointment_id]),
            }
        )
    elif finding.appointment_starts_at or finding.appointment_service_name:
        items.append(
            {
                "label": "Занятие",
                "value": " · ".join(
                    str(value)
                    for value in [
                        finding.appointment_starts_at.strftime("%d.%m.%Y %H:%M")
                        if finding.appointment_starts_at
                        else "",
                        finding.appointment_service_name,
                    ]
                    if value
                ),
            }
        )
    if finding.appointment_participant_id:
        items.append(
            {
                "label": "Участник",
                "value": str(finding.appointment_participant.child),
            }
        )
    elif finding.participant_name:
        items.append({"label": "Участник", "value": finding.participant_name})
    if finding.account_id:
        items.append(
            {
                "label": "Счет",
                "value": str(finding.account),
                "href": reverse("balance_account_edit", args=[finding.account_id]),
            }
        )
    elif finding.account_label:
        items.append({"label": "Счет", "value": finding.account_label})
    if finding.funding_source_id:
        items.append(
            {
                "label": "Источник",
                "value": str(finding.funding_source),
                "href": reverse("funding_source_edit", args=[finding.funding_source_id]),
            }
        )
    elif finding.funding_source_name:
        items.append({"label": "Источник", "value": finding.funding_source_name})
    if finding.ledger_entry_id:
        items.append({"label": "Ledger", "value": f"#{finding.ledger_entry_id}"})
    elif finding.ledger_entry_type or finding.ledger_amount:
        items.append(
            {
                "label": "Ledger",
                "value": f"{finding.ledger_entry_type} {finding.ledger_amount}".strip(),
            }
        )
    return items


@login_required
@user_passes_test(is_admin_user)
def financial_integrity_finding_detail(request, pk: int):
    finding = get_object_or_404(financial_integrity_finding_detail_queryset(), pk=pk)
    detail_url = financial_integrity_finding_detail_url(finding)
    return render(
        request,
        "operations/financial_integrity_finding_detail.html",
        {
            "finding": finding,
            "tone": financial_integrity_finding_tone(finding),
            "source_items": financial_integrity_finding_source_items(finding),
            "event_rows": financial_integrity_event_rows(finding),
            "detail_url": detail_url,
            "work_queue_financial_url": financial_integrity_finding_triage_fallback_url(),
        },
    )


@login_required
@user_passes_test(is_admin_user)
@require_POST
def financial_integrity_finding_triage(request, pk: int):
    finding = get_object_or_404(FinancialIntegrityFinding, pk=pk)
    action = request.POST.get("action", "")
    note = request.POST.get("note", "")

    try:
        if action == "acknowledge":
            financial_integrity_triage_svc.acknowledge_finding(
                finding,
                actor=request.user,
                note=note,
            )
            messages.success(request, "Финансовое расхождение принято в работу.")
        elif action == "return_to_open":
            financial_integrity_triage_svc.return_finding_to_open(
                finding,
                actor=request.user,
                note=note,
            )
            messages.success(request, "Финансовое расхождение возвращено в очередь.")
        elif action == "ignore":
            financial_integrity_triage_svc.ignore_finding(
                finding,
                actor=request.user,
                note=note,
            )
            messages.success(request, "Финансовое расхождение скрыто из очереди.")
        elif action == "reopen":
            financial_integrity_triage_svc.reopen_finding(
                finding,
                actor=request.user,
                note=note,
            )
            messages.success(request, "Финансовое расхождение возвращено в очередь.")
        else:
            raise financial_integrity_triage_svc.FinancialIntegrityTriageError(
                "Неизвестное действие разбора."
            )
    except financial_integrity_triage_svc.FinancialIntegrityTriageError as exc:
        messages.error(request, f"Действие не выполнено: {exc}")

    return redirect(safe_next_url(request, financial_integrity_finding_triage_fallback_url()))


@login_required
@user_passes_test(is_admin_user)
@require_POST
def financial_integrity_finding_recheck(request, pk: int):
    finding = get_object_or_404(financial_integrity_finding_detail_queryset(), pk=pk)
    fallback_url = financial_integrity_finding_detail_url(finding)
    if not finding.appointment_id:
        messages.error(request, "Повторная проверка недоступна: занятие не сохранено.")
        return redirect(safe_next_url(request, fallback_url))

    try:
        status_from = finding.status
        run = financial_integrity_checks_svc.run_financial_integrity_check(
            appointments=[finding.appointment],
            requested_by=request.user,
        )
    except Exception as exc:
        messages.error(request, f"Повторная проверка не выполнена: {exc}")
    else:
        finding.refresh_from_db()
        financial_integrity_events_svc.record_finding_event(
            finding,
            event_type=FinancialIntegrityFindingEvent.EventType.SCOPED_RECHECK,
            run=run,
            actor=request.user,
            status_from=status_from,
            status_to=finding.status,
            note=f"candidate_count={run.candidate_count}; issue_count={run.issue_count}",
        )
        messages.success(
            request,
            (
                "Повторная проверка занятия завершена: "
                f"кандидатов {run.candidate_count}, расхождений {run.issue_count}."
            ),
        )
    return redirect(safe_next_url(request, fallback_url))


def quick_billing_account(appointment):
    participants = list(appointment.participants.all())
    if len(participants) == 1:
        return participants[0].billing_account
    if not participants:
        return appointment.billing_account
    return None


def attendance_summary_label(appointment):
    participants = list(appointment.participants.all())
    if not participants:
        return appointment.get_attendance_status_display()
    if len(participants) == 1:
        return participants[0].get_attendance_status_display()
    unknown = sum(
        1
        for participant in participants
        if participant.attendance_status == Appointment.AttendanceStatus.UNKNOWN
    )
    if unknown:
        return f"не отмечено участников: {unknown} из {len(participants)}"
    attended = sum(
        1
        for participant in participants
        if participant.attendance_status == Appointment.AttendanceStatus.ATTENDED
    )
    missed = sum(
        1
        for participant in participants
        if participant.attendance_status == Appointment.AttendanceStatus.MISSED
    )
    return f"пришли {attended}, не пришли {missed}"


def dashboard_focus_items(
    *,
    unresolved_billing: int,
    overdue_attendance: int,
    awaiting_transfer: int,
    confirmation_tasks: int,
    time_off_requests: int,
    low_balance_count: int,
    ready_chain_count: int,
    stale_chain_count: int,
    failed_chain_count: int,
    reschedule_step_count: int,
    ready_step_count: int,
    stale_step_count: int,
    failed_step_count: int,
    financial_integrity_count: int,
    financial_integrity_tone: str,
):
    queue_url = reverse("work_queue")
    items = []
    if unresolved_billing:
        items.append(
            {
                "tone": "warning",
                "value": unresolved_billing,
                "title": "Решить списания",
                "detail": "Есть занятия без решения по оплате или участникам.",
                "href": queue_url,
            }
        )
    if financial_integrity_count:
        items.append(
            {
                "tone": financial_integrity_tone,
                "value": financial_integrity_count,
                "title": "Проверить финансы",
                "detail": "Есть расхождения между списаниями, участниками и ledger.",
                "href": f"{queue_url}#queue-financial-integrity",
            }
        )
    if overdue_attendance:
        items.append(
            {
                "tone": "warning",
                "value": overdue_attendance,
                "title": "Отметить факт",
                "detail": "Прошедшие занятия ждут отметки посещения.",
                "href": queue_url,
            }
        )
    if awaiting_transfer:
        items.append(
            {
                "tone": "danger",
                "value": awaiting_transfer,
                "title": "Перенести отмены",
                "detail": "Отмененные занятия еще не связаны с новым временем.",
                "href": queue_url,
            }
        )
    if failed_chain_count:
        items.append(
            {
                "tone": "danger",
                "value": failed_chain_count,
                "title": "Ошибки цепочек переноса",
                "detail": "Атомарные переносы не применились или требуют ручной проверки перед повтором.",
                "href": reschedule_plan_focus_url("chain_failed"),
            }
        )
    if stale_chain_count:
        items.append(
            {
                "tone": "warning",
                "value": stale_chain_count,
                "title": "Проверить цепочки",
                "detail": "Расписание изменилось после подготовки цепочки; нужна перепроверка.",
                "href": reschedule_plan_focus_url("chain_stale"),
            }
        )
    if ready_chain_count:
        items.append(
            {
                "tone": "info",
                "value": ready_chain_count,
                "title": "Применить цепочки",
                "detail": "Есть готовые атомарные переносы без свободных окон.",
                "href": reschedule_plan_focus_url("chain_ready"),
            }
        )
    if reschedule_step_count:
        items.append(
            {
                "tone": reschedule_step_attention_tone(
                    total_count=reschedule_step_count,
                    failed_count=failed_step_count,
                    stale_count=stale_step_count,
                    ready_count=ready_step_count,
                ),
                "value": reschedule_step_count,
                "title": "Разобрать шаги переноса",
                "detail": "Одиночные шаги планов переноса ждут проверки, согласования или применения.",
                "href": f"{queue_url}#queue-reschedule-steps",
            }
        )
    if confirmation_tasks:
        items.append(
            {
                "tone": "info",
                "value": confirmation_tasks,
                "title": "Проверить согласования",
                "detail": "Есть ожидающие, отклоненные или неотправленные подтверждения.",
                "href": queue_url,
            }
        )
    if time_off_requests:
        items.append(
            {
                "tone": "info",
                "value": time_off_requests,
                "title": "Разобрать отгулы",
                "detail": "Специалисты ждут решения по отсутствию.",
                "href": queue_url,
            }
        )
    if low_balance_count:
        items.append(
            {
                "tone": "warning",
                "value": low_balance_count,
                "title": "Пополнить балансы",
                "detail": "Есть счета с низким остатком.",
                "href": reverse("balances"),
            }
        )
    if not items:
        items.append(
            {
                "tone": "success",
                "value": 0,
                "title": "Критичных задач нет",
                "detail": "Проверьте календарь и подготовку завтрашнего дня.",
                "href": reverse("schedule"),
            }
        )
    return items


def work_queue_summary_items(
    *,
    needs_billing_count: int,
    needs_attendance_count: int,
    needs_transfer_count: int,
    low_balance_count: int,
    confirmation_count: int,
    time_off_count: int,
    ready_chain_count: int,
    stale_chain_count: int,
    failed_chain_count: int,
    reschedule_step_count: int,
    failed_step_count: int,
    stale_step_count: int,
    nonready_step_count: int,
    financial_integrity_count: int,
    financial_integrity_tone: str,
):
    chain_attention_count = ready_chain_count + stale_chain_count + failed_chain_count
    chain_tone = "success"
    if failed_chain_count:
        chain_tone = "danger"
    elif stale_chain_count:
        chain_tone = "warning"
    elif ready_chain_count:
        chain_tone = "info"

    step_tone = reschedule_step_attention_tone(
        total_count=reschedule_step_count,
        failed_count=failed_step_count,
        stale_count=stale_step_count,
        ready_count=reschedule_step_count - nonready_step_count,
    )

    return [
        {
            "label": "Решения по списанию",
            "value": needs_billing_count,
            "href": "#queue-billing",
            "tone": "warning" if needs_billing_count else "success",
            "detail": "Списать, не списывать или решить по участникам.",
        },
        {
            "label": "Финансовый контроль",
            "value": financial_integrity_count,
            "href": "#queue-financial-integrity",
            "tone": financial_integrity_tone if financial_integrity_count else "success",
            "detail": "Расхождения между списаниями, участниками и ledger.",
        },
        {
            "label": "Факт посещения",
            "value": needs_attendance_count,
            "href": "#queue-attendance",
            "tone": "warning" if needs_attendance_count else "success",
            "detail": "Прошедшие занятия без отметки администратора.",
        },
        {
            "label": "Переносы",
            "value": needs_transfer_count,
            "href": "#queue-transfer",
            "tone": "danger" if needs_transfer_count else "success",
            "detail": "Отмененные занятия без нового времени.",
        },
        {
            "label": "Цепочки переносов",
            "value": chain_attention_count,
            "href": "#queue-reschedule-chains",
            "tone": chain_tone,
            "detail": "Готовые, устаревшие или проблемные атомарные переносы.",
        },
        {
            "label": "Шаги планов переноса",
            "value": reschedule_step_count,
            "href": "#queue-reschedule-steps",
            "tone": step_tone,
            "detail": "Одиночные шаги переноса без цепочки, где нужно решение администратора.",
        },
        {
            "label": "Низкие балансы",
            "value": low_balance_count,
            "href": "#queue-balances",
            "tone": "warning" if low_balance_count else "success",
            "detail": "Счета, где скоро нечем будет списывать занятия.",
        },
        {
            "label": "Email-согласования",
            "value": confirmation_count,
            "href": "#queue-confirmations",
            "tone": "info" if confirmation_count else "success",
            "detail": "Ожидают ответа, отклонены или не отправлены.",
        },
        {
            "label": "Заявки специалистов",
            "value": time_off_count,
            "href": "#queue-time-off",
            "tone": "info" if time_off_count else "success",
            "detail": "Отпуска, отгулы и другие отсутствия.",
        },
    ]


def work_queue_next_action(summary_items: list[dict[str, object]]) -> dict[str, object]:
    for item in summary_items:
        if item["value"]:
            return {
                "label": "Следующее действие",
                "title": item["label"],
                "value": item["value"],
                "detail": item["detail"],
                "href": item["href"],
                "tone": item["tone"],
            }
    return {
        "label": "Следующее действие",
        "title": "Критичных задач нет",
        "value": 0,
        "detail": "Можно проверить календарь или подготовку завтрашнего дня.",
        "href": reverse("schedule"),
        "tone": "success",
    }


@login_required
def dashboard(request):
    if not request.user.is_staff:
        return redirect("specialist_home")

    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)
    unresolved_billing = needs_billing_queryset().count()
    awaiting_transfer = needs_transfer_queryset().count()
    overdue_attendance = needs_attendance_queryset().count()
    confirmation_tasks = confirmation_attention_queryset().count()
    time_off_requests = TimeOffRequest.objects.filter(status=TimeOffRequest.Status.PENDING).count()
    chain_counts = reschedule_chain_attention_counts()
    chain_attention_count = chain_counts["ready"] + chain_counts["stale"] + chain_counts["failed"]
    step_counts = reschedule_step_attention_counts()
    reschedule_step_count = step_counts["total"]
    financial_findings = financial_integrity_active_findings_queryset()
    financial_summary = financial_integrity_summary(financial_findings)
    priority_total = (
        unresolved_billing
        + awaiting_transfer
        + overdue_attendance
        + confirmation_tasks
        + time_off_requests
        + chain_attention_count
        + reschedule_step_count
        + int(financial_summary["total"])
    )
    today_appointments = (
        Appointment.objects.filter(starts_at__date=today)
        .select_related("child", "staff_member", "service", "room")
        .prefetch_related("participants__child", "staff_assignments__staff_member")
        .order_by("starts_at")
    )
    tomorrow_appointments = (
        Appointment.objects.filter(starts_at__date=tomorrow)
        .select_related("child", "staff_member", "service", "room")
        .prefetch_related("participants__child", "staff_assignments__staff_member")
        .order_by("starts_at")
    )
    staff_load = (
        AppointmentStaffAssignment.objects.filter(
            starts_at_snapshot__date__gte=today,
            starts_at_snapshot__date__lt=today + timedelta(days=14),
        )
        .values("staff_member__full_name")
        .annotate(total=Count("appointment_id", distinct=True))
        .order_by("staff_member__full_name")
    )
    low_balances = low_balance_accounts()
    dashboard_focus = dashboard_focus_items(
        unresolved_billing=unresolved_billing,
        overdue_attendance=overdue_attendance,
        awaiting_transfer=awaiting_transfer,
        confirmation_tasks=confirmation_tasks,
        time_off_requests=time_off_requests,
        low_balance_count=len(low_balances),
        ready_chain_count=chain_counts["ready"],
        stale_chain_count=chain_counts["stale"],
        failed_chain_count=chain_counts["failed"],
        reschedule_step_count=reschedule_step_count,
        ready_step_count=step_counts["ready"],
        stale_step_count=step_counts["stale"],
        failed_step_count=step_counts["failed"],
        financial_integrity_count=int(financial_summary["total"]),
        financial_integrity_tone=str(financial_summary["tone"]),
    )
    return render(
        request,
        "operations/dashboard.html",
        {
            "today": today,
            "tomorrow": tomorrow,
            "today_appointments": today_appointments,
            "unresolved_billing": unresolved_billing,
            "awaiting_transfer": awaiting_transfer,
            "overdue_attendance": overdue_attendance,
            "confirmation_tasks": confirmation_tasks,
            "time_off_requests": time_off_requests,
            "ready_chain_count": chain_counts["ready"],
            "stale_chain_count": chain_counts["stale"],
            "failed_chain_count": chain_counts["failed"],
            "chain_attention_count": chain_attention_count,
            "reschedule_step_count": reschedule_step_count,
            "ready_step_count": step_counts["ready"],
            "stale_step_count": step_counts["stale"],
            "failed_step_count": step_counts["failed"],
            "priority_total": priority_total,
            "financial_integrity_count": financial_summary["total"],
            "financial_integrity_error_count": financial_summary["errors"],
            "financial_integrity_warning_count": financial_summary["warnings"],
            "financial_integrity_info_count": financial_summary["info"],
            "financial_integrity_tone": financial_summary["tone"],
            "staff_load": staff_load,
            "low_balances": low_balances,
            "tomorrow_appointments": tomorrow_appointments,
            "dashboard_focus_items": dashboard_focus,
        },
    )


@login_required
@user_passes_test(is_admin_user)
def work_queue(request):
    needs_billing = list(needs_billing_queryset()[:40])
    for appointment in needs_billing:
        appointment.attendance_summary_label = attendance_summary_label(appointment)
        appointment.quick_billing_account = quick_billing_account(appointment)
        appointment.quick_charge_amount = (
            default_charge_amount(appointment.quick_billing_account, appointment)
            if appointment.quick_billing_account
            else None
        )
    needs_attendance = list(needs_attendance_queryset()[:40])
    for appointment in needs_attendance:
        appointment.attendance_summary_label = attendance_summary_label(appointment)
    needs_transfer = list(needs_transfer_queryset()[:40])
    for appointment in needs_transfer:
        appointment.attendance_summary_label = attendance_summary_label(appointment)
    confirmation_tasks = (
        confirmation_attention_queryset()
        .select_related(
            "appointment",
            "appointment__child",
            "appointment__staff_member",
            "appointment__service",
            "participant__child",
            "staff_assignment__staff_member",
            "reschedule_step",
            "reschedule_step__plan",
        )
        .prefetch_related("appointment__participants__child")
        .order_by("status", "-created_at")[:40]
    )
    time_off_requests = (
        TimeOffRequest.objects.filter(status=TimeOffRequest.Status.PENDING)
        .select_related("staff_member")
        .order_by("starts_on", "staff_member__full_name")[:40]
    )
    reschedule_chains_queryset = reschedule_chain_attention_queryset()
    chain_counts = reschedule_chain_attention_counts(reschedule_chains_queryset)
    reschedule_chains = list(reschedule_chains_queryset[:40])
    reschedule_steps_queryset = reschedule_step_attention_queryset()
    step_counts = reschedule_step_attention_counts(reschedule_steps_queryset)
    reschedule_steps = list(reschedule_steps_queryset[:40])
    low_balances = low_balance_accounts()
    financial_findings = financial_integrity_active_findings_queryset()
    financial_summary = financial_integrity_summary(financial_findings)
    financial_issue_rows = financial_integrity_finding_rows(financial_findings)
    financial_latest_run = financial_integrity_latest_run()
    queue_summary = work_queue_summary_items(
        needs_billing_count=len(needs_billing),
        needs_attendance_count=len(needs_attendance),
        needs_transfer_count=len(needs_transfer),
        low_balance_count=len(low_balances),
        confirmation_count=len(confirmation_tasks),
        time_off_count=len(time_off_requests),
        ready_chain_count=chain_counts["ready"],
        stale_chain_count=chain_counts["stale"],
        failed_chain_count=chain_counts["failed"],
        reschedule_step_count=step_counts["total"],
        failed_step_count=step_counts["failed"],
        stale_step_count=step_counts["stale"],
        nonready_step_count=step_counts["total"] - step_counts["ready"],
        financial_integrity_count=int(financial_summary["total"]),
        financial_integrity_tone=str(financial_summary["tone"]),
    )
    return render(
        request,
        "operations/work_queue.html",
        {
            "needs_billing": needs_billing,
            "needs_attendance": needs_attendance,
            "needs_transfer": needs_transfer,
            "low_balances": low_balances,
            "financial_integrity_issues": financial_issue_rows,
            "financial_integrity_issue_count": financial_summary["total"],
            "financial_integrity_error_count": financial_summary["errors"],
            "financial_integrity_warning_count": financial_summary["warnings"],
            "financial_integrity_info_count": financial_summary["info"],
            "financial_integrity_next_url": f"{request.get_full_path()}#queue-financial-integrity",
            "financial_integrity_latest_run": financial_latest_run,
            "financial_integrity_latest_run_tone": financial_integrity_run_tone(
                financial_latest_run
            ),
            "confirmation_tasks": confirmation_tasks,
            "time_off_requests": time_off_requests,
            "reschedule_chains": reschedule_chains,
            "reschedule_steps": reschedule_steps,
            "ready_chain_count": chain_counts["ready"],
            "stale_chain_count": chain_counts["stale"],
            "failed_chain_count": chain_counts["failed"],
            "reschedule_step_count": step_counts["total"],
            "failed_step_count": step_counts["failed"],
            "stale_step_count": step_counts["stale"],
            "queue_summary_items": queue_summary,
            "queue_next_action": work_queue_next_action(queue_summary),
        },
    )
