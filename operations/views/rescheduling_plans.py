from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentReschedulePlan,
    AppointmentRescheduleStep,
)
from operations.services import rescheduling_plans as plan_svc
from operations.tasks import send_appointment_confirmation_email

from ._common import is_admin_user

METRICS_PERIOD_CHOICES = (
    ("7", "7 дней"),
    ("30", "30 дней"),
    ("90", "90 дней"),
    ("all", "Все время"),
)


def _metrics_period(raw_period: str) -> tuple[str, str, object | None]:
    labels = dict(METRICS_PERIOD_CHOICES)
    if raw_period not in labels:
        raw_period = "30"
    if raw_period == "all":
        return raw_period, labels[raw_period], None
    days = int(raw_period)
    return raw_period, labels[raw_period], timezone.now() - timedelta(days=days)


def _apply_since_filter(queryset, since, field_name: str):
    if since is None:
        return queryset
    return queryset.filter(**{f"{field_name}__gte": since})


def _reschedule_metric_items(since) -> list[dict]:
    created_plans = _apply_since_filter(
        AppointmentReschedulePlan.objects.all(), since, "created_at"
    )
    applied_plans = _apply_since_filter(
        AppointmentReschedulePlan.objects.filter(
            status=AppointmentReschedulePlan.Status.APPLIED
        ),
        since,
        "applied_at",
    )
    cancelled_plans = _apply_since_filter(
        AppointmentReschedulePlan.objects.filter(
            status=AppointmentReschedulePlan.Status.CANCELLED
        ),
        since,
        "cancelled_at",
    )
    created_steps = _apply_since_filter(
        AppointmentRescheduleStep.objects.all(), since, "plan__created_at"
    )
    review_plans = created_plans.filter(
        steps__action_type=AppointmentRescheduleStep.ActionType.REVIEW_CONFLICT
    ).distinct()
    return [
        {
            "label": "Создано",
            "value": created_plans.count(),
            "hint": "новые планы переноса",
        },
        {
            "label": "Шагов",
            "value": created_steps.count(),
            "hint": "запланированные действия",
        },
        {
            "label": "Применено",
            "value": applied_plans.count(),
            "hint": "закрыто применением",
        },
        {
            "label": "Отменено",
            "value": cancelled_plans.count(),
            "hint": "закрыто без переноса",
        },
        {
            "label": "Отсутствия",
            "value": created_plans.filter(
                plan_type=AppointmentReschedulePlan.PlanType.STAFF_ABSENCE
            ).count(),
            "hint": "планы по специалистам",
        },
        {
            "label": "Ручной разбор",
            "value": review_plans.count(),
            "hint": "планы с конфликтами",
        },
    ]


@login_required
@user_passes_test(is_admin_user)
def appointment_reschedule_plan_create(request, pk: int):
    appointment = get_object_or_404(Appointment, pk=pk)
    if request.method != "POST":
        return redirect("appointment_move", pk=appointment.pk)

    plan = plan_svc.create_plan_for_appointment(appointment, actor=request.user)
    if plan.steps.exists():
        messages.success(request, "План переноса сохранен. Проверьте шаги перед применением.")
    else:
        messages.warning(
            request,
            "План создан, но подходящих шагов не найдено. Измените диапазон или расписание.",
        )
    return redirect("appointment_reschedule_plan_detail", pk=plan.pk)


@login_required
@user_passes_test(is_admin_user)
def reschedule_plan_list(request):
    status = request.GET.get("status", "active")
    confirmation = request.GET.get("confirmation", "")
    focus = request.GET.get("focus", "")
    metrics_period, metrics_period_label, metrics_since = _metrics_period(
        request.GET.get("metrics_period", "30")
    )
    confirmation_status = AppointmentRescheduleStep.ConfirmationStatus
    step_status = AppointmentRescheduleStep.Status
    action_type = AppointmentRescheduleStep.ActionType
    open_step_statuses = [
        step_status.PENDING,
        step_status.VALID,
        step_status.STALE,
        step_status.FAILED,
    ]
    active_step_filter = Q(steps__status__in=open_step_statuses)
    plans = AppointmentReschedulePlan.objects.select_related(
        "root_appointment",
        "root_appointment__child",
        "root_appointment__service",
        "root_appointment__staff_member",
        "staff_member",
        "created_by",
    ).annotate(
        step_count=Count("steps", distinct=True),
        waiting_step_count=Count(
            "steps",
            filter=Q(steps__confirmation_status=confirmation_status.WAITING),
            distinct=True,
        ),
        approved_step_count=Count(
            "steps",
            filter=Q(steps__confirmation_status=confirmation_status.APPROVED),
            distinct=True,
        ),
        declined_step_count=Count(
            "steps",
            filter=Q(steps__confirmation_status=confirmation_status.DECLINED),
            distinct=True,
        ),
        manual_review_step_count=Count(
            "steps",
            filter=Q(steps__action_type=action_type.REVIEW_CONFLICT) & active_step_filter,
            distinct=True,
        ),
        stale_step_count=Count(
            "steps",
            filter=Q(steps__status=step_status.STALE),
            distinct=True,
        ),
        failed_step_count=Count(
            "steps",
            filter=Q(steps__status=step_status.FAILED),
            distinct=True,
        ),
        ready_step_count=Count(
            "steps",
            filter=Q(steps__status=step_status.VALID)
            & Q(
                steps__confirmation_status__in=[
                    confirmation_status.NOT_REQUESTED,
                    confirmation_status.APPROVED,
                ]
            ),
            distinct=True,
        ),
    )
    if status == "active":
        plans = plans.exclude(
            status__in=[
                AppointmentReschedulePlan.Status.APPLIED,
                AppointmentReschedulePlan.Status.CANCELLED,
            ]
        )
    elif status in AppointmentReschedulePlan.Status.values:
        plans = plans.filter(status=status)

    if confirmation in confirmation_status.values:
        plans = plans.filter(steps__confirmation_status=confirmation).distinct()

    if focus == "manual_review":
        plans = plans.filter(
            steps__action_type=action_type.REVIEW_CONFLICT,
            steps__status__in=open_step_statuses,
        )
    elif focus == "stale":
        plans = plans.filter(steps__status=step_status.STALE)
    elif focus == "failed":
        plans = plans.filter(steps__status=step_status.FAILED)
    elif focus == "waiting":
        plans = plans.filter(steps__confirmation_status=confirmation_status.WAITING)
    elif focus == "declined":
        plans = plans.filter(steps__confirmation_status=confirmation_status.DECLINED)
    elif focus == "ready_to_apply":
        plans = plans.filter(
            steps__status=step_status.VALID,
            steps__confirmation_status__in=[
                confirmation_status.NOT_REQUESTED,
                confirmation_status.APPROVED,
            ],
        )

    active_plans = AppointmentReschedulePlan.objects.exclude(
        status__in=[
            AppointmentReschedulePlan.Status.APPLIED,
            AppointmentReschedulePlan.Status.CANCELLED,
        ]
    )
    summary_items = [
        {
            "label": "Активные",
            "value": active_plans.count(),
            "hint": "готовы к проверке или требуют решения",
        },
        {
            "label": "Ручной разбор",
            "value": active_plans.filter(
                steps__action_type=action_type.REVIEW_CONFLICT,
                steps__status__in=open_step_statuses,
            )
            .distinct()
            .count(),
            "hint": "конфликты без автоматического решения",
        },
        {
            "label": "Ждут ответы",
            "value": active_plans.filter(
                steps__confirmation_status=confirmation_status.WAITING
            )
            .distinct()
            .count(),
            "hint": "есть отправленные согласования",
        },
        {
            "label": "Есть отказ",
            "value": active_plans.filter(
                steps__confirmation_status=confirmation_status.DECLINED
            )
            .distinct()
            .count(),
            "hint": "нужно ручное решение",
        },
        {
            "label": "Устарели",
            "value": active_plans.filter(steps__status=step_status.STALE)
            .distinct()
            .count(),
            "hint": "нужна перепроверка плана",
        },
        {
            "label": "Можно применять",
            "value": active_plans.filter(
                steps__status=step_status.VALID,
                steps__confirmation_status__in=[
                    confirmation_status.NOT_REQUESTED,
                    confirmation_status.APPROVED,
                ],
            )
            .distinct()
            .count(),
            "hint": "есть валидный шаг без блокирующих ответов",
        },
    ]
    focus_choices = [
        ("", "Все"),
        ("manual_review", "Ручной разбор"),
        ("stale", "Устарели"),
        ("failed", "Ошибки"),
        ("waiting", "Ждут ответы"),
        ("declined", "Есть отказ"),
        ("ready_to_apply", "Можно применять"),
    ]
    return render(
        request,
        "operations/reschedule_plan_list.html",
        {
            "plans": plans.order_by("-created_at", "-pk")[:100],
            "current_status": status,
            "current_confirmation": confirmation,
            "current_focus": focus,
            "status_choices": AppointmentReschedulePlan.Status.choices,
            "confirmation_choices": confirmation_status.choices,
            "focus_choices": focus_choices,
            "summary_items": summary_items,
            "metric_items": _reschedule_metric_items(metrics_since),
            "metrics_period_choices": METRICS_PERIOD_CHOICES,
            "current_metrics_period": metrics_period,
            "metrics_period_label": metrics_period_label,
        },
    )


@login_required
@user_passes_test(is_admin_user)
def appointment_reschedule_plan_detail(request, pk: int):
    plan = get_object_or_404(
        AppointmentReschedulePlan.objects.select_related(
            "root_appointment",
            "root_appointment__child",
            "root_appointment__staff_member",
            "root_appointment__service",
            "staff_member",
            "created_by",
            "applied_by",
            "cancelled_by",
        ).prefetch_related(
            "steps__source_appointment",
            "steps__blocking_appointment",
            "steps__created_appointment",
            "steps__proposed_primary_staff",
            "steps__proposed_room",
        ),
        pk=pk,
    )
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "revalidate":
            result = plan_svc.revalidate_plan(plan)
            messages.success(
                request,
                (
                    "План перепроверен: "
                    f"готово {result.valid_steps}, устарело {result.stale_steps}, "
                    f"ожидает решения {result.pending_steps}."
                ),
            )
            return redirect("appointment_reschedule_plan_detail", pk=plan.pk)
        if action == "revalidate_chain":
            chain = get_object_or_404(plan.chains.all(), pk=request.POST.get("chain_id"))
            try:
                result = plan_svc.revalidate_chain(chain)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                messages.success(
                    request,
                    (
                        "Chain revalidated: "
                        f"ready {result.ready_steps}, stale {result.stale_steps}, "
                        f"blocked {result.blocked_steps}."
                    ),
                )
            return redirect("appointment_reschedule_plan_detail", pk=plan.pk)
        if action == "apply_chain":
            chain = get_object_or_404(plan.chains.all(), pk=request.POST.get("chain_id"))
            try:
                result = plan_svc.apply_chain(chain, actor=request.user)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                messages.success(
                    request,
                    f"Chain applied: {len(result.applied_steps)} steps.",
                )
            return redirect("appointment_reschedule_plan_detail", pk=plan.pk)
        if action == "apply_step":
            step = get_object_or_404(plan.steps.all(), pk=request.POST.get("step_id"))
            allow_room_override = request.POST.get("allow_room_override") == "1"
            try:
                applied_step = plan_svc.apply_step(
                    step,
                    actor=request.user,
                    allow_room_override=allow_room_override,
                )
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                if allow_room_override:
                    messages.success(
                        request,
                        "Шаг переноса применен с одноразовым разрешением кабинета.",
                    )
                elif (
                    applied_step.confirmation_status
                    == applied_step.ConfirmationStatus.APPROVED
                ):
                    messages.success(request, "Согласованный шаг переноса применен.")
                else:
                    messages.success(request, "Шаг переноса применен без согласований.")
            return redirect("appointment_reschedule_plan_detail", pk=plan.pk)
        if action == "mark_review_conflict_resolved":
            step = get_object_or_404(plan.steps.all(), pk=request.POST.get("step_id"))
            try:
                plan_svc.mark_review_conflict_step_resolved(step, actor=request.user)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                messages.success(request, "Ручной конфликт отмечен разобранным.")
            return redirect("appointment_reschedule_plan_detail", pk=plan.pk)
        if action == "send_step_confirmations":
            step = get_object_or_404(plan.steps.all(), pk=request.POST.get("step_id"))
            try:
                result = plan_svc.create_confirmations_for_step(step, actor=request.user)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages))
            else:
                for confirmation in result.created:
                    send_appointment_confirmation_email.enqueue(confirmation.pk)
                if result.created:
                    messages.success(
                        request,
                        f"Создано и поставлено в очередь писем: {len(result.created)}.",
                    )
                elif result.existing:
                    messages.info(request, "Согласования по этому шагу уже были созданы.")
                else:
                    messages.warning(request, "Для этого шага нет адресатов с email.")
            return redirect("appointment_reschedule_plan_detail", pk=plan.pk)

    chains = list(
        plan.chains.prefetch_related(
            "steps__source_appointment",
            "steps__proposed_primary_staff",
            "steps__proposed_room",
            "dependencies__predecessor_step",
            "dependencies__successor_step",
        ).order_by("created_at", "pk")
    )
    for chain in chains:
        chain.ordered_steps = list(
            chain.steps.select_related(
                "source_appointment",
                "proposed_primary_staff",
                "proposed_room",
            ).order_by("chain_position", "position", "pk")
        )
        chain.dependency_rows = list(
            chain.dependencies.select_related(
                "predecessor_step",
                "successor_step",
            ).order_by(
                "predecessor_step__chain_position",
                "successor_step__chain_position",
            )
        )

    return render(
        request,
        "operations/reschedule_plan_detail.html",
        {
            "plan": plan,
            "chains": chains,
            "steps": plan.steps.select_related(
                "source_appointment",
                "blocking_appointment",
                "created_appointment",
                "proposed_primary_staff",
                "proposed_room",
            )
            .prefetch_related("confirmations")
            .order_by("position"),
        },
    )
