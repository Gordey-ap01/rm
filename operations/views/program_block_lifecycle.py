"""Operator UI for the append-only lifecycle of a program block."""

from django.contrib import messages
from django.core.exceptions import NON_FIELD_ERRORS, PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from operations.forms_program_block_lifecycle import ProgramBlockLifecycleActionForm
from operations.models import ProgramBlock
from operations.services import program_block_lifecycle

from ._common import admin_required, is_director

_ACTION_COMPLETE = "complete"
_ACTION_CANCEL = "cancel"

_ACTION_DEFINITIONS = {
    _ACTION_COMPLETE: {
        "slug": _ACTION_COMPLETE,
        "title": "Завершить каскад",
        "label": "Завершить каскад",
        "icon": "bi-check2-circle",
        "button_class": "btn-outline-success",
        "confirm_class": "btn-success",
        "confirm_label": "Завершить каскад",
        "description": (
            "Завершение фиксирует отдельное решение по каскаду. После закрытия нельзя "
            "назначать новые занятия в этот каскад. Уже назначенные занятия сохраняются; "
            "проведение можно отметить позже. Финансовые факты сохраняются."
        ),
    },
    _ACTION_CANCEL: {
        "slug": _ACTION_CANCEL,
        "title": "Отменить каскад",
        "label": "Отменить каскад",
        "icon": "bi-x-circle",
        "button_class": "btn-outline-danger",
        "confirm_class": "btn-danger",
        "confirm_label": "Отменить каскад",
        "description": (
            "Отмена фиксирует отдельное решение по каскаду. После закрытия нельзя "
            "назначать новые занятия в этот каскад. Уже назначенные занятия сохраняются; "
            "проведение можно отметить позже. Финансовые факты сохраняются."
        ),
    },
}


def _latest_event(block):
    return block.lifecycle_events.order_by("-pk").first()


def _action_definition(block, action, *, user, review):
    definition = _ACTION_DEFINITIONS.get(action)
    if definition is None:
        raise Http404("Неизвестная команда каскада.")

    if action == _ACTION_COMPLETE:
        available = not review.is_terminal and (
            review.can_complete_normally or is_director(user)
        )
        if review.is_terminal:
            blocked_reason = "Каскад уже закрыт. Новое решение не создаётся."
        elif not review.can_complete_normally:
            blocked_reason = (
                "План фактически не выполнен. Досрочно завершить каскад может "
                "только руководитель с указанным основанием."
            )
        else:
            blocked_reason = ""
    else:
        available = not review.is_terminal
        blocked_reason = (
            "Каскад уже закрыт. Новое решение не создаётся."
            if review.is_terminal
            else ""
        )

    result = dict(definition)
    result.update(
        available=available,
        blocked_reason="" if available else blocked_reason,
        early_completion=(
            action == _ACTION_COMPLETE
            and available
            and review.remaining > 0
        ),
    )
    return result


def _available_actions(block, *, user, review):
    return [
        _action_definition(block, action, user=user, review=review)
        for action in (_ACTION_COMPLETE, _ACTION_CANCEL)
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
def program_block_lifecycle_detail(request, block_id):
    block = get_object_or_404(
        ProgramBlock.objects.select_related("program", "program__child", "service", "balance_account"),
        pk=block_id,
    )
    review = program_block_lifecycle.get_program_block_lifecycle_review(block)
    history_page = Paginator(
        block.lifecycle_events.select_related("actor").order_by("-pk"), 10
    ).get_page(request.GET.get("history_page"))
    return render(
        request,
        "operations/program_block_lifecycle_detail.html",
        {
            "program_block": block,
            "review": review,
            "block_actions": _available_actions(block, user=request.user, review=review),
            "lifecycle_events": history_page.object_list,
            "lifecycle_page": history_page,
        },
    )


@admin_required
@require_http_methods(["GET", "POST"])
def program_block_lifecycle_action(request, block_id, action):
    block = get_object_or_404(
        ProgramBlock.objects.select_related("program", "program__child", "service", "balance_account"),
        pk=block_id,
    )
    review = program_block_lifecycle.get_program_block_lifecycle_review(block)
    latest_event = _latest_event(block)
    action_definition = _action_definition(block, action, user=request.user, review=review)
    form = ProgramBlockLifecycleActionForm(
        request.POST if request.method == "POST" else None,
        initial={
            "expected_event_id": latest_event.pk if latest_event else 0,
            "expected_review_fingerprint": review.fingerprint,
        },
    )
    response_status = 200
    review_is_stale = False

    # The service checks an accepted replay before terminal-state validation.  Keep
    # POST dispatch independent from the current button availability so a retry of
    # an already accepted UUID can still return its original decision.
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        lifecycle_service = {
            _ACTION_COMPLETE: program_block_lifecycle.complete_block,
            _ACTION_CANCEL: program_block_lifecycle.cancel_block,
        }[action]
        try:
            result = lifecycle_service(
                block,
                actor=request.user,
                reason=data["reason"],
                operation_key=data["operation_key"],
                expected_event_id=data["expected_event_id"],
                expected_review_fingerprint=data["expected_review_fingerprint"],
            )
        except program_block_lifecycle.ProgramBlockLifecycleMismatch as exc:
            _add_form_validation_error(form, exc)
            response_status = 409
            review_is_stale = True
        except PermissionDenied:
            raise
        except ValidationError as exc:
            _add_form_validation_error(form, exc)
        else:
            if result.reused_event:
                messages.info(request, "Повторный запрос распознан без нового события.")
            elif action == _ACTION_COMPLETE:
                messages.success(request, "Каскад завершён. Существующие факты сохранены.")
            else:
                messages.success(request, "Каскад отменён. Существующие факты сохранены.")
            return redirect("program_detail", program_id=block.program_id)

    return render(
        request,
        "operations/program_block_lifecycle_action.html",
        {
            "program_block": block,
            "review": review,
            "action": action_definition,
            "form": form,
            "cancel_url": reverse("program_block_lifecycle_detail", args=[block.pk]),
            "show_action_form": action_definition["available"] or request.method == "POST",
            "review_is_stale": review_is_stale,
            "refresh_url": reverse("program_block_lifecycle_action", args=[block.pk, action]),
        },
        status=response_status,
    )
