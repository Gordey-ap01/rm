"""Authority-aware, append-only decisions for specialist time-off requests."""

from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Prefetch, Q, QuerySet
from django.utils import timezone

from operations.models import TimeOffRequest, TimeOffRequestDecision

from .authority import AuthorityRole, authority_role


def _decision_value(action: str) -> str:
    values = {
        "approve": TimeOffRequestDecision.Decision.APPROVED,
        "reject": TimeOffRequestDecision.Decision.REJECTED,
    }
    try:
        return values[action]
    except KeyError as exc:
        raise ValueError("Неизвестное решение по заявке специалиста.") from exc


def _current_decision_queryset() -> QuerySet[TimeOffRequestDecision]:
    return TimeOffRequestDecision.objects.filter(is_current=True).select_related("actor")


def with_current_decision(
    queryset: QuerySet[TimeOffRequest],
) -> QuerySet[TimeOffRequest]:
    return queryset.prefetch_related(
        Prefetch(
            "decision_history",
            queryset=_current_decision_queryset(),
            to_attr="current_decision_rows",
        )
    )


def decorate_rows(
    rows,
    *,
    actor=None,
) -> list[TimeOffRequest]:
    viewer_role = authority_role(actor)
    can_manage = viewer_role in {
        AuthorityRole.DIRECTOR,
        AuthorityRole.ADMINISTRATOR,
    }
    decorated = list(rows)
    for item in decorated:
        current_rows = getattr(item, "current_decision_rows", ())
        current = current_rows[0] if current_rows else None
        item.current_decision_record = current
        item.awaits_director_review = bool(current and current.awaits_director_review)
        legacy_director = (
            current is None
            and item.decided_by_id
            and authority_role(item.decided_by) == AuthorityRole.DIRECTOR
        )
        item.can_resolve_manually = can_manage and (
            viewer_role == AuthorityRole.DIRECTOR
            or (
                not (
                    current
                    and current.source
                    == TimeOffRequestDecision.Source.DIRECTOR_MANUAL
                )
                and not legacy_director
            )
        )
    return decorated


def attention_queryset() -> QuerySet[TimeOffRequest]:
    return TimeOffRequest.objects.filter(
        Q(status=TimeOffRequest.Status.PENDING)
        | Q(
            decision_history__is_current=True,
            decision_history__director_priority=True,
            decision_history__source=TimeOffRequestDecision.Source.ADMINISTRATOR_MANUAL,
        )
    ).distinct()


def attention_rows(*, limit: int, actor=None) -> list[TimeOffRequest]:
    queryset = (
        attention_queryset()
        .select_related("staff_member", "decided_by")
        .order_by("starts_on", "staff_member__full_name")
    )
    return decorate_rows(with_current_decision(queryset)[:limit], actor=actor)


@transaction.atomic
def resolve_manually(
    time_off_request: TimeOffRequest,
    *,
    action: str,
    reason: str,
    actor,
) -> TimeOffRequestDecision:
    role = authority_role(actor)
    if role not in {AuthorityRole.DIRECTOR, AuthorityRole.ADMINISTRATOR}:
        raise PermissionDenied("Недостаточно прав для решения по заявке специалиста.")

    reason = reason.strip()
    if len(reason) < 5:
        raise ValueError("Укажите основание решения не короче 5 символов.")

    locked = TimeOffRequest.objects.select_for_update().get(pk=time_off_request.pk)
    previous = (
        TimeOffRequestDecision.objects.select_for_update()
        .filter(time_off_request=locked, is_current=True)
        .first()
    )
    legacy_director_decision = (
        previous is None
        and locked.status
        in {
            TimeOffRequest.Status.APPROVED,
            TimeOffRequest.Status.REJECTED,
        }
        and locked.decided_by_id
        and authority_role(locked.decided_by) == AuthorityRole.DIRECTOR
    )
    if role != AuthorityRole.DIRECTOR and (
        (
            previous
            and previous.source == TimeOffRequestDecision.Source.DIRECTOR_MANUAL
        )
        or legacy_director_decision
    ):
        raise PermissionDenied("Решение руководителя может изменить только руководитель.")

    if previous:
        previous.is_current = False
        previous.save(update_fields=["is_current", "updated_at"])

    if role == AuthorityRole.DIRECTOR:
        source = TimeOffRequestDecision.Source.DIRECTOR_MANUAL
        actor_role = TimeOffRequestDecision.ActorRole.DIRECTOR
    else:
        source = TimeOffRequestDecision.Source.ADMINISTRATOR_MANUAL
        actor_role = TimeOffRequestDecision.ActorRole.ADMINISTRATOR

    decision = _decision_value(action)
    record = TimeOffRequestDecision.objects.create(
        time_off_request=locked,
        decision=decision,
        source=source,
        actor=actor,
        actor_role_snapshot=actor_role,
        note=reason,
        director_priority=locked.director_priority_required,
        supersedes=previous,
        is_current=True,
    )
    locked.status = decision
    locked.admin_note = reason
    locked.decided_by = actor
    locked.decided_at = timezone.now()
    locked.save(
        update_fields=[
            "status",
            "admin_note",
            "decided_by",
            "decided_at",
            "updated_at",
        ]
    )
    return record
