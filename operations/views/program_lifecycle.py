"""Operator UI for the append-only treatment-program lifecycle."""

from django.contrib import messages
from django.core.exceptions import NON_FIELD_ERRORS, PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from operations.forms_program_lifecycle import TreatmentProgramLifecycleActionForm
from operations.models import TreatmentProgram, TreatmentProgramLifecycleEvent
from operations.services import program_lifecycle

from ._common import admin_required, is_director

_ACTION_PAUSE = "pause"
_ACTION_RESUME = "resume"

_ACTION_DEFINITIONS = {
    _ACTION_PAUSE: {
        "slug": _ACTION_PAUSE,
        "title": "Приостановить программу",
        "label": "Приостановить программу",
        "icon": "bi-pause-circle",
        "button_class": "btn-outline-warning",
        "confirm_class": "btn-warning",
        "confirm_label": "Приостановить программу",
        "description": (
            "Новые назначения по программе будут заблокированы. Уже созданные занятия, "
            "участия, каскады и финансовые факты сохранятся без изменений."
        ),
    },
    _ACTION_RESUME: {
        "slug": _ACTION_RESUME,
        "title": "Возобновить программу",
        "label": "Возобновить программу",
        "icon": "bi-play-circle",
        "button_class": "btn-outline-success",
        "confirm_class": "btn-success",
        "confirm_label": "Возобновить программу",
        "description": (
            "Программа снова станет доступной для новых назначений. Возобновление не "
            "создает занятия, назначения или финансовые записи автоматически."
        ),
    },
}


def _latest_event(program):
    return program.lifecycle_events.order_by("-event_number", "-pk").first()


def _action_definition(program, action, *, user, latest_event):
    definition = _ACTION_DEFINITIONS.get(action)
    if definition is None:
        raise Http404("Неизвестная команда программы.")

    if action == _ACTION_PAUSE:
        director_lock = bool(
            latest_event
            and latest_event.actor_role_snapshot
            == TreatmentProgramLifecycleEvent.ActorRole.DIRECTOR
            and not is_director(user)
        )
        available = program.status == TreatmentProgram.Status.ACTIVE and not director_lock
        blocked_reason = (
            "Последнее решение по программе принято руководителем. Приостановить "
            "программу может только руководитель."
            if director_lock
            else "Приостановить можно только активную программу."
        )
    else:
        available = program.status == TreatmentProgram.Status.PAUSED and is_director(user)
        if program.status != TreatmentProgram.Status.PAUSED:
            blocked_reason = "Возобновить можно только программу на паузе."
        elif not is_director(user):
            blocked_reason = "Возобновить программу может только руководитель."
        elif latest_event is None:
            blocked_reason = ""
        else:
            blocked_reason = ""

    result = dict(definition)
    result.update(available=available, blocked_reason=blocked_reason)
    return result


def _available_actions(program, *, user, latest_event):
    return [
        _action_definition(program, action, user=user, latest_event=latest_event)
        for action in (_ACTION_PAUSE, _ACTION_RESUME)
    ]


def _add_form_validation_error(form, exc):
    if not hasattr(exc, "error_dict"):
        form.add_error(None, exc)
        return
    for field_name, errors in exc.error_dict.items():
        target = field_name if field_name in form.fields else None
        prefix = "" if target or field_name == NON_FIELD_ERRORS else f"{field_name}: "
        for error in errors:
            for message in error.messages:
                form.add_error(target, prefix + message)


@admin_required
def program_detail(request, program_id):
    program = get_object_or_404(
        TreatmentProgram.objects.select_related("child").prefetch_related(
            "blocks__service", "blocks__staff_member", "blocks__balance_account"
        ),
        pk=program_id,
    )
    latest_event = _latest_event(program)
    history_page = Paginator(
        program.lifecycle_events.select_related("actor").order_by("-event_number", "-pk"),
        10,
    ).get_page(request.GET.get("history_page"))
    return render(
        request,
        "operations/program_detail.html",
        {
            "program": program,
            "blocks": program.blocks.all(),
            "program_actions": _available_actions(
                program, user=request.user, latest_event=latest_event
            ),
            "latest_event": latest_event,
            "lifecycle_events": history_page.object_list,
            "lifecycle_page": history_page,
            "legacy_paused_without_history": bool(
                program.status == TreatmentProgram.Status.PAUSED and latest_event is None
            ),
        },
    )


@admin_required
@require_http_methods(["GET", "POST"])
def program_lifecycle_action(request, program_id, action):
    program = get_object_or_404(
        TreatmentProgram.objects.select_related("child"), pk=program_id
    )
    latest_event = _latest_event(program)
    action_definition = _action_definition(
        program, action, user=request.user, latest_event=latest_event
    )
    form = TreatmentProgramLifecycleActionForm(
        request.POST if request.method == "POST" else None,
        initial={"expected_event_id": latest_event.pk if latest_event else 0},
    )
    response_status = 200

    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        lifecycle_service = (
            program_lifecycle.pause_program
            if action == _ACTION_PAUSE
            else program_lifecycle.resume_program
        )
        try:
            result = lifecycle_service(
                program,
                actor=request.user,
                reason=data["reason"],
                operation_key=data["operation_key"],
                expected_event_id=data["expected_event_id"],
            )
        except program_lifecycle.ProgramLifecycleMismatch as exc:
            _add_form_validation_error(form, exc)
            response_status = 409
        except PermissionDenied:
            raise
        except ValidationError as exc:
            _add_form_validation_error(form, exc)
        else:
            if result.reused_event:
                messages.info(request, "Повторный запрос распознан без нового события.")
            elif action == _ACTION_PAUSE:
                messages.success(request, "Программа приостановлена. Новые назначения заблокированы.")
            else:
                messages.success(request, "Программа возобновлена. Новые записи не созданы автоматически.")
            return redirect("program_detail", program_id=program.pk)

    return render(
        request,
        "operations/program_lifecycle_action.html",
        {
            "program": program,
            "action": action_definition,
            "form": form,
            "cancel_url": reverse("program_detail", args=[program.pk]),
            "show_action_form": action_definition["available"] or request.method == "POST",
            "legacy_paused_without_history": bool(
                program.status == TreatmentProgram.Status.PAUSED and latest_event is None
            ),
        },
        status=response_status,
    )

