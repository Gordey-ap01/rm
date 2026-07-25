"""Persisted plans for appointment rescheduling.

The plan stores suggestions and conflict snapshots, but every write revalidates
the live schedule before changing appointments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from operations.forms import AppointmentMoveForm
from operations.models import (
    Appointment,
    AppointmentConfirmation,
    AppointmentParticipant,
    AppointmentRescheduleChain,
    AppointmentReschedulePlan,
    AppointmentRescheduleStep,
    AppointmentRescheduleStepDependency,
    AppointmentStaffAssignment,
    ParentGuardian,
    StaffMember,
)
from operations.schedule_validation import (
    appointment_group_conflicts,
    build_local_datetime,
    conflict_messages,
    staff_unavailability_reason,
)


@dataclass(frozen=True)
class PlanValidationResult:
    plan: AppointmentReschedulePlan
    valid_steps: int
    stale_steps: int
    pending_steps: int


@dataclass(frozen=True)
class ChainValidationResult:
    chain: AppointmentRescheduleChain
    ready_steps: int
    stale_steps: int
    blocked_steps: int
    dependency_count: int


@dataclass(frozen=True)
class ChainApplyResult:
    chain: AppointmentRescheduleChain
    applied_steps: list[AppointmentRescheduleStep] = field(default_factory=list)


@dataclass(frozen=True)
class StepConfirmationResult:
    step: AppointmentRescheduleStep
    created: list[AppointmentConfirmation] = field(default_factory=list)
    existing: list[AppointmentConfirmation] = field(default_factory=list)


@dataclass(frozen=True)
class ChainBuildResult:
    chain: AppointmentRescheduleChain
    steps: list[AppointmentRescheduleStep] = field(default_factory=list)
    dependencies: list[AppointmentRescheduleStepDependency] = field(default_factory=list)


ACTIVE_APPOINTMENT_STATUSES = (
    Appointment.Status.PROPOSED,
    Appointment.Status.CONFIRMED,
    Appointment.Status.RESERVED,
)

TERMINAL_STEP_STATUSES = (
    AppointmentRescheduleStep.Status.APPLIED,
    AppointmentRescheduleStep.Status.SKIPPED,
)

TERMINAL_PLAN_STATUSES = (
    AppointmentReschedulePlan.Status.APPLIED,
    AppointmentReschedulePlan.Status.CANCELLED,
)


def _raise_if_terminal_plan(
    plan: AppointmentReschedulePlan,
    *,
    verb: str = "изменять",
) -> None:
    if plan.status in TERMINAL_PLAN_STATUSES:
        raise ValidationError(f"Завершенный или отмененный план нельзя {verb}.")


def _dependency_input_parts(raw: Any) -> tuple[int, int, str, str, dict[str, Any]]:
    relation_default = AppointmentRescheduleStepDependency.RelationType.FREES_TARGET_SLOT
    if isinstance(raw, dict):
        predecessor_id = int(raw["predecessor_step_id"])
        successor_id = int(raw["successor_step_id"])
        relation_type = raw.get("relation_type") or relation_default
        reason = raw.get("reason", "")
        snapshot = raw.get("snapshot", {})
    else:
        predecessor_id = int(raw[0])
        successor_id = int(raw[1])
        relation_type = raw[2] if len(raw) > 2 else relation_default
        reason = raw[3] if len(raw) > 3 else ""
        snapshot = raw[4] if len(raw) > 4 else {}
    return predecessor_id, successor_id, relation_type, reason, snapshot


def _topological_step_ids(
    step_ids: set[int],
    edges: list[tuple[int, int]],
    *,
    position_by_step_id: dict[int, int],
) -> list[int]:
    successors: dict[int, list[int]] = {step_id: [] for step_id in step_ids}
    indegree: dict[int, int] = {step_id: 0 for step_id in step_ids}
    for predecessor_id, successor_id in edges:
        successors[predecessor_id].append(successor_id)
        indegree[successor_id] += 1

    ready = sorted(
        [step_id for step_id, degree in indegree.items() if degree == 0],
        key=lambda step_id: (position_by_step_id[step_id], step_id),
    )
    ordered: list[int] = []
    while ready:
        step_id = ready.pop(0)
        ordered.append(step_id)
        for successor_id in sorted(
            successors[step_id],
            key=lambda sid: (position_by_step_id[sid], sid),
        ):
            indegree[successor_id] -= 1
            if indegree[successor_id] == 0:
                ready.append(successor_id)
                ready.sort(key=lambda sid: (position_by_step_id[sid], sid))

    if len(ordered) != len(step_ids):
        raise ValidationError("Цепочка переноса содержит цикл зависимостей.")
    return ordered


@transaction.atomic
def create_chain_for_steps(
    plan: AppointmentReschedulePlan,
    *,
    step_ids: list[int],
    dependencies: list[Any],
    title: str = "",
    actor: Any = None,
) -> ChainBuildResult:
    if len(step_ids) < 2:
        raise ValidationError("Для цепочки нужно выбрать минимум два шага.")
    if len(step_ids) != len(set(step_ids)):
        raise ValidationError("Один и тот же шаг нельзя добавить в цепочку дважды.")
    if not dependencies:
        raise ValidationError("Для цепочки нужна хотя бы одна зависимость между шагами.")

    plan = AppointmentReschedulePlan.objects.select_for_update().get(pk=plan.pk)
    if plan.status in TERMINAL_PLAN_STATUSES:
        raise ValidationError("Цепочку нельзя создать для завершенного или отмененного плана.")
    step_id_set = set(step_ids)
    steps = list(
        AppointmentRescheduleStep.objects.select_for_update(
            of=("self", "source_appointment")
        )
        .select_related("source_appointment")
        .filter(plan=plan, pk__in=step_id_set)
        .order_by("position", "pk")
    )
    if len(steps) != len(step_id_set):
        raise ValidationError("Все шаги цепочки должны принадлежать выбранному плану.")
    if any(step.chain_id for step in steps):
        raise ValidationError("Шаг уже входит в цепочку переноса.")
    if any(step.action_type != AppointmentRescheduleStep.ActionType.MOVE for step in steps):
        raise ValidationError("В цепочку можно включать только шаги переноса.")
    if any(step.status in TERMINAL_STEP_STATUSES for step in steps):
        raise ValidationError("Примененные или пропущенные шаги нельзя включать в новую цепочку.")

    source_ids = [step.source_appointment_id for step in steps]
    if len(source_ids) != len(set(source_ids)):
        raise ValidationError(
            "Несколько шагов одного исходного занятия являются альтернативами, а не цепочкой."
        )

    normalized_dependencies: list[tuple[int, int, str, str, dict[str, Any]]] = []
    dependency_keys: set[tuple[int, int, str]] = set()
    connected_step_ids: set[int] = set()
    for raw_dependency in dependencies:
        predecessor_id, successor_id, relation_type, reason, snapshot = _dependency_input_parts(
            raw_dependency
        )
        if predecessor_id == successor_id:
            raise ValidationError("Шаг не может зависеть от самого себя.")
        if predecessor_id not in step_id_set or successor_id not in step_id_set:
            raise ValidationError("Зависимости цепочки должны ссылаться только на выбранные шаги.")
        if relation_type not in AppointmentRescheduleStepDependency.RelationType.values:
            raise ValidationError("Неизвестный тип зависимости цепочки.")
        dependency_key = (predecessor_id, successor_id, relation_type)
        if dependency_key in dependency_keys:
            raise ValidationError("Одна и та же зависимость указана дважды.")
        dependency_keys.add(dependency_key)
        connected_step_ids.update([predecessor_id, successor_id])
        normalized_dependencies.append(
            (predecessor_id, successor_id, relation_type, reason, snapshot)
        )
    if connected_step_ids != step_id_set:
        raise ValidationError("Каждый шаг цепочки должен участвовать хотя бы в одной зависимости.")

    step_by_id = {step.pk: step for step in steps}
    ordered_step_ids = _topological_step_ids(
        step_id_set,
        [(pred, succ) for pred, succ, *_ in normalized_dependencies],
        position_by_step_id={step.pk: step.position for step in steps},
    )

    chain = AppointmentRescheduleChain.objects.create(
        plan=plan,
        title=title.strip(),
        status=AppointmentRescheduleChain.Status.DRAFT,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
        validation_summary={
            "structural": "ok",
            "steps": len(ordered_step_ids),
            "dependencies": len(normalized_dependencies),
            "topological_step_ids": ordered_step_ids,
        },
    )
    ordered_steps: list[AppointmentRescheduleStep] = []
    for chain_position, step_id in enumerate(ordered_step_ids, start=1):
        step = step_by_id[step_id]
        step.chain = chain
        step.chain_position = chain_position
        step.chain_required = True
        step.full_clean()
        step.save(update_fields=["chain", "chain_position", "chain_required", "updated_at"])
        ordered_steps.append(step)

    created_dependencies: list[AppointmentRescheduleStepDependency] = []
    for predecessor_id, successor_id, relation_type, reason, snapshot in normalized_dependencies:
        dependency = AppointmentRescheduleStepDependency(
            plan=plan,
            chain=chain,
            predecessor_step=step_by_id[predecessor_id],
            successor_step=step_by_id[successor_id],
            relation_type=relation_type,
            reason=reason,
            snapshot=snapshot,
        )
        dependency.full_clean()
        dependency.save()
        created_dependencies.append(dependency)

    return ChainBuildResult(
        chain=chain,
        steps=ordered_steps,
        dependencies=created_dependencies,
    )


def _finish_plan_if_all_steps_terminal(
    plan: AppointmentReschedulePlan,
    *,
    actor: Any = None,
) -> None:
    if plan.steps.exclude(status__in=TERMINAL_STEP_STATUSES).exists():
        return
    plan.status = AppointmentReschedulePlan.Status.APPLIED
    plan.applied_by = actor if getattr(actor, "is_authenticated", False) else None
    plan.applied_at = timezone.now()
    plan.save(update_fields=["status", "applied_by", "applied_at", "updated_at"])


def _skip_alternative_steps_for_source(applied_step: AppointmentRescheduleStep) -> None:
    note = "Пропущено: применен другой вариант переноса этого занятия."
    alternatives = (
        AppointmentRescheduleStep.objects.select_for_update()
        .filter(
            plan_id=applied_step.plan_id,
            source_appointment_id=applied_step.source_appointment_id,
        )
        .exclude(pk=applied_step.pk)
        .exclude(status__in=TERMINAL_STEP_STATUSES)
        .order_by("position")
    )
    for alternative in alternatives:
        alternative.status = AppointmentRescheduleStep.Status.SKIPPED
        if note not in alternative.admin_note:
            alternative.admin_note = "\n".join(
                part for part in [alternative.admin_note, note] if part
            )
        alternative.save(update_fields=["status", "admin_note", "updated_at"])


def _appointment_children(appointment: Appointment) -> list[Any]:
    participants = list(appointment.participants.select_related("child").order_by("pk"))
    if not participants:
        return [appointment.child]
    children = [participant.child for participant in participants]
    if appointment.child_id and all(child.pk != appointment.child_id for child in children):
        children.insert(0, appointment.child)
    return children


def _appointment_staff_for_move(
    appointment: Appointment, selected_staff: StaffMember
) -> list[StaffMember]:
    assignments = list(
        appointment.staff_assignments.select_related("staff_member").order_by("pk")
    )
    if not assignments:
        return [selected_staff]

    members: list[StaffMember] = []
    seen: set[int] = set()
    primary_replaced = False
    for assignment in assignments:
        staff = assignment.staff_member
        if assignment.role == AppointmentStaffAssignment.Role.PRIMARY and not primary_replaced:
            staff = selected_staff
            primary_replaced = True
        if staff.pk in seen:
            continue
        members.append(staff)
        seen.add(staff.pk)
    if selected_staff.pk not in seen:
        members.insert(0, selected_staff)
    return members


def _participant_snapshot(appointment: Appointment) -> list[dict[str, Any]]:
    participants = list(appointment.participants.select_related("child").order_by("pk"))
    if not participants:
        return [
            {
                "participant_id": None,
                "child_id": appointment.child_id,
                "label": str(appointment.child),
                "legacy": True,
            }
        ]
    rows = [
        {
            "participant_id": participant.pk,
            "child_id": participant.child_id,
            "label": str(participant.child),
            "legacy": False,
        }
        for participant in participants
    ]
    if appointment.child_id and all(row["child_id"] != appointment.child_id for row in rows):
        rows.insert(
            0,
            {
                "participant_id": None,
                "child_id": appointment.child_id,
                "label": str(appointment.child),
                "legacy": True,
            },
        )
    return rows


def _staff_snapshot(
    appointment: Appointment, selected_staff: StaffMember | None = None
) -> list[dict[str, Any]]:
    assignments = list(
        appointment.staff_assignments.select_related("staff_member").order_by("pk")
    )
    if not assignments:
        staff = selected_staff or appointment.staff_member
        return [
            {
                "assignment_id": None,
                "staff_member_id": staff.pk if staff else None,
                "label": str(staff) if staff else "",
                "role": AppointmentStaffAssignment.Role.PRIMARY,
                "legacy": True,
            }
        ]

    rows: list[dict[str, Any]] = []
    primary_replaced = False
    for assignment in assignments:
        staff = assignment.staff_member
        if (
            selected_staff
            and assignment.role == AppointmentStaffAssignment.Role.PRIMARY
            and not primary_replaced
        ):
            staff = selected_staff
            primary_replaced = True
        rows.append(
            {
                "assignment_id": assignment.pk,
                "staff_member_id": staff.pk,
                "label": str(staff),
                "role": assignment.role,
                "legacy": False,
            }
        )
    return rows


def _representative_targets(
    appointment: Appointment,
) -> list[tuple[ParentGuardian, AppointmentParticipant | None]]:
    targets: list[tuple[ParentGuardian, AppointmentParticipant | None]] = []
    participants = list(
        appointment.participants.select_related(
            "child", "child__primary_parent"
        ).order_by("pk")
    )
    if participants:
        participant_rows: list[tuple[Any, AppointmentParticipant | None]] = [
            (participant.child, participant) for participant in participants
        ]
        if appointment.child_id and all(
            participant.child_id != appointment.child_id for participant in participants
        ):
            participant_rows.insert(0, (appointment.child, None))
    else:
        participant_rows = [(appointment.child, None)]

    for child, participant in participant_rows:
        representative_links = list(
            child.representative_links.select_related("representative").order_by(
                "-is_primary", "representative__last_name", "representative__first_name"
            )
        )
        if representative_links:
            for link in representative_links:
                if link.receives_schedule and link.representative.email:
                    targets.append((link.representative, participant))
            continue
        if child.primary_parent_id and child.primary_parent.email:
            targets.append((child.primary_parent, participant))
    return targets


def _staff_targets(
    appointment: Appointment,
    selected_staff: StaffMember,
) -> list[tuple[StaffMember, AppointmentStaffAssignment | None]]:
    assignments = list(
        appointment.staff_assignments.select_related("staff_member", "staff_member__user").order_by(
            "pk"
        )
    )
    if not assignments:
        return [(selected_staff, None)]

    targets: list[tuple[StaffMember, AppointmentStaffAssignment | None]] = []
    seen: set[int] = set()
    primary_replaced = False
    for assignment in assignments:
        staff = assignment.staff_member
        staff_assignment: AppointmentStaffAssignment | None = assignment
        if assignment.role == AppointmentStaffAssignment.Role.PRIMARY and not primary_replaced:
            staff = selected_staff
            primary_replaced = True
            if selected_staff.pk != assignment.staff_member_id:
                staff_assignment = None
        if staff.pk in seen:
            continue
        seen.add(staff.pk)
        targets.append((staff, staff_assignment))
    if selected_staff.pk not in seen:
        targets.insert(0, (selected_staff, None))
    return targets


def _conflict_snapshot(conflicts: dict[str, Any]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for key in ("child", "staff", "room"):
        qs = conflicts.get(key)
        if not qs:
            snapshot[key] = []
            continue
        snapshot[key] = [
            {
                "appointment_id": appointment.pk,
                "label": str(appointment),
                "starts_at": appointment.starts_at.isoformat(),
            }
            for appointment in qs.select_related("child", "staff_member", "service", "room")
            .order_by("starts_at", "pk")
            .distinct()[:5]
        ]
    snapshot["room_limit_reasons"] = conflicts.get("room_limit_reasons", {})
    return snapshot


def _blocking_appointment(conflicts: dict[str, Any]) -> Appointment | None:
    for key in ("child", "staff", "room"):
        qs = conflicts.get(key)
        if qs:
            appointment = qs.order_by("starts_at", "pk").first()
            if appointment:
                return appointment
    return None


def _step_messages(
    appointment: Appointment,
    starts_at,
    ends_at,
    selected_staff: StaffMember,
    room,
    *,
    exclude_appointment_ids: set[int] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    children = _appointment_children(appointment)
    staff_members = _appointment_staff_for_move(appointment, selected_staff)
    excluded_ids = {appointment.pk}
    if exclude_appointment_ids:
        excluded_ids.update(exclude_appointment_ids)
    conflicts = appointment_group_conflicts(
        starts_at,
        ends_at,
        children,
        staff_members,
        room,
        exclude_pks=excluded_ids,
    )
    messages = conflict_messages(conflicts)
    for staff in staff_members:
        unavailable = staff_unavailability_reason(staff, starts_at, ends_at)
        if unavailable:
            messages.append(f"{staff.full_name}: {unavailable}")
    return messages, conflicts


def _slot_times(day, clock: str, duration_minutes: int):
    parsed_time = datetime.strptime(clock, "%H:%M").time()
    starts_at = build_local_datetime(day, parsed_time)
    return starts_at, starts_at + timedelta(minutes=duration_minutes)


def _active_staff_appointments(
    staff: StaffMember,
    *,
    date_from: date,
    date_to: date,
) -> list[Appointment]:
    if date_to < date_from:
        return []
    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(date_from, datetime.min.time()), tz)
    end_dt = timezone.make_aware(datetime.combine(date_to, datetime.max.time()), tz)
    return list(
        Appointment.objects.filter(
            Q(staff_member=staff) | Q(staff_assignments__staff_member=staff),
            starts_at__gte=start_dt,
            starts_at__lte=end_dt,
            status__in=ACTIVE_APPOINTMENT_STATUSES,
        )
        .distinct()
        .select_related("child", "staff_member", "service", "room")
        .prefetch_related(
            "participants__child",
            "staff_assignments__staff_member",
        )
        .order_by("starts_at", "pk")
    )


def _step_time_label(step: AppointmentRescheduleStep) -> str:
    local_start = timezone.localtime(step.proposed_starts_at)
    local_end = timezone.localtime(step.proposed_ends_at)
    return f"{local_start:%d.%m.%Y %H:%M}-{local_end:%H:%M}"


def _confirmation_message(
    step: AppointmentRescheduleStep,
    *,
    target_label: str,
    target_role: str,
) -> tuple[str, str]:
    appointment = step.source_appointment
    time_label = _step_time_label(step)
    room_label = step.proposed_room.name if step.proposed_room_id else "кабинет не указан"
    staff_label = (
        step.proposed_primary_staff.full_name
        if step.proposed_primary_staff_id
        else appointment.staff_member.full_name
    )
    subject = f"Согласование переноса занятия {time_label}"
    message = "\n".join(
        [
            "Здравствуйте.",
            "",
            "Просим согласовать перенос занятия:",
            f"Адресат: {target_label}",
            f"Роль: {target_role}",
            f"Получатель: {appointment.participant_label()}",
            f"Услуга: {appointment.service.name}",
            f"Новое время: {time_label}",
            f"Специалист: {staff_label}",
            f"Кабинет: {room_label}",
            "",
            "Ответьте по ссылке ниже: подтвердить или отклонить.",
            "Подтверждение не переносит занятие автоматически; решение применяет администратор.",
        ]
    )
    return subject, message


def _existing_confirmation(
    step: AppointmentRescheduleStep,
    *,
    target_type: str,
    email: str,
    participant: AppointmentParticipant | None = None,
    staff_assignment: AppointmentStaffAssignment | None = None,
    representative: ParentGuardian | None = None,
) -> AppointmentConfirmation | None:
    qs = AppointmentConfirmation.objects.filter(
        reschedule_step=step,
        target_type=target_type,
        email=email,
    )
    if participant is not None:
        qs = qs.filter(participant=participant)
    else:
        qs = qs.filter(participant__isnull=True)
    if staff_assignment is not None:
        qs = qs.filter(staff_assignment=staff_assignment)
    else:
        qs = qs.filter(staff_assignment__isnull=True)
    if representative is not None:
        qs = qs.filter(representative=representative)
    else:
        qs = qs.filter(representative__isnull=True)
    return qs.first()


def _confirmation_state_for_step(
    step: AppointmentRescheduleStep,
) -> tuple[str, dict[str, Any]]:
    rows = list(
        AppointmentConfirmation.objects.filter(reschedule_step=step).values(
            "status",
            "target_type",
        )
    )
    summary: dict[str, Any] = {
        "total": len(rows),
        "pending": 0,
        "confirmed": 0,
        "declined": 0,
        "specialist": 0,
        "representative": 0,
        "recipient": 0,
    }
    for row in rows:
        status = row["status"]
        target_type = row["target_type"]
        if status == AppointmentConfirmation.Status.PENDING:
            summary["pending"] += 1
        elif status == AppointmentConfirmation.Status.CONFIRMED:
            summary["confirmed"] += 1
        elif status == AppointmentConfirmation.Status.DECLINED:
            summary["declined"] += 1
        if target_type in summary:
            summary[target_type] += 1

    if not rows:
        status = AppointmentRescheduleStep.ConfirmationStatus.NOT_REQUESTED
    elif summary["declined"]:
        status = AppointmentRescheduleStep.ConfirmationStatus.DECLINED
    elif summary["pending"]:
        status = AppointmentRescheduleStep.ConfirmationStatus.WAITING
    else:
        status = AppointmentRescheduleStep.ConfirmationStatus.APPROVED
    return status, summary


def _update_step_confirmation_state(
    step: AppointmentRescheduleStep,
) -> AppointmentRescheduleStep:
    status, summary = _confirmation_state_for_step(step)
    step.confirmation_status = status
    step.confirmation_summary = summary
    step.save(update_fields=["confirmation_status", "confirmation_summary", "updated_at"])
    return step


@transaction.atomic
def refresh_step_confirmation_status(
    step: AppointmentRescheduleStep,
) -> AppointmentRescheduleStep:
    step = AppointmentRescheduleStep.objects.select_for_update().get(pk=step.pk)
    return _update_step_confirmation_state(step)


@transaction.atomic
def create_plan_for_appointment(
    appointment: Appointment,
    *,
    actor: Any = None,
    days: int = 7,
    limit: int = 12,
) -> AppointmentReschedulePlan:
    start_day = max(timezone.localdate(), timezone.localtime(appointment.starts_at).date())
    duration = appointment.duration_minutes
    staff_members = StaffMember.objects.filter(status=StaffMember.Status.ACTIVE).order_by(
        "full_name"
    )
    plan = AppointmentReschedulePlan.objects.create(
        status=AppointmentReschedulePlan.Status.DRAFT,
        plan_type=AppointmentReschedulePlan.PlanType.SINGLE_MOVE,
        root_appointment=appointment,
        staff_member=appointment.staff_member,
        date_from=start_day,
        date_to=start_day + timedelta(days=max(days - 1, 0)),
        reason="План переноса занятия.",
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )

    position = 1
    for day_offset in range(days):
        day = start_day + timedelta(days=day_offset)
        for staff_member in staff_members:
            for minute in range(9 * 60, (18 * 60) - duration + 1, 30):
                hour, clock_minute = divmod(minute, 60)
                starts_at, ends_at = _slot_times(
                    day, f"{hour:02d}:{clock_minute:02d}", duration
                )
                if starts_at == appointment.starts_at:
                    continue
                messages, conflicts = _step_messages(
                    appointment, starts_at, ends_at, staff_member, appointment.room
                )
                if messages:
                    continue
                AppointmentRescheduleStep.objects.create(
                    plan=plan,
                    position=position,
                    action_type=AppointmentRescheduleStep.ActionType.MOVE,
                    status=AppointmentRescheduleStep.Status.VALID,
                    source_appointment=appointment,
                    proposed_starts_at=starts_at,
                    proposed_ends_at=ends_at,
                    proposed_room=appointment.room,
                    proposed_primary_staff=staff_member,
                    participant_snapshot=_participant_snapshot(appointment),
                    staff_snapshot=_staff_snapshot(appointment, staff_member),
                    conflict_snapshot=_conflict_snapshot(conflicts),
                    validation_messages=[],
                )
                position += 1
                if position > limit:
                    break
            if position > limit:
                break
        if position > limit:
            break

    if position == 1:
        plan.plan_type = AppointmentReschedulePlan.PlanType.CASCADE_SHIFT
        for day_offset in range(days):
            day = start_day + timedelta(days=day_offset)
            for staff_member in staff_members:
                for minute in range(9 * 60, (18 * 60) - duration + 1, 30):
                    hour, clock_minute = divmod(minute, 60)
                    starts_at, ends_at = _slot_times(
                        day, f"{hour:02d}:{clock_minute:02d}", duration
                    )
                    if starts_at == appointment.starts_at:
                        continue
                    messages, conflicts = _step_messages(
                        appointment, starts_at, ends_at, staff_member, appointment.room
                    )
                    if not messages:
                        continue
                    AppointmentRescheduleStep.objects.create(
                        plan=plan,
                        position=position,
                        action_type=AppointmentRescheduleStep.ActionType.REVIEW_CONFLICT,
                        status=AppointmentRescheduleStep.Status.PENDING,
                        source_appointment=appointment,
                        blocking_appointment=_blocking_appointment(conflicts),
                        proposed_starts_at=starts_at,
                        proposed_ends_at=ends_at,
                        proposed_room=appointment.room,
                        proposed_primary_staff=staff_member,
                        participant_snapshot=_participant_snapshot(appointment),
                        staff_snapshot=_staff_snapshot(appointment, staff_member),
                        conflict_snapshot=_conflict_snapshot(conflicts),
                        validation_messages=messages,
                        requires_staff_override=any(
                            ":" in message and "специалист" not in message
                            for message in messages
                        ),
                        requires_room_override=any("кабинет" in message for message in messages),
                    )
                    position += 1
                    if position > limit:
                        break
                if position > limit:
                    break
            if position > limit:
                break

    step_count = position - 1
    plan.status = (
        AppointmentReschedulePlan.Status.READY
        if step_count
        else AppointmentReschedulePlan.Status.NEEDS_RECHECK
    )
    plan.validation_summary = {
        "steps": step_count,
        "valid": plan.steps.filter(status=AppointmentRescheduleStep.Status.VALID).count(),
        "pending": plan.steps.filter(status=AppointmentRescheduleStep.Status.PENDING).count(),
        "stale": 0,
    }
    plan.save(update_fields=["status", "plan_type", "validation_summary", "updated_at"])
    return plan


@transaction.atomic
def create_staff_absence_plan(
    staff: StaffMember,
    *,
    date_from: date,
    date_to: date,
    reason: str,
    actor: Any = None,
) -> AppointmentReschedulePlan:
    if date_to < date_from:
        raise ValidationError("Дата окончания не может быть раньше даты начала.")
    reason = reason.strip()
    if not reason:
        raise ValidationError("Укажите причину отсутствия специалиста.")

    plan = AppointmentReschedulePlan.objects.create(
        status=AppointmentReschedulePlan.Status.DRAFT,
        plan_type=AppointmentReschedulePlan.PlanType.STAFF_ABSENCE,
        staff_member=staff,
        date_from=date_from,
        date_to=date_to,
        reason=reason,
        created_by=actor if getattr(actor, "is_authenticated", False) else None,
    )
    appointments = _active_staff_appointments(staff, date_from=date_from, date_to=date_to)
    for position, appointment in enumerate(appointments, start=1):
        AppointmentRescheduleStep.objects.create(
            plan=plan,
            position=position,
            action_type=AppointmentRescheduleStep.ActionType.REVIEW_CONFLICT,
            status=AppointmentRescheduleStep.Status.PENDING,
            source_appointment=appointment,
            proposed_starts_at=appointment.starts_at,
            proposed_ends_at=appointment.ends_at,
            proposed_room=appointment.room,
            proposed_primary_staff=staff,
            participant_snapshot=_participant_snapshot(appointment),
            staff_snapshot=_staff_snapshot(appointment),
            conflict_snapshot={
                "staff_absence": {
                    "staff_member_id": staff.pk,
                    "staff_member": staff.full_name,
                    "reason": reason,
                }
            },
            validation_messages=[
                f"Отсутствие специалиста {staff.full_name}: {reason}. Требуется ручное решение."
            ],
        )

    step_count = len(appointments)
    plan.status = (
        AppointmentReschedulePlan.Status.READY
        if step_count
        else AppointmentReschedulePlan.Status.NEEDS_RECHECK
    )
    plan.validation_summary = {
        "steps": step_count,
        "pending": step_count,
        "valid": 0,
        "stale": 0,
        "staff_absence": True,
    }
    plan.save(update_fields=["status", "validation_summary", "updated_at"])
    return plan


@transaction.atomic
def create_confirmations_for_step(
    step: AppointmentRescheduleStep,
    *,
    actor: Any = None,
) -> StepConfirmationResult:
    step = (
        AppointmentRescheduleStep.objects.select_for_update(
            of=("self", "plan", "source_appointment")
        )
        .select_related(
            "plan",
            "source_appointment",
            "source_appointment__child",
            "source_appointment__service",
            "source_appointment__staff_member",
            "proposed_primary_staff",
            "proposed_room",
        )
        .prefetch_related(
            "source_appointment__participants__child__representative_links__representative",
            "source_appointment__staff_assignments__staff_member__user",
        )
        .get(pk=step.pk)
    )
    _raise_if_terminal_plan(step.plan)
    if step.action_type != AppointmentRescheduleStep.ActionType.MOVE:
        raise ValidationError("Согласования можно отправить только для шага переноса.")
    if step.status != AppointmentRescheduleStep.Status.VALID:
        raise ValidationError("Перед отправкой согласований шаг должен быть валидным.")

    created: list[AppointmentConfirmation] = []
    existing: list[AppointmentConfirmation] = []
    appointment = step.source_appointment
    sent_by = actor if getattr(actor, "is_authenticated", False) else None

    for representative, participant in _representative_targets(appointment):
        subject, message = _confirmation_message(
            step,
            target_label=representative.full_name,
            target_role="представитель",
        )
        duplicate = _existing_confirmation(
            step,
            target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE,
            email=representative.email,
            participant=participant,
            representative=representative,
        )
        if duplicate:
            existing.append(duplicate)
            continue
        created.append(
            AppointmentConfirmation.objects.create(
                appointment=appointment,
                reschedule_step=step,
                target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE,
                representative=representative,
                participant=participant,
                email=representative.email,
                subject=subject,
                message=message,
                sent_by=sent_by,
            )
        )

    selected_staff = step.proposed_primary_staff or appointment.staff_member
    for staff, assignment in _staff_targets(appointment, selected_staff):
        staff_email = staff.email or (staff.user.email if staff.user_id else "")
        if not staff_email:
            continue
        subject, message = _confirmation_message(
            step,
            target_label=staff.full_name,
            target_role="специалист",
        )
        duplicate = _existing_confirmation(
            step,
            target_type=AppointmentConfirmation.TargetType.SPECIALIST,
            email=staff_email,
            staff_assignment=assignment,
        )
        if duplicate:
            existing.append(duplicate)
            continue
        created.append(
            AppointmentConfirmation.objects.create(
                appointment=appointment,
                reschedule_step=step,
                target_type=AppointmentConfirmation.TargetType.SPECIALIST,
                staff_assignment=assignment,
                email=staff_email,
                subject=subject,
                message=message,
                sent_by=sent_by,
            )
        )

    step = _update_step_confirmation_state(step)
    return StepConfirmationResult(step=step, created=created, existing=existing)


def _refresh_move_step_validation(
    step: AppointmentRescheduleStep,
    *,
    exclude_appointment_ids: set[int] | None = None,
) -> list[str]:
    messages, conflicts = _step_messages(
        step.source_appointment,
        step.proposed_starts_at,
        step.proposed_ends_at,
        step.proposed_primary_staff,
        step.proposed_room,
        exclude_appointment_ids=exclude_appointment_ids,
    )
    step.validation_messages = messages
    step.conflict_snapshot = _conflict_snapshot(conflicts)
    step.requires_staff_override = any(":" in message for message in messages)
    room_conflicts = conflicts.get("room")
    step.requires_room_override = bool(
        conflicts.get("room_over_limit") or (room_conflicts and room_conflicts.exists())
    )
    step.status = (
        AppointmentRescheduleStep.Status.STALE
        if messages
        else AppointmentRescheduleStep.Status.VALID
    )
    step.save(
        update_fields=[
            "status",
            "validation_messages",
            "conflict_snapshot",
            "requires_staff_override",
            "requires_room_override",
            "updated_at",
        ]
    )
    return messages


@transaction.atomic
def revalidate_plan(plan: AppointmentReschedulePlan) -> PlanValidationResult:
    plan = AppointmentReschedulePlan.objects.select_for_update().get(pk=plan.pk)
    _raise_if_terminal_plan(plan, verb="перепроверять")

    valid_steps = 0
    stale_steps = 0
    pending_steps = 0
    for step in plan.steps.select_related(
        "source_appointment",
        "proposed_primary_staff",
        "proposed_room",
    ).order_by("position"):
        if step.status in {
            AppointmentRescheduleStep.Status.APPLIED,
            AppointmentRescheduleStep.Status.SKIPPED,
        }:
            continue
        if step.action_type != AppointmentRescheduleStep.ActionType.MOVE:
            pending_steps += 1
            continue
        messages, conflicts = _step_messages(
            step.source_appointment,
            step.proposed_starts_at,
            step.proposed_ends_at,
            step.proposed_primary_staff,
            step.proposed_room,
        )
        step.validation_messages = messages
        step.conflict_snapshot = _conflict_snapshot(conflicts)
        step.requires_staff_override = any(
            ":" in message and "специалист" not in message for message in messages
        )
        step.requires_room_override = any("кабинет" in message for message in messages)
        step.status = (
            AppointmentRescheduleStep.Status.STALE
            if messages
            else AppointmentRescheduleStep.Status.VALID
        )
        step.save(
            update_fields=[
                "status",
                "validation_messages",
                "conflict_snapshot",
                "requires_staff_override",
                "requires_room_override",
                "updated_at",
            ]
        )
        if messages:
            stale_steps += 1
        else:
            valid_steps += 1

    plan.status = (
        AppointmentReschedulePlan.Status.NEEDS_RECHECK
        if stale_steps
        else AppointmentReschedulePlan.Status.READY
    )
    plan.validation_summary = {
        "valid": valid_steps,
        "stale": stale_steps,
        "pending": pending_steps,
    }
    plan.save(update_fields=["status", "validation_summary", "updated_at"])
    return PlanValidationResult(plan=plan, valid_steps=valid_steps, stale_steps=stale_steps, pending_steps=pending_steps)


@transaction.atomic
def revalidate_chain(chain: AppointmentRescheduleChain) -> ChainValidationResult:
    chain = (
        AppointmentRescheduleChain.objects.select_for_update(of=("self", "plan"))
        .select_related("plan")
        .get(pk=chain.pk)
    )
    if chain.status in {
        AppointmentRescheduleChain.Status.APPLYING,
        AppointmentRescheduleChain.Status.APPLIED,
        AppointmentRescheduleChain.Status.CANCELLED,
    }:
        raise ValidationError("Completed chains cannot be revalidated.")
    _raise_if_terminal_plan(chain.plan, verb="перепроверять")

    steps = list(
        AppointmentRescheduleStep.objects.select_for_update(
            of=("self", "source_appointment")
        )
        .select_related(
            "source_appointment",
            "proposed_primary_staff",
            "proposed_room",
        )
        .filter(chain=chain)
        .order_by("chain_position", "position", "pk")
    )
    dependencies = list(
        AppointmentRescheduleStepDependency.objects.select_for_update()
        .select_related("predecessor_step", "successor_step")
        .filter(chain=chain)
        .order_by("predecessor_step__chain_position", "successor_step__chain_position", "pk")
    )

    issues: list[dict[str, Any]] = []
    step_ids = {step.pk for step in steps}
    blocked_step_ids: set[int] = set()
    stale_step_ids: set[int] = set()
    invalid_step_ids: set[int] = set()
    edge_keys: set[tuple[int, int, str]] = set()
    dependency_edges: list[tuple[int, int]] = []
    connected_step_ids: set[int] = set()
    freed_source_ids_by_successor: dict[int, set[int]] = {step.pk: set() for step in steps}

    if len(steps) < 2:
        issues.append(
            {
                "code": "not_enough_steps",
                "message": "Chain requires at least two steps.",
            }
        )
    if not dependencies:
        issues.append(
            {
                "code": "missing_dependencies",
                "message": "Chain requires at least one dependency.",
            }
        )

    source_ids = [step.source_appointment_id for step in steps]
    if len(source_ids) != len(set(source_ids)):
        issues.append(
            {
                "code": "duplicate_source_appointment",
                "message": "Chain has several steps for one source appointment.",
            }
        )

    step_by_id = {step.pk: step for step in steps}
    for step in steps:
        if step.plan_id != chain.plan_id:
            invalid_step_ids.add(step.pk)
            issues.append(
                {
                    "code": "step_plan_mismatch",
                    "step_id": step.pk,
                    "message": "Step does not belong to the chain plan.",
                }
            )
        if step.action_type != AppointmentRescheduleStep.ActionType.MOVE:
            invalid_step_ids.add(step.pk)
            issues.append(
                {
                    "code": "step_not_move",
                    "step_id": step.pk,
                    "message": "Chain can only revalidate move steps.",
                }
            )
        if step.status in TERMINAL_STEP_STATUSES:
            invalid_step_ids.add(step.pk)
            issues.append(
                {
                    "code": "terminal_step",
                    "step_id": step.pk,
                    "message": "Step is already applied or skipped.",
                }
            )
        missing_fields = []
        if not step.proposed_starts_at:
            missing_fields.append("proposed_starts_at")
        if not step.proposed_ends_at:
            missing_fields.append("proposed_ends_at")
        if not step.proposed_primary_staff_id:
            missing_fields.append("proposed_primary_staff")
        if missing_fields:
            invalid_step_ids.add(step.pk)
            issues.append(
                {
                    "code": "missing_proposed_fields",
                    "step_id": step.pk,
                    "fields": missing_fields,
                    "message": "Step has incomplete target slot data.",
                }
            )

    for dependency in dependencies:
        predecessor_id = dependency.predecessor_step_id
        successor_id = dependency.successor_step_id
        edge_key = (predecessor_id, successor_id, dependency.relation_type)
        if dependency.plan_id != chain.plan_id:
            issues.append(
                {
                    "code": "dependency_plan_mismatch",
                    "dependency_id": dependency.pk,
                    "message": "Dependency does not belong to the chain plan.",
                }
            )
        if predecessor_id == successor_id:
            issues.append(
                {
                    "code": "self_dependency",
                    "dependency_id": dependency.pk,
                    "message": "Step cannot depend on itself.",
                }
            )
        if predecessor_id not in step_ids or successor_id not in step_ids:
            issues.append(
                {
                    "code": "dependency_step_outside_chain",
                    "dependency_id": dependency.pk,
                    "message": "Dependency points to a step outside this chain.",
                }
            )
            continue
        if edge_key in edge_keys:
            issues.append(
                {
                    "code": "duplicate_dependency",
                    "dependency_id": dependency.pk,
                    "message": "Dependency is duplicated.",
                }
            )
        edge_keys.add(edge_key)
        dependency_edges.append((predecessor_id, successor_id))
        connected_step_ids.update([predecessor_id, successor_id])
        if dependency.relation_type == AppointmentRescheduleStepDependency.RelationType.FREES_TARGET_SLOT:
            freed_source_ids_by_successor[successor_id].add(
                step_by_id[predecessor_id].source_appointment_id
            )

    ordered_step_ids: list[int] = []
    if step_ids and dependency_edges:
        if connected_step_ids != step_ids:
            issues.append(
                {
                    "code": "disconnected_steps",
                    "message": "Every chain step must participate in at least one dependency.",
                }
            )
        try:
            ordered_step_ids = _topological_step_ids(
                step_ids,
                dependency_edges,
                position_by_step_id={
                    step.pk: step.chain_position or step.position for step in steps
                },
            )
        except ValidationError as exc:
            issues.append(
                {
                    "code": "dependency_cycle",
                    "message": "; ".join(exc.messages),
                }
            )

    for step in steps:
        if step.pk in invalid_step_ids:
            blocked_step_ids.add(step.pk)
            continue
        step = _update_step_confirmation_state(step)
        if step.confirmation_status in {
            AppointmentRescheduleStep.ConfirmationStatus.WAITING,
            AppointmentRescheduleStep.ConfirmationStatus.DECLINED,
        }:
            blocked_step_ids.add(step.pk)
            issues.append(
                {
                    "code": "confirmation_blocked",
                    "step_id": step.pk,
                    "confirmation_status": step.confirmation_status,
                    "message": "Step is blocked by confirmations.",
                }
            )
        messages = _refresh_move_step_validation(
            step,
            exclude_appointment_ids=freed_source_ids_by_successor.get(step.pk, set()),
        )
        if messages:
            stale_step_ids.add(step.pk)

    ready_step_ids = {
        step.pk
        for step in steps
        if step.pk not in stale_step_ids
        and step.pk not in blocked_step_ids
        and step.pk not in invalid_step_ids
    }
    chain.status = (
        AppointmentRescheduleChain.Status.READY
        if not issues and len(ready_step_ids) == len(steps) and steps
        else AppointmentRescheduleChain.Status.STALE
    )
    chain.validation_summary = {
        "structural": "ok" if not issues else "blocked",
        "steps": len(steps),
        "dependencies": len(dependencies),
        "topological_step_ids": ordered_step_ids,
        "ready": len(ready_step_ids),
        "stale": len(stale_step_ids),
        "blocked": len(blocked_step_ids),
        "issues": issues,
        "checked_at": timezone.now().isoformat(),
    }
    chain.save(update_fields=["status", "validation_summary", "updated_at"])
    return ChainValidationResult(
        chain=chain,
        ready_steps=len(ready_step_ids),
        stale_steps=len(stale_step_ids),
        blocked_steps=len(blocked_step_ids),
        dependency_count=len(dependencies),
    )


@transaction.atomic
def _mark_chain_apply_failed(chain_pk: int, messages: list[str]) -> None:
    chain = AppointmentRescheduleChain.objects.select_for_update().get(pk=chain_pk)
    if chain.status in {
        AppointmentRescheduleChain.Status.APPLIED,
        AppointmentRescheduleChain.Status.CANCELLED,
    }:
        return
    summary = dict(chain.validation_summary or {})
    summary["apply_error"] = messages
    summary["failed_at"] = timezone.now().isoformat()
    chain.status = AppointmentRescheduleChain.Status.FAILED
    chain.validation_summary = summary
    chain.save(update_fields=["status", "validation_summary", "updated_at"])


def apply_chain(
    chain: AppointmentRescheduleChain,
    *,
    actor: Any = None,
) -> ChainApplyResult:
    chain_pk = chain.pk
    mark_failed = False
    persist_revalidation = False
    try:
        with transaction.atomic():
            chain = (
                AppointmentRescheduleChain.objects.select_for_update(of=("self", "plan"))
                .select_related("plan")
                .get(pk=chain_pk)
            )
            _raise_if_terminal_plan(chain.plan)
            if chain.status != AppointmentRescheduleChain.Status.READY:
                raise ValidationError("Chain must be ready before applying.")

            validation = revalidate_chain(chain)
            chain = validation.chain
            if chain.status != AppointmentRescheduleChain.Status.READY:
                persist_revalidation = True
                raise ValidationError("Chain is not ready after revalidation.")

            steps = list(
                AppointmentRescheduleStep.objects.select_for_update(
                    of=("self", "source_appointment")
                )
                .select_related(
                    "source_appointment",
                    "proposed_primary_staff",
                    "proposed_room",
                )
                .filter(chain=chain)
                .order_by("chain_position", "position", "pk")
            )
            dependencies = list(
                AppointmentRescheduleStepDependency.objects.select_for_update()
                .filter(chain=chain)
                .order_by(
                    "predecessor_step__chain_position",
                    "successor_step__chain_position",
                    "pk",
                )
            )
            step_ids = {step.pk for step in steps}
            ordered_step_ids = _topological_step_ids(
                step_ids,
                [(dep.predecessor_step_id, dep.successor_step_id) for dep in dependencies],
                position_by_step_id={
                    step.pk: step.chain_position or step.position for step in steps
                },
            )
            if len(ordered_step_ids) != len(steps):
                raise ValidationError("Chain apply order is incomplete.")

            list(
                Appointment.objects.select_for_update()
                .filter(pk__in=[step.source_appointment_id for step in steps])
                .order_by("pk")
            )

            chain.status = AppointmentRescheduleChain.Status.APPLYING
            chain.save(update_fields=["status", "updated_at"])
            mark_failed = True

            step_by_id = {step.pk: step for step in steps}
            applied_steps: list[AppointmentRescheduleStep] = []
            for step_id in ordered_step_ids:
                applied_steps.append(apply_step(step_by_id[step_id], actor=actor))

            applied_at = timezone.now()
            summary = dict(chain.validation_summary or {})
            summary["applied"] = len(applied_steps)
            summary["applied_step_ids"] = [step.pk for step in applied_steps]
            summary["applied_at"] = applied_at.isoformat()
            chain.status = AppointmentRescheduleChain.Status.APPLIED
            chain.applied_by = actor if getattr(actor, "is_authenticated", False) else None
            chain.applied_at = applied_at
            chain.validation_summary = summary
            chain.save(
                update_fields=[
                    "status",
                    "applied_by",
                    "applied_at",
                    "validation_summary",
                    "updated_at",
                ]
            )
            _finish_plan_if_all_steps_terminal(chain.plan, actor=actor)
            return ChainApplyResult(chain=chain, applied_steps=applied_steps)
    except ValidationError as exc:
        if mark_failed:
            _mark_chain_apply_failed(chain_pk, exc.messages)
        elif persist_revalidation:
            revalidate_chain(AppointmentRescheduleChain(pk=chain_pk))
        raise
    except Exception as exc:
        if mark_failed:
            _mark_chain_apply_failed(chain_pk, [str(exc) or exc.__class__.__name__])
        raise


@transaction.atomic
def mark_review_conflict_step_resolved(
    step: AppointmentRescheduleStep,
    *,
    actor: Any = None,
) -> AppointmentRescheduleStep:
    step = (
        AppointmentRescheduleStep.objects.select_for_update()
        .select_related("plan", "source_appointment")
        .get(pk=step.pk)
    )
    _raise_if_terminal_plan(step.plan)
    if step.action_type != AppointmentRescheduleStep.ActionType.REVIEW_CONFLICT:
        raise ValidationError("Разобранным можно отметить только шаг ручного конфликта.")
    if step.status in TERMINAL_STEP_STATUSES:
        _finish_plan_if_all_steps_terminal(step.plan, actor=actor)
        return step

    appointment = Appointment.objects.select_for_update().get(pk=step.source_appointment_id)
    if appointment.status in ACTIVE_APPOINTMENT_STATUSES:
        raise ValidationError(
            "Сначала перенесите или отмените занятие, затем отметьте конфликт разобранным."
        )

    status_label = appointment.get_status_display()
    note = f"Разобрано вручную: исходное занятие в статусе «{status_label}»."
    if step.admin_note:
        if note not in step.admin_note:
            step.admin_note = f"{step.admin_note.rstrip()}\n{note}"
    else:
        step.admin_note = note
    step.status = AppointmentRescheduleStep.Status.SKIPPED
    step.save(update_fields=["status", "admin_note", "updated_at"])

    _finish_plan_if_all_steps_terminal(step.plan, actor=actor)
    return step


@transaction.atomic
def apply_step(
    step: AppointmentRescheduleStep,
    *,
    actor: Any = None,
    allow_staff_override: bool = False,
    allow_room_override: bool = False,
) -> AppointmentRescheduleStep:
    step = (
        AppointmentRescheduleStep.objects.select_for_update(
            of=("self", "plan", "source_appointment")
        )
        .select_related("plan", "source_appointment", "proposed_primary_staff", "proposed_room")
        .get(pk=step.pk)
    )
    _raise_if_terminal_plan(step.plan)
    if step.action_type != AppointmentRescheduleStep.ActionType.MOVE:
        raise ValidationError("Применять можно только шаг переноса.")
    if step.status != AppointmentRescheduleStep.Status.VALID:
        raise ValidationError("Шаг переноса нужно перепроверить перед применением.")
    step = _update_step_confirmation_state(step)
    if step.confirmation_status == AppointmentRescheduleStep.ConfirmationStatus.WAITING:
        raise ValidationError("Есть отправленные согласования без ответа.")
    if step.confirmation_status == AppointmentRescheduleStep.ConfirmationStatus.DECLINED:
        raise ValidationError("Есть отказ по согласованию. Выберите другой шаг или создайте новый план.")
    if step.requires_staff_override and not allow_staff_override:
        raise ValidationError("Для шага нужен явный выход специалиста вне графика.")
    if step.requires_room_override and not allow_room_override:
        raise ValidationError("Для шага нужен отдельный override кабинета.")

    Appointment.objects.select_for_update().get(pk=step.source_appointment_id)
    local_start = timezone.localtime(step.proposed_starts_at)
    data = {
        "date": local_start.date().isoformat(),
        "time": f"{local_start:%H:%M}",
        "duration_minutes": str(
            int((step.proposed_ends_at - step.proposed_starts_at).total_seconds() // 60)
        ),
        "staff_member": str(step.proposed_primary_staff_id),
        "room": str(step.proposed_room_id or ""),
        "admin_note": step.admin_note or step.plan.reason,
    }
    if allow_staff_override:
        data["staff_availability_override"] = "1"
    if allow_room_override:
        data["room_limit_override"] = "1"
    form = AppointmentMoveForm(data, appointment=step.source_appointment, actor=actor)
    if not form.is_valid():
        step.status = AppointmentRescheduleStep.Status.FAILED
        step.validation_messages = [str(error) for error in form.errors.values()]
        step.save(update_fields=["status", "validation_messages", "updated_at"])
        raise ValidationError("Шаг переноса не применен: " + "; ".join(step.validation_messages))

    new_appointment = form.save()
    step.created_appointment = new_appointment
    step.status = AppointmentRescheduleStep.Status.APPLIED
    step.save(update_fields=["created_appointment", "status", "updated_at"])

    _skip_alternative_steps_for_source(step)
    _finish_plan_if_all_steps_terminal(step.plan, actor=actor)
    return step
