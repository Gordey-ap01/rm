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

from operations.forms import (
    AppointmentMoveForm,
    appointment_group_conflicts,
    build_local_datetime,
    conflict_messages,
    staff_unavailability_reason,
)
from operations.models import (
    Appointment,
    AppointmentConfirmation,
    AppointmentParticipant,
    AppointmentReschedulePlan,
    AppointmentRescheduleStep,
    AppointmentStaffAssignment,
    ParentGuardian,
    StaffMember,
)


@dataclass(frozen=True)
class PlanValidationResult:
    plan: AppointmentReschedulePlan
    valid_steps: int
    stale_steps: int
    pending_steps: int


@dataclass(frozen=True)
class StepConfirmationResult:
    step: AppointmentRescheduleStep
    created: list[AppointmentConfirmation] = field(default_factory=list)
    existing: list[AppointmentConfirmation] = field(default_factory=list)


ACTIVE_APPOINTMENT_STATUSES = (
    Appointment.Status.PROPOSED,
    Appointment.Status.CONFIRMED,
    Appointment.Status.RESERVED,
)

TERMINAL_STEP_STATUSES = (
    AppointmentRescheduleStep.Status.APPLIED,
    AppointmentRescheduleStep.Status.SKIPPED,
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
) -> tuple[list[str], dict[str, Any]]:
    children = _appointment_children(appointment)
    staff_members = _appointment_staff_for_move(appointment, selected_staff)
    conflicts = appointment_group_conflicts(
        starts_at,
        ends_at,
        children,
        staff_members,
        room,
        exclude_pk=appointment.pk,
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
        AppointmentRescheduleStep.objects.select_for_update()
        .select_related(
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


@transaction.atomic
def revalidate_plan(plan: AppointmentReschedulePlan) -> PlanValidationResult:
    plan = AppointmentReschedulePlan.objects.select_for_update().get(pk=plan.pk)
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
        AppointmentRescheduleStep.objects.select_for_update()
        .select_related("plan", "source_appointment", "proposed_primary_staff", "proposed_room")
        .get(pk=step.pk)
    )
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
