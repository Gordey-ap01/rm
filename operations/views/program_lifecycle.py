"""Operator UI for the append-only treatment-program lifecycle."""

from django.contrib import messages
from django.core.exceptions import NON_FIELD_ERRORS, PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from operations.forms_program_lifecycle import TreatmentProgramLifecycleActionForm
from operations.models import (
    ProgramBlock,
    Service,
    TreatmentProgram,
    TreatmentProgramLifecycleEvent,
)
from operations.services import program_lifecycle

from ._common import admin_required, is_director

_ACTION_PAUSE = "pause"
_ACTION_RESUME = "resume"
_ACTION_ACTIVATE = "activate"
_ACTION_COMPLETE = "complete"
_ACTION_CANCEL = "cancel"

_REVIEW_ACTIONS = {_ACTION_ACTIVATE, _ACTION_COMPLETE, _ACTION_CANCEL}

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
    _ACTION_ACTIVATE: {
        "slug": _ACTION_ACTIVATE,
        "title": "Активировать программу",
        "label": "Активировать программу",
        "icon": "bi-play-circle",
        "button_class": "btn-outline-success",
        "confirm_class": "btn-success",
        "confirm_label": "Активировать программу",
        "description": (
            "Перед активацией будут проверены каскады программы. Команда не создает "
            "занятия, назначения или финансовые записи автоматически."
        ),
        "review_heading": "Каскады для активации",
        "review_empty": "Каскады еще не созданы.",
    },
    _ACTION_COMPLETE: {
        "slug": _ACTION_COMPLETE,
        "title": "Завершить программу",
        "label": "Завершить программу",
        "icon": "bi-check2-circle",
        "button_class": "btn-outline-success",
        "confirm_class": "btn-success",
        "confirm_label": "Завершить программу",
        "description": (
            "Завершение является явным решением. Оно не отменяет и не удаляет уже "
            "созданные занятия, участия, каскады или финансовые факты."
        ),
        "review_heading": "Незавершённые каскады",
        "review_empty": "Все каскады завершены или отменены.",
    },
    _ACTION_CANCEL: {
        "slug": _ACTION_CANCEL,
        "title": "Отменить программу",
        "label": "Отменить программу",
        "icon": "bi-x-circle",
        "button_class": "btn-outline-danger",
        "confirm_class": "btn-danger",
        "confirm_label": "Отменить программу",
        "description": (
            "Отмена фиксирует решение по программе. Она не отменяет и не удаляет "
            "уже созданные занятия, участия, каскады или финансовые факты."
        ),
        "review_heading": "Каскады программы",
        "review_empty": "Каскады еще не созданы.",
    },
}


def _latest_event(program):
    return program.lifecycle_events.order_by("-event_number", "-pk").first()


def _director_lock(latest_event, user):
    return bool(
        latest_event
        and latest_event.actor_role_snapshot
        == TreatmentProgramLifecycleEvent.ActorRole.DIRECTOR
        and not is_director(user)
    )


def _action_definition(program, action, *, user, latest_event, review=None):
    definition = _ACTION_DEFINITIONS.get(action)
    if definition is None:
        raise Http404("Неизвестная команда программы.")

    director_lock = _director_lock(latest_event, user)
    if action == _ACTION_PAUSE:
        available = program.status == TreatmentProgram.Status.ACTIVE and not director_lock
        blocked_reason = (
            "Последнее решение по программе принято руководителем. Приостановить "
            "программу может только руководитель."
            if director_lock
            else "Приостановить можно только активную программу."
        )
    elif action == _ACTION_RESUME:
        available = program.status == TreatmentProgram.Status.PAUSED and is_director(user)
        if program.status != TreatmentProgram.Status.PAUSED:
            blocked_reason = "Возобновить можно только программу на паузе."
        elif not is_director(user):
            blocked_reason = "Возобновить программу может только руководитель."
        elif latest_event is None:
            blocked_reason = ""
        else:
            blocked_reason = ""
    elif action == _ACTION_ACTIVATE:
        activation_error = getattr(review, "activation_error", "") if review else ""
        available = (
            program.status == TreatmentProgram.Status.DRAFT
            and not director_lock
            and bool(getattr(review, "can_activate", False))
        )
        if director_lock:
            blocked_reason = "Последнее решение по программе принято руководителем. Активировать программу может только руководитель."
        elif program.status != TreatmentProgram.Status.DRAFT:
            blocked_reason = "Активировать можно только программу в черновике."
        else:
            blocked_reason = activation_error or "Программа пока не готова к активации."
    elif action == _ACTION_COMPLETE:
        unfinished = tuple(getattr(review, "unfinished_blocks", ()))
        available = (
            program.status in {TreatmentProgram.Status.ACTIVE, TreatmentProgram.Status.PAUSED}
            and not director_lock
            and (is_director(user) or not unfinished)
        )
        if director_lock:
            blocked_reason = "Последнее решение по программе принято руководителем. Завершить программу может только руководитель."
        elif program.status not in {TreatmentProgram.Status.ACTIVE, TreatmentProgram.Status.PAUSED}:
            blocked_reason = "Завершить можно только активную программу или программу на паузе."
        elif unfinished and not is_director(user):
            blocked_reason = "Незавершённую программу досрочно завершает только руководитель."
        else:
            blocked_reason = ""
    else:
        available = (
            program.status in {
                TreatmentProgram.Status.DRAFT,
                TreatmentProgram.Status.ACTIVE,
                TreatmentProgram.Status.PAUSED,
            }
            and not director_lock
        )
        if director_lock:
            blocked_reason = "Последнее решение по программе принято руководителем. Отменить программу может только руководитель."
        elif program.status not in {
            TreatmentProgram.Status.DRAFT,
            TreatmentProgram.Status.ACTIVE,
            TreatmentProgram.Status.PAUSED,
        }:
            blocked_reason = "Отменить можно только черновик, активную программу или программу на паузе."
        else:
            blocked_reason = ""

    result = dict(definition)
    result.update(available=available, blocked_reason=blocked_reason)
    return result


def _available_actions(program, *, user, latest_event, review):
    return [
        _action_definition(program, action, user=user, latest_event=latest_event, review=review)
        for action in (
            _ACTION_ACTIVATE,
            _ACTION_PAUSE,
            _ACTION_RESUME,
            _ACTION_COMPLETE,
            _ACTION_CANCEL,
        )
    ]


def _review_blocks(blocks):
    status_labels = dict(ProgramBlock.Status.choices)
    service_names = {
        service.pk: service.name
        for service in Service.objects.in_bulk(
            {block["service_id"] for block in blocks if block["service_id"]}
        ).values()
    }
    return [
        {
            **block,
            "service_name": service_names.get(block["service_id"], "Неизвестная услуга"),
            "status_display": status_labels.get(block["status"], block["status"]),
        }
        for block in blocks
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
    review = program_lifecycle.get_program_lifecycle_review(program)
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
                program, user=request.user, latest_event=latest_event, review=review
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
    review = program_lifecycle.get_program_lifecycle_review(program)
    action_definition = _action_definition(
        program, action, user=request.user, latest_event=latest_event, review=review
    )
    is_review_action = action in _REVIEW_ACTIONS
    review_source = (
        review.unfinished_blocks if action == _ACTION_COMPLETE else review.blocks
    )
    action_definition["review_blocks"] = _review_blocks(review_source) if is_review_action else ()
    form = TreatmentProgramLifecycleActionForm(
        request.POST if request.method == "POST" else None,
        initial={
            "expected_event_id": latest_event.pk if latest_event else 0,
            "expected_review_fingerprint": review.fingerprint,
        },
        requires_review_fingerprint=is_review_action,
    )
    response_status = 200
    review_is_stale = False

    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        lifecycle_service = {
            _ACTION_PAUSE: program_lifecycle.pause_program,
            _ACTION_RESUME: program_lifecycle.resume_program,
            _ACTION_ACTIVATE: program_lifecycle.activate_program,
            _ACTION_COMPLETE: program_lifecycle.complete_program,
            _ACTION_CANCEL: program_lifecycle.cancel_program,
        }[action]
        service_kwargs = {
            "actor": request.user,
            "reason": data["reason"],
            "operation_key": data["operation_key"],
            "expected_event_id": data["expected_event_id"],
        }
        if is_review_action:
            service_kwargs["expected_review_fingerprint"] = data["expected_review_fingerprint"]
        try:
            result = lifecycle_service(program, **service_kwargs)
        except program_lifecycle.ProgramLifecycleMismatch as exc:
            _add_form_validation_error(form, exc)
            response_status = 409
            review_is_stale = is_review_action
        except PermissionDenied:
            raise
        except ValidationError as exc:
            _add_form_validation_error(form, exc)
        else:
            if result.reused_event:
                messages.info(request, "Повторный запрос распознан без нового события.")
            elif action == _ACTION_PAUSE:
                messages.success(request, "Программа приостановлена. Новые назначения заблокированы.")
            elif action == _ACTION_RESUME:
                messages.success(request, "Программа возобновлена. Новые записи не созданы автоматически.")
            elif action == _ACTION_ACTIVATE:
                messages.success(request, "Программа активирована. Новые записи не созданы автоматически.")
            elif action == _ACTION_COMPLETE:
                messages.success(request, "Программа завершена. Существующие занятия и финансовые факты сохранены.")
            else:
                messages.success(request, "Программа отменена. Существующие занятия и финансовые факты сохранены.")
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
            "review_blocks": action_definition["review_blocks"],
            "review_is_stale": review_is_stale,
            "refresh_url": reverse("program_lifecycle_action", args=[program.pk, action]),
            "legacy_paused_without_history": bool(
                program.status == TreatmentProgram.Status.PAUSED and latest_event is None
            ),
        },
        status=response_status,
    )
