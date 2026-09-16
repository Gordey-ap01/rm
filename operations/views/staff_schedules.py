"""Request, review and audit views for recurring staff schedules."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Prefetch
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from operations.models import (
    StaffAvailability,
    StaffMember,
    StaffScheduleChangeDecision,
    StaffScheduleChangeRequest,
)
from operations.services import staff_schedules as schedule_svc
from operations.services.authority import AuthorityRole, authority_role, is_center_operator
from operations.staff_schedule_forms import (
    WEEKDAY_LABELS,
    StaffScheduleDecisionForm,
    StaffScheduleRequestForm,
    empty_week,
)

from ._common import safe_next_url


def _actor_staff(request):
    role = authority_role(request.user)
    if role in {AuthorityRole.DIRECTOR, AuthorityRole.ADMINISTRATOR}:
        return None
    staff = getattr(request.user, "staff_profile", None)
    if role != AuthorityRole.SPECIALIST or not staff:
        raise PermissionDenied("Для этого раздела нужен профиль специалиста или права оператора.")
    if not staff.can_use_mobile:
        raise PermissionDenied("Доступ к мобильному кабинету специалиста отключен.")
    return staff


def _active_staff():
    return StaffMember.objects.filter(status=StaffMember.Status.ACTIVE).order_by("full_name")


def _scoped_request(request, pk: int):
    item = get_object_or_404(
        _with_current_decision(
            StaffScheduleChangeRequest.objects.select_related("staff_member", "created_by")
        ),
        pk=pk,
    )
    _attach_current_decision(item)
    staff = _actor_staff(request)
    if staff and item.staff_member_id != staff.pk:
        raise PermissionDenied("Можно просматривать только собственный график.")
    return item


def _history(item):
    return item.decisions.select_related("actor").order_by("created_at", "pk")


def _current_decision(item):
    return item.current_decision_record


def _with_current_decision(queryset):
    return queryset.prefetch_related(
        Prefetch(
            "decisions",
            queryset=StaffScheduleChangeDecision.objects.filter(is_current=True).select_related("actor"),
            to_attr="current_decision_rows",
        )
    )


def _attach_current_decision(item) -> None:
    current_rows = getattr(item, "current_decision_rows", ())
    item.current_decision_record = current_rows[0] if current_rows else None


def _format_window_time(value: str) -> str:
    return datetime.strptime(value, "%H:%M").strftime("%H:%M")


def _schedule_week(item) -> list[dict]:
    """Normalize a model/service payload for the presentation layer only."""
    by_weekday = {row["weekday"]: row for row in item.week}
    result = []
    for weekday, label in enumerate(WEEKDAY_LABELS):
        row = by_weekday.get(weekday, {})
        windows = []
        for window in row.get("windows", ()):
            windows.append(
                {
                    "start": _format_window_time(window["start"]),
                    "end": _format_window_time(window["end"]),
                }
            )
        result.append(
            {
                "label": label,
                "closed": row.get("closed", False),
                "windows": windows,
            }
        )
    return result


def _initial_week(staff: StaffMember | None, effective_from) -> list[dict]:
    if staff is None:
        return empty_week()
    week = []
    for weekday, label in enumerate(WEEKDAY_LABELS):
        day = effective_from + timedelta(days=(weekday - effective_from.weekday()) % 7)
        effective = schedule_svc.effective_windows(staff, day)
        if effective is None:
            effective = list(
                StaffAvailability.objects.filter(
                    staff_member=staff,
                    weekday=weekday,
                    is_active=True,
                )
                .order_by("starts_at")
                .values_list("starts_at", "ends_at")
            ) or [(time(9), time(18))]
        week.append(
            {
                "weekday": weekday,
                "label": label,
                "closed": not effective,
                "windows": [
                    {"start": starts_at.strftime("%H:%M"), "end": ends_at.strftime("%H:%M")}
                    for starts_at, ends_at in effective
                ],
            }
        )
    return week


def _actor_role(decision) -> str:
    return decision.actor_role


def _director_has_final_decision(item) -> bool:
    current = _current_decision(item)
    return bool(current and _actor_role(current) == AuthorityRole.DIRECTOR)


def _allowed_actions(request, item) -> tuple[str, ...]:
    if not is_center_operator(request.user):
        return ()
    if item.status == StaffScheduleChangeRequest.Status.REJECTED:
        return ()
    role = authority_role(request.user)
    current = _current_decision(item)
    if item.status == StaffScheduleChangeRequest.Status.APPROVED:
        if role == AuthorityRole.DIRECTOR:
            return ("confirm", "reject") if current.requires_director_review else ("reject",)
        return () if _director_has_final_decision(item) else ("reject",)
    return ("approve", "reject")


def _can_decide(request, item) -> bool:
    return bool(_allowed_actions(request, item))


def _decision_form(item, *, request=None):
    current = _current_decision(item)
    role = authority_role(request.user) if request else AuthorityRole.AUTHENTICATED
    kwargs = {
        "is_director": role == AuthorityRole.DIRECTOR,
        "allowed_actions": _allowed_actions(request, item) if request else (),
    }
    if request and request.method == "POST":
        return StaffScheduleDecisionForm(request.POST, **kwargs)
    kwargs["initial"] = {
        "expected_decision_id": getattr(current, "pk", None),
        "expected_revision_id": schedule_svc.latest_revision_id(item.staff_member),
    }
    return StaffScheduleDecisionForm(**kwargs)


def _detail_context(request, item, *, decision_form=None):
    impact = schedule_svc.impact_rows(item)
    current = _current_decision(item)
    return {
        "schedule_request": item,
        "schedule_week": _schedule_week(item),
        "decision_history": _history(item),
        "revision_history": [
            {"revision": revision, "week": _schedule_week(revision)}
            for revision in item.revisions.order_by("effective_from", "pk")
        ],
        "today": timezone.localdate(),
        "current_decision": current,
        "impact_rows": impact,
        "can_decide": _can_decide(request, item),
        "is_director": authority_role(request.user) == AuthorityRole.DIRECTOR,
        "awaits_director_review": bool(current and current.requires_director_review),
        "decision_form": decision_form or _decision_form(item, request=request),
    }


def _render_detail(request, item, *, decision_form=None, status=200):
    return render(
        request,
        "operations/staff_schedule_detail.html",
        _detail_context(request, item, decision_form=decision_form),
        status=status,
    )


@login_required
def staff_schedule_list(request):
    own_staff = _actor_staff(request)
    queryset = _with_current_decision(
        StaffScheduleChangeRequest.objects.select_related("staff_member", "created_by").order_by(
            "-effective_from", "-created_at", "-pk"
        )
    )
    selected_staff = None
    if own_staff:
        queryset = queryset.filter(staff_member=own_staff)
        selected_staff = own_staff
    elif request.GET.get("staff_id"):
        selected_staff = get_object_or_404(_active_staff(), pk=request.GET["staff_id"])
        queryset = queryset.filter(staff_member=selected_staff)
    page_obj = Paginator(queryset, 25).get_page(request.GET.get("page"))
    schedule_requests = list(page_obj)
    for item in schedule_requests:
        _attach_current_decision(item)
    return render(
        request,
        "operations/staff_schedule_list.html",
        {
            "schedule_requests": schedule_requests,
            "page_obj": page_obj,
            "staff_members": _active_staff() if not own_staff else (),
            "selected_staff": selected_staff,
            "is_operator": own_staff is None,
            "can_create": bool(own_staff or is_center_operator(request.user)),
        },
    )


@login_required
def staff_schedule_create(request):
    own_staff = _actor_staff(request)
    is_operator = own_staff is None
    selected_staff = own_staff
    if is_operator and request.GET.get("staff_id"):
        selected_staff = _active_staff().filter(pk=request.GET["staff_id"]).first()
    form_kwargs = {
        "staff_queryset": _active_staff(),
        "selected_staff": selected_staff,
        "show_staff_select": is_operator,
    }
    if request.method == "POST":
        form = StaffScheduleRequestForm(request.POST, **form_kwargs)
        if form.is_valid():
            staff = form.cleaned_data["staff_member"] if is_operator else own_staff
            try:
                item = schedule_svc.create_request(
                    staff_member=staff,
                    effective_from=form.cleaned_data["effective_from"],
                    week=form.cleaned_data["week"],
                    reason=form.cleaned_data["reason"],
                    actor=request.user,
                    request_key=form.cleaned_data["request_key"],
                )
            except (ValidationError, PermissionDenied, ValueError) as exc:
                form.add_error(None, _error_text(exc))
            else:
                messages.success(request, "Запрос на изменение постоянного графика создан.")
                return redirect("staff_schedule_detail", pk=item.pk)
    elif request.method == "GET":
        form = StaffScheduleRequestForm(**form_kwargs)
        form.week = _initial_week(selected_staff, form.initial["effective_from"])
    else:
        return HttpResponseNotAllowed(["GET", "POST"])
    return render(
        request,
        "operations/staff_schedule_form.html",
        {
            "form": form,
            "week": form.week,
            "is_operator": is_operator,
            "staff": selected_staff,
        },
    )


@login_required
def staff_schedule_detail(request, pk: int):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    return _render_detail(request, _scoped_request(request, pk))


@login_required
def staff_schedule_decide(request, pk: int):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    item = _scoped_request(request, pk)
    if not _can_decide(request, item):
        raise PermissionDenied("Текущее решение руководителя нельзя изменить в этом интерфейсе.")
    form = _decision_form(item, request=request)
    if not form.is_valid():
        return _render_detail(request, item, decision_form=form, status=400)
    try:
        decision = schedule_svc.decide_request(
            item,
            action=form.cleaned_data["action"],
            reason=form.cleaned_data["reason"],
            actor=request.user,
            expected_decision_id=form.cleaned_data["expected_decision_id"],
            expected_revision_id=form.cleaned_data["expected_revision_id"],
            request_key=form.cleaned_data["request_key"],
        )
    except (ValidationError, PermissionDenied, ValueError) as exc:
        form.add_error(None, _error_text(exc))
        return _render_detail(request, item, decision_form=form, status=409)
    messages.success(request, f"Решение «{decision.get_action_display()}» сохранено.")
    return redirect(safe_next_url(request, reverse("staff_schedule_detail", kwargs={"pk": item.pk})))


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return " ".join(str(message) for message in exc.messages)
    return str(exc) or "Не удалось обработать запрос. Проверьте данные и повторите попытку."
