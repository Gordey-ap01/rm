from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.exceptions import ValidationError
from django.db.models import Case, Count, IntegerField, Prefetch, Q, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from operations.models import (
    Appointment,
    AppointmentRescheduleChain,
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

CHAIN_ISSUE_LABELS = {
    "terminal_plan": "План уже завершен или отменен",
    "not_enough_steps": "В цепочке меньше двух шагов",
    "missing_dependencies": "Не заданы зависимости шагов",
    "duplicate_source_appointment": "Есть альтернативы одного исходного занятия",
    "step_plan_mismatch": "Шаг относится к другому плану",
    "step_not_move": "В цепочке есть шаг не переноса",
    "terminal_step": "Шаг уже применен или пропущен",
    "missing_proposed_fields": "Не заполнено новое окно шага",
    "dependency_plan_mismatch": "Зависимость относится к другому плану",
    "self_dependency": "Шаг зависит от самого себя",
    "dependency_step_outside_chain": "Зависимость указывает вне цепочки",
    "duplicate_dependency": "Зависимость указана повторно",
    "disconnected_steps": "Не все шаги связаны зависимостями",
    "dependency_cycle": "В зависимостях есть цикл",
    "confirmation_blocked": "Согласование блокирует шаг",
}

CONFIRMATION_STATUS_HINTS = {
    AppointmentRescheduleStep.ConfirmationStatus.WAITING: "ждет ответа",
    AppointmentRescheduleStep.ConfirmationStatus.DECLINED: "есть отказ",
    AppointmentRescheduleStep.ConfirmationStatus.APPROVED: "согласовано",
    AppointmentRescheduleStep.ConfirmationStatus.NOT_REQUESTED: "не запрошено",
}

CHAIN_ACTION_ERROR_MESSAGES = {
    "Chain must be ready before applying.": "Цепочка должна быть готова перед применением.",
    "Chain is not ready after revalidation.": "Цепочка устарела после финальной проверки.",
    "Chain apply order is incomplete.": "Порядок применения цепочки неполный.",
}


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
    created_chains = _apply_since_filter(
        AppointmentRescheduleChain.objects.all(), since, "created_at"
    )
    applied_plans = _apply_since_filter(
        AppointmentReschedulePlan.objects.filter(
            status=AppointmentReschedulePlan.Status.APPLIED
        ),
        since,
        "applied_at",
    )
    applied_chains = _apply_since_filter(
        AppointmentRescheduleChain.objects.filter(
            status=AppointmentRescheduleChain.Status.APPLIED
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
        {
            "label": "Цепочек",
            "value": created_chains.count(),
            "hint": "зависимые переносы",
        },
        {
            "label": "Применено цепочек",
            "value": applied_chains.count(),
            "hint": "атомарно закрыты",
        },
    ]


def _localized_messages(error: ValidationError) -> str:
    return "; ".join(
        CHAIN_ACTION_ERROR_MESSAGES.get(message, message) for message in error.messages
    )


def _chain_issue_rows(chain: AppointmentRescheduleChain) -> list[dict]:
    summary = chain.validation_summary or {}
    steps_by_id = {step.pk: step for step in getattr(chain, "ordered_steps", [])}
    rows: list[dict] = []

    apply_errors = summary.get("apply_error") or []
    if isinstance(apply_errors, str):
        apply_errors = [apply_errors]
    for message in apply_errors:
        rows.append(
            {
                "tone": "danger",
                "label": "Ошибка применения",
                "title": "Ошибка применения цепочки",
                "detail": str(message),
            }
        )

    for issue in summary.get("issues", []):
        if not isinstance(issue, dict):
            continue
        code = issue.get("code", "")
        step = steps_by_id.get(issue.get("step_id"))
        detail_parts = []
        if step:
            detail_parts.append(f"шаг {step.chain_position or step.position}")
        if issue.get("confirmation_status"):
            status = CONFIRMATION_STATUS_HINTS.get(issue["confirmation_status"])
            if status:
                detail_parts.append(f"статус: {status}")
        if issue.get("fields"):
            detail_parts.append("поля: " + ", ".join(issue["fields"]))
        if not detail_parts and issue.get("message") and code not in CHAIN_ISSUE_LABELS:
            detail_parts.append(str(issue["message"]))
        rows.append(
            {
                "tone": "danger" if code in {"terminal_plan", "dependency_cycle"} else "warning",
                "label": code or "проверка",
                "title": CHAIN_ISSUE_LABELS.get(code, "Проверьте цепочку"),
                "detail": " · ".join(detail_parts) or "откройте шаги и зависимости цепочки",
            }
        )
    return rows


def _chain_stale_step_rows(chain: AppointmentRescheduleChain) -> list[dict]:
    rows: list[dict] = []
    for step in getattr(chain, "ordered_steps", []):
        messages = step.validation_messages or []
        if not messages:
            continue
        rows.append(
            {
                "tone": "warning",
                "label": f"Шаг {step.chain_position or step.position}",
                "title": "Проверка расписания устарела",
                "detail": "; ".join(messages),
            }
        )
    return rows


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
    chain_status = AppointmentRescheduleChain.Status
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
        chain_count=Count("chains", distinct=True),
        ready_chain_count=Count(
            "chains",
            filter=Q(chains__status=chain_status.READY),
            distinct=True,
        ),
        stale_chain_count=Count(
            "chains",
            filter=Q(chains__status=chain_status.STALE),
            distinct=True,
        ),
        failed_chain_count=Count(
            "chains",
            filter=Q(chains__status=chain_status.FAILED),
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
    elif focus == "chain_ready":
        plans = plans.filter(chains__status=chain_status.READY)
    elif focus == "chain_stale":
        plans = plans.filter(chains__status=chain_status.STALE)
    elif focus == "chain_failed":
        plans = plans.filter(chains__status=chain_status.FAILED)

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
        {
            "label": "Цепочки",
            "value": active_plans.filter(chains__isnull=False).distinct().count(),
            "hint": "планы с зависимыми переносами",
        },
        {
            "label": "Готовые цепочки",
            "value": active_plans.filter(chains__status=chain_status.READY)
            .distinct()
            .count(),
            "hint": "можно применить атомарно",
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
        ("chain_ready", "Цепочки готовы"),
        ("chain_stale", "Цепочки устарели"),
        ("chain_failed", "Ошибки цепочек"),
    ]
    chain_attention_queryset = (
        AppointmentRescheduleChain.objects.filter(
            status__in=[
                chain_status.FAILED,
                chain_status.STALE,
                chain_status.READY,
            ]
        )
        .annotate(
            attention_priority=Case(
                When(status=chain_status.FAILED, then=Value(0)),
                When(status=chain_status.STALE, then=Value(1)),
                When(status=chain_status.READY, then=Value(2)),
                default=Value(9),
                output_field=IntegerField(),
            )
        )
        .order_by("attention_priority", "-updated_at", "-pk")
    )
    plan_rows = list(
        plans.order_by("-created_at", "-pk")
        .prefetch_related(
            Prefetch("chains", queryset=chain_attention_queryset, to_attr="attention_chains")
        )[:100]
    )
    for plan in plan_rows:
        plan.primary_attention_chain = (
            plan.attention_chains[0] if plan.attention_chains else None
        )

    return render(
        request,
        "operations/reschedule_plan_list.html",
        {
            "plans": plan_rows,
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
                messages.error(request, _localized_messages(exc))
            else:
                messages.success(
                    request,
                    (
                        "Цепочка перепроверена: "
                        f"готово {result.ready_steps}, устарело {result.stale_steps}, "
                        f"заблокировано {result.blocked_steps}."
                    ),
                )
            return redirect("appointment_reschedule_plan_detail", pk=plan.pk)
        if action == "apply_chain":
            chain = get_object_or_404(plan.chains.all(), pk=request.POST.get("chain_id"))
            try:
                result = plan_svc.apply_chain(chain, actor=request.user)
            except ValidationError as exc:
                messages.error(request, _localized_messages(exc))
            else:
                messages.success(
                    request,
                    f"Цепочка применена: шагов {len(result.applied_steps)}.",
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
        chain.issue_rows = _chain_issue_rows(chain)
        chain.stale_step_rows = _chain_stale_step_rows(chain)

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
