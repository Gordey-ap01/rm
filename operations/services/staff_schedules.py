"""Date-effective, audited revisions of a specialist's regular weekly schedule."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta
from itertools import pairwise

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from operations.models import (
    ACTIVE_APPOINTMENT_STATUSES,
    Appointment,
    StaffAvailability,
    StaffMember,
    StaffScheduleChangeDecision,
    StaffScheduleChangeRequest,
    StaffScheduleRevision,
)

from .authority import AuthorityRole, authority_role


def _fingerprint(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _reason(value: object) -> str:
    value = str(value or "").strip()
    if len(value) < 5:
        raise ValidationError("Укажите основание не короче 5 символов.")
    return value


def _week(value: object) -> list[dict]:
    if not isinstance(value, list) or len(value) != 7:
        raise ValidationError("Недельный график должен содержать ровно семь дней.")
    normalized: list[dict] = []
    weekdays: set[int] = set()
    for day in value:
        if not isinstance(day, dict) or set(day) != {"weekday", "closed", "windows"}:
            raise ValidationError("Каждый день графика должен содержать weekday, closed и windows.")
        weekday = day["weekday"]
        closed = day["closed"]
        windows = day["windows"]
        if type(weekday) is not int or weekday not in range(7) or weekday in weekdays:
            raise ValidationError("Дни недели должны быть уникальными значениями от 0 до 6.")
        if type(closed) is not bool or not isinstance(windows, list):
            raise ValidationError("Поля closed и windows имеют неверный формат.")
        if closed and windows:
            raise ValidationError("У закрытого дня не может быть рабочих окон.")
        if not closed and not windows:
            raise ValidationError("Для открытого дня укажите хотя бы одно рабочее окно.")
        periods: list[tuple[time, time]] = []
        for window in windows:
            if not isinstance(window, dict) or set(window) != {"start", "end"}:
                raise ValidationError("Рабочее окно должно содержать start и end.")
            try:
                starts_at = datetime.strptime(window["start"], "%H:%M").time()
                ends_at = datetime.strptime(window["end"], "%H:%M").time()
            except (TypeError, ValueError) as exc:
                raise ValidationError("Время окна укажите в формате ЧЧ:ММ.") from exc
            if ends_at <= starts_at:
                raise ValidationError("Окно не может пересекать полночь или иметь нулевую длину.")
            periods.append((starts_at, ends_at))
        periods.sort()
        if any(current[0] < previous[1] for previous, current in pairwise(periods)):
            raise ValidationError("Рабочие окна одного дня не должны пересекаться.")
        weekdays.add(weekday)
        normalized.append(
            {
                "weekday": weekday,
                "closed": closed,
                "windows": [
                    {"start": starts_at.strftime("%H:%M"), "end": ends_at.strftime("%H:%M")}
                    for starts_at, ends_at in periods
                ],
            }
        )
    return sorted(normalized, key=lambda item: item["weekday"])


def _actor_can_create(*, actor, staff: StaffMember) -> AuthorityRole:
    role = authority_role(actor)
    if role in {AuthorityRole.DIRECTOR, AuthorityRole.ADMINISTRATOR}:
        return role
    if role == AuthorityRole.SPECIALIST and staff.user_id == actor.pk:
        if not staff.can_use_mobile:
            raise PermissionDenied("Специалисту отключён доступ к заявкам на график.")
        return role
    raise PermissionDenied("Заявку на график можно создать только за себя.")


def _manager_role(actor) -> AuthorityRole:
    role = authority_role(actor)
    if role not in {AuthorityRole.DIRECTOR, AuthorityRole.ADMINISTRATOR}:
        raise PermissionDenied("Недостаточно прав для решения по постоянному графику.")
    return role


def _actor_role_value(role: AuthorityRole) -> str:
    return (
        StaffScheduleChangeDecision.ActorRole.DIRECTOR
        if role == AuthorityRole.DIRECTOR
        else StaffScheduleChangeDecision.ActorRole.ADMINISTRATOR
    )


def _tomorrow() -> date:
    return timezone.localdate() + timedelta(days=1)


def latest_revision_id(staff: StaffMember) -> int | None:
    """Return the monotonic staff-wide schedule version token.

    The public name is retained for callers that previously used the latest
    revision id. A future revision may be deactivated by a later rejection,
    so only the last decision reliably invalidates every stale form.
    """
    decision = (
        StaffScheduleChangeDecision.objects.filter(request__staff_member=staff)
        .order_by("-pk")
        .first()
    )
    return decision.pk if decision else None


@transaction.atomic
def create_request(*, staff_member, effective_from, week, reason, actor, request_key):
    """Create an idempotent proposal; managers may submit for another specialist."""
    staff = StaffMember.objects.select_for_update().get(pk=staff_member.pk)
    role = _actor_can_create(actor=actor, staff=staff)
    normalized_week = _week(week)
    normalized_reason = _reason(reason)
    fingerprint = _fingerprint(
        {
            "staff": staff.pk,
            "effective_from": effective_from.isoformat(),
            "week": normalized_week,
            "reason": normalized_reason,
            "actor": actor.pk,
        }
    )
    replay = StaffScheduleChangeRequest.objects.filter(request_key=request_key).first()
    if replay:
        if replay.fingerprint == fingerprint:
            return replay
        raise ValidationError("Этот ключ повтора уже относится к другой заявке.")
    if effective_from < _tomorrow():
        raise ValidationError("Постоянный график можно назначить не раньше завтрашнего дня.")
    return StaffScheduleChangeRequest.objects.create(
        staff_member=staff,
        effective_from=effective_from,
        week=normalized_week,
        reason=normalized_reason,
        created_by=actor,
        created_by_role=role.value,
        request_key=request_key,
        fingerprint=fingerprint,
    )


def effective_windows(staff, day) -> list[tuple[time, time]] | None:
    """Return None for legacy availability, [] for an explicit closed day."""
    revision = (
        StaffScheduleRevision.objects.filter(
            staff_member=staff, is_current=True, effective_from__lte=day
        )
        .order_by("-effective_from", "-pk")
        .first()
    )
    if revision is None or revision.uses_legacy:
        return None
    weekday = day.weekday()
    definition = next(item for item in revision.week if item["weekday"] == weekday)
    if definition["closed"]:
        return []
    return [
        (
            datetime.strptime(item["start"], "%H:%M").time(),
            datetime.strptime(item["end"], "%H:%M").time(),
        )
        for item in definition["windows"]
    ]


def impact_rows(request) -> list[Appointment]:
    """Future active appointments outside this proposal before the next revision."""
    next_revision = (
        StaffScheduleRevision.objects.filter(
            staff_member=request.staff_member,
            is_current=True,
            effective_from__gt=request.effective_from,
        )
        .order_by("effective_from", "pk")
        .first()
    )
    start = max(
        timezone.now(),
        timezone.make_aware(datetime.combine(request.effective_from, time.min)),
    )
    rows = Appointment.objects.select_related("child", "service", "room", "staff_member").filter(
        status__in=ACTIVE_APPOINTMENT_STATUSES,
        starts_at__gte=start,
    ).filter(
        Q(staff_member=request.staff_member)
        | Q(staff_assignments__staff_member=request.staff_member)
    )
    if next_revision:
        end = timezone.make_aware(datetime.combine(next_revision.effective_from, time.min))
        rows = rows.filter(starts_at__lt=end)
    definitions = {item["weekday"]: item for item in request.week}
    impacted: list[Appointment] = []
    for appointment in rows.distinct().order_by("starts_at", "pk"):
        starts_at = timezone.localtime(appointment.starts_at)
        ends_at = timezone.localtime(appointment.ends_at)
        definition = definitions[starts_at.weekday()]
        fits = (
            starts_at.date() == ends_at.date()
            and not definition["closed"]
            and any(
                datetime.strptime(window["start"], "%H:%M").time()
                <= starts_at.time()
                and ends_at.time()
                <= datetime.strptime(window["end"], "%H:%M").time()
                for window in definition["windows"]
            )
        )
        if not fits:
            impacted.append(appointment)
    return impacted


def _validate_stale(*, current, latest, expected_decision_id, expected_revision_id) -> None:
    if expected_decision_id != (current.pk if current else None):
        raise ValidationError("Заявка уже получила другое решение. Обновите страницу.")
    if expected_revision_id != latest:
        raise ValidationError("График уже изменился. Обновите страницу.")


def _restore_predecessor(*, request, decision, today: date) -> None:
    source = (
        StaffScheduleRevision.objects.filter(
            staff_member=request.staff_member,
            request=request,
            is_current=True,
            effective_from__lt=today,
        )
        .order_by("-effective_from", "-pk")
        .first()
    )
    if source is None:
        return
    newer = (
        StaffScheduleRevision.objects.filter(
            staff_member=request.staff_member,
            is_current=True,
            effective_from__gt=source.effective_from,
            effective_from__lte=today,
        )
        .exclude(pk=source.pk)
        .exists()
    )
    if newer:
        raise ValidationError("Есть более новая утверждённая версия графика; переопределение невозможно.")
    predecessor = (
        StaffScheduleRevision.objects.filter(
            staff_member=request.staff_member,
            is_current=True,
            effective_from__lt=source.effective_from,
        )
        .order_by("-effective_from", "-pk")
        .first()
    )
    legacy_week = _legacy_week_snapshot(request.staff_member)
    StaffScheduleRevision.objects.create(
        staff_member=request.staff_member,
        request=request,
        decision=decision,
        effective_from=today,
        week=(predecessor.week if predecessor and not predecessor.uses_legacy else legacy_week),
        uses_legacy=False,
    )


def _legacy_week_snapshot(staff: StaffMember) -> list[dict]:
    """Freeze today's legacy windows so later edits cannot rewrite history."""
    windows_by_day: dict[int, list[dict]] = {weekday: [] for weekday in range(7)}
    for item in StaffAvailability.objects.filter(staff_member=staff, is_active=True).order_by(
        "weekday", "starts_at", "pk"
    ):
        windows_by_day[item.weekday].append(
            {"start": item.starts_at.strftime("%H:%M"), "end": item.ends_at.strftime("%H:%M")}
        )
    return [
        {
            "weekday": weekday,
            "closed": False,
            "windows": windows_by_day[weekday]
            or [{"start": "09:00", "end": "18:00"}],
        }
        for weekday in range(7)
    ]


@transaction.atomic
def decide_request(
    request,
    *,
    action,
    reason,
    actor,
    expected_decision_id,
    expected_revision_id,
    request_key,
):
    """Append a role-aware decision and, when appropriate, a weekly snapshot."""
    role = _manager_role(actor)
    if action not in StaffScheduleChangeDecision.Action.values:
        raise ValidationError("Неизвестное решение по заявке на график.")
    normalized_reason = _reason(reason)
    staff_id = request.staff_member_id
    staff = StaffMember.objects.select_for_update().get(pk=staff_id)
    locked = StaffScheduleChangeRequest.objects.select_for_update().get(pk=request.pk)
    current = (
        StaffScheduleChangeDecision.objects.select_for_update()
        .filter(request=locked, is_current=True)
        .first()
    )
    latest = latest_revision_id(staff)
    fingerprint = _fingerprint(
        {
            "request": locked.pk,
            "action": action,
            "reason": normalized_reason,
            "actor": actor.pk,
            "expected_decision_id": expected_decision_id,
            "expected_revision_id": expected_revision_id,
        }
    )
    replay = StaffScheduleChangeDecision.objects.filter(request_key=request_key).first()
    if replay:
        if replay.fingerprint == fingerprint:
            return replay
        raise ValidationError("Этот ключ повтора уже относится к другому решению.")
    _validate_stale(
        current=current,
        latest=latest,
        expected_decision_id=expected_decision_id,
        expected_revision_id=expected_revision_id,
    )
    if current and current.actor_role == StaffScheduleChangeDecision.ActorRole.DIRECTOR and role != AuthorityRole.DIRECTOR:
        raise PermissionDenied("Решение руководителя может изменить только руководитель.")
    tomorrow = _tomorrow()
    if action == StaffScheduleChangeDecision.Action.APPROVE and locked.effective_from < tomorrow:
        raise ValidationError("Срок действия заявки истёк. Отклоните её и подайте новую.")
    if action == StaffScheduleChangeDecision.Action.APPROVE and locked.status == StaffScheduleChangeRequest.Status.APPROVED:
        raise ValidationError("Эта заявка уже согласована.")
    if action == StaffScheduleChangeDecision.Action.REJECT and locked.status == StaffScheduleChangeRequest.Status.REJECTED:
        raise ValidationError("Эта заявка уже отклонена.")
    if action == StaffScheduleChangeDecision.Action.CONFIRM:
        if role != AuthorityRole.DIRECTOR:
            raise PermissionDenied("Подтвердить график может только руководитель.")
        if not current or current.action != StaffScheduleChangeDecision.Action.APPROVE or not current.requires_director_review:
            raise ValidationError("Подтверждать руководителю нечего.")
    if action == StaffScheduleChangeDecision.Action.APPROVE:
        conflict = StaffScheduleRevision.objects.filter(
            staff_member=staff, effective_from__gte=tomorrow, is_current=True
        ).exclude(request=locked)
        if conflict.filter(effective_from=locked.effective_from).exists():
            raise ValidationError("На эту дату уже утверждена другая версия графика.")

    if current:
        current.is_current = False
        current.save(update_fields=["is_current", "updated_at"])
    decision = StaffScheduleChangeDecision.objects.create(
        request=locked,
        actor=actor,
        actor_role=_actor_role_value(role),
        action=action,
        reason=normalized_reason,
        request_key=request_key,
        fingerprint=fingerprint,
        supersedes=current,
        requires_director_review=(
            action == StaffScheduleChangeDecision.Action.APPROVE
            and role == AuthorityRole.ADMINISTRATOR
            and locked.director_priority
        ),
    )
    if action == StaffScheduleChangeDecision.Action.APPROVE:
        StaffScheduleRevision.objects.create(
            staff_member=staff,
            request=locked,
            decision=decision,
            effective_from=locked.effective_from,
            week=locked.week,
        )
        locked.status = StaffScheduleChangeRequest.Status.APPROVED
    elif action == StaffScheduleChangeDecision.Action.REJECT:
        existing = StaffScheduleRevision.objects.filter(
            request=locked, is_current=True, effective_from__gte=tomorrow
        )
        existing.update(is_current=False)
        _restore_predecessor(request=locked, decision=decision, today=tomorrow)
        locked.status = StaffScheduleChangeRequest.Status.REJECTED
    else:
        locked.status = StaffScheduleChangeRequest.Status.APPROVED
    locked.save(update_fields=["status", "updated_at"])
    return decision
