"""Fixed-composition program series preview and materialization."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from itertools import pairwise
from typing import Any
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from operations import schedule_writes
from operations.models import (
    Appointment,
    AppointmentParticipant,
    AppointmentSeries,
    AppointmentSeriesOccurrence,
    AppointmentSeriesParticipant,
    AppointmentSeriesRevision,
    AppointmentSeriesStaffAssignment,
    AppointmentStaffAssignment,
    BalanceAccount,
    ProgramBlock,
    Room,
    StaffMember,
    TreatmentProgram,
)
from operations.services import program_wizard, scheduling, series_revisions


@dataclass(frozen=True)
class GroupSeriesDatePreview:
    starts_at: datetime
    ends_at: datetime
    ready: bool
    reason_code: str = ""
    reason: str = ""
    room_staff_occupancy: int = 0
    room_recipient_occupancy: int = 0


@dataclass(frozen=True)
class GroupSeriesPreview:
    blocks: tuple[ProgramBlock, ...]
    staff_members: tuple[StaffMember, ...]
    room: Room
    title: str
    start_date: date
    end_date: date
    weekdays: tuple[int, ...]
    start_time: time
    duration_minutes: int
    default_appointment_status: str
    allow_unpaid_reserve: bool
    allow_outside_availability: bool
    override_reason: str
    dates: tuple[GroupSeriesDatePreview, ...]

    @property
    def ready_count(self) -> int:
        return sum(item.ready for item in self.dates)

    @property
    def skipped_count(self) -> int:
        return len(self.dates) - self.ready_count


@dataclass(frozen=True)
class GroupSeriesCreateResult:
    series: AppointmentSeries
    created_count: int
    skipped_count: int
    unchanged_count: int
    reused_series: bool = False


@dataclass(frozen=True)
class SeriesMaterializationResult:
    series: AppointmentSeries
    created_count: int
    skipped_count: int
    unchanged_count: int


@dataclass(frozen=True)
class GroupJoinCandidate:
    appointment: Appointment
    ready: bool
    reason_code: str = ""
    reason: str = ""
    recipient_count_after: int = 0
    staff_count: int = 0


@dataclass(frozen=True)
class GroupJoinPreview:
    block: ProgramBlock
    candidates: tuple[GroupJoinCandidate, ...]
    planned_remaining: int
    funded_remaining: int

    @property
    def ready_count(self) -> int:
        return sum(item.ready for item in self.candidates)


@dataclass(frozen=True)
class GroupJoinCreateResult:
    series: AppointmentSeries
    joined_count: int
    skipped_count: int
    unchanged_count: int
    reused_series: bool = False


class _SkipOccurrence(Exception):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class _BlockCapacity:
    block: ProgramBlock
    planned_remaining: int
    funded_remaining: int | None


def _operation_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _unique_in_input_order(items: Iterable[Any]) -> tuple[Any, ...]:
    result = []
    seen_pks = set()
    for item in items:
        if item.pk not in seen_pks:
            result.append(item)
            seen_pks.add(item.pk)
    return tuple(result)


def _group_series_fingerprint(preview: GroupSeriesPreview) -> str:
    return _operation_fingerprint(
        {
            "kind": AppointmentSeries.MaterializationMode.CREATE_APPOINTMENTS,
            "blocks": [block.pk for block in preview.blocks],
            "staff": [staff.pk for staff in preview.staff_members],
            "room": preview.room.pk,
            "title": preview.title,
            "start_date": preview.start_date.isoformat(),
            "end_date": preview.end_date.isoformat(),
            "weekdays": list(preview.weekdays),
            "start_time": preview.start_time.isoformat(),
            "duration_minutes": preview.duration_minutes,
            "status": preview.default_appointment_status,
            "allow_unpaid_reserve": preview.allow_unpaid_reserve,
            "allow_outside_availability": preview.allow_outside_availability,
            "override_reason": preview.override_reason,
        }
    )


def _group_join_fingerprint(
    block: ProgramBlock,
    appointments: Iterable[Appointment],
) -> str:
    snapshots = sorted(
        (
            {
                "id": int(appointment.pk),
                "starts_at": appointment.starts_at.isoformat(),
            }
            for appointment in appointments
        ),
        key=lambda item: (item["starts_at"], item["id"]),
    )
    return _operation_fingerprint(
        {
            "kind": AppointmentSeries.MaterializationMode.JOIN_EXISTING,
            "block": block.pk,
            "appointments": snapshots,
        }
    )


def _local_datetime(day: date, clock: time) -> datetime:
    return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())


def _candidate_starts(
    start_date: date,
    end_date: date,
    weekdays: Iterable[int],
    start_time: time,
) -> list[datetime]:
    selected = set(weekdays)
    values: list[datetime] = []
    cursor = start_date
    while cursor <= end_date:
        if cursor.weekday() in selected:
            values.append(_local_datetime(cursor, start_time))
        cursor += timedelta(days=1)
    return values


def _validate_composition(
    blocks: Iterable[ProgramBlock],
    staff_members: Iterable[StaffMember],
    room: Room,
) -> tuple[tuple[ProgramBlock, ...], tuple[StaffMember, ...]]:
    ordered_blocks = _unique_in_input_order(blocks)
    ordered_staff = _unique_in_input_order(staff_members)
    errors: list[str] = []
    if len(ordered_blocks) < 2:
        errors.append("Для групповой серии выберите минимум два каскада.")
    if len({block.program.child_id for block in ordered_blocks}) != len(ordered_blocks):
        errors.append("Один получатель не может быть добавлен в групповую серию дважды.")
    if len({block.service_id for block in ordered_blocks}) > 1:
        errors.append("Все каскады групповой серии должны относиться к одной услуге.")
    terminal_statuses = {ProgramBlock.Status.COMPLETED, ProgramBlock.Status.CANCELLED}
    if any(block.status in terminal_statuses for block in ordered_blocks):
        errors.append("Завершенный или отмененный каскад нельзя включить в новую серию.")
    if any(block.program.status != TreatmentProgram.Status.ACTIVE for block in ordered_blocks):
        errors.append("Групповую серию можно создавать только для активных программ.")
    if not ordered_staff:
        errors.append("Выберите хотя бы одного специалиста.")
    if any(staff.status != StaffMember.Status.ACTIVE for staff in ordered_staff):
        errors.append("В серию можно назначать только активных специалистов.")
    if not room.is_active:
        errors.append("Выбранный кабинет неактивен.")
    if not room.allow_group_sessions:
        errors.append("Выбранный кабинет не разрешает групповые занятия.")
    if errors:
        raise ValidationError(errors)
    return ordered_blocks, ordered_staff


def _block_capacities(
    blocks: Iterable[ProgramBlock],
    *,
    allow_unpaid_reserve: bool,
) -> tuple[_BlockCapacity, ...]:
    return tuple(
        _BlockCapacity(
            block=block,
            planned_remaining=max(block.planned_sessions - block.scheduled_count, 0),
            funded_remaining=(
                None
                if allow_unpaid_reserve
                else program_wizard.funded_sessions_remaining(block)
            ),
        )
        for block in blocks
    )


def _capacity_limit_reason(
    capacities: Iterable[_BlockCapacity],
    scheduled_in_preview: int,
    *,
    on_date: date,
) -> tuple[str, str] | None:
    for capacity in capacities:
        block = capacity.block
        program_reason = program_wizard.program_availability_reason(
            block,
            on_date=on_date,
        )
        if program_reason:
            return (
                "program_unavailable",
                f"Каскад «{block.title}» недоступен: {program_reason}.",
            )
        if scheduled_in_preview >= capacity.planned_remaining:
            return (
                "plan_limit",
                f"План каскада «{block.title}» исчерпан.",
            )
        if capacity.funded_remaining is not None:
            availability_reason = program_wizard.account_availability_reason(
                block.balance_account,
                block.service,
                on_date=on_date,
            )
            if availability_reason:
                return (
                    "funding_unavailable",
                    f"Оплата каскада «{block.title}» недоступна: {availability_reason}.",
                )
        if (
            capacity.funded_remaining is not None
            and scheduled_in_preview >= capacity.funded_remaining
        ):
            return (
                "funding_limit",
                f"Доступная оплата каскада «{block.title}» исчерпана.",
            )
    return None


def _block_limit_reason(
    blocks: Iterable[ProgramBlock],
    scheduled_in_preview: int,
    *,
    allow_unpaid_reserve: bool,
    on_date: date,
) -> tuple[str, str] | None:
    return _capacity_limit_reason(
        _block_capacities(blocks, allow_unpaid_reserve=allow_unpaid_reserve),
        scheduled_in_preview,
        on_date=on_date,
    )


def _slot_conflict(
    *,
    starts_at: datetime,
    ends_at: datetime,
    blocks: Iterable[ProgramBlock],
    staff_members: Iterable[StaffMember],
    room: Room,
    allow_outside_availability: bool,
) -> tuple[str, str, int, int]:
    children = [block.program.child for block in blocks]
    report = scheduling.find_overlaps(
        starts_at,
        ends_at,
        children=children,
        staff_members=staff_members,
        room=room,
    )
    if report.has_conflict:
        reasons = ", ".join(report.human_messages()) or "конфликт расписания"
        code = "capacity" if report.room_over_limit else "schedule_conflict"
        return code, reasons, report.room_staff_occupancy, report.room_recipient_occupancy

    unavailable = [
        f"{staff}: {reason}"
        for staff in staff_members
        if (reason := scheduling.is_within_availability(staff, starts_at, ends_at))
    ]
    if unavailable and not allow_outside_availability:
        return (
            "staff_unavailable",
            "; ".join(unavailable),
            report.room_staff_occupancy,
            report.room_recipient_occupancy,
        )
    return "", "", report.room_staff_occupancy, report.room_recipient_occupancy


def preview_group_series(
    *,
    blocks: Iterable[ProgramBlock],
    staff_members: Iterable[StaffMember],
    room: Room,
    title: str,
    start_date: date,
    end_date: date,
    weekdays: Iterable[int],
    start_time: time,
    duration_minutes: int,
    default_appointment_status: str = Appointment.Status.PROPOSED,
    allow_unpaid_reserve: bool = False,
    allow_outside_availability: bool = False,
    override_reason: str = "",
) -> GroupSeriesPreview:
    ordered_blocks, ordered_staff = _validate_composition(blocks, staff_members, room)
    selected_weekdays = tuple(sorted(set(weekdays)))
    errors: list[str] = []
    if not title.strip():
        errors.append("Укажите название серии.")
    if len(title.strip()) > 200:
        errors.append("Название серии не может быть длиннее 200 символов.")
    if start_date < timezone.localdate():
        errors.append("Серия не может начинаться в прошлом.")
    if end_date < start_date:
        errors.append("Дата окончания не может быть раньше даты начала.")
    if end_date - start_date > timedelta(days=366):
        errors.append("Одна серия может охватывать не более 366 дней.")
    for block in ordered_blocks:
        program = block.program
        if program.starts_on and start_date < program.starts_on:
            errors.append(
                f"Серия начинается раньше программы «{program.title}» ({program.starts_on:%d.%m.%Y})."
            )
        if program.ends_on and end_date > program.ends_on:
            errors.append(
                f"Серия заканчивается позже программы «{program.title}» ({program.ends_on:%d.%m.%Y})."
            )
    if not selected_weekdays:
        errors.append("Выберите хотя бы один день недели.")
    if any(value not in range(7) for value in selected_weekdays):
        errors.append("День недели должен быть числом от 0 до 6.")
    if duration_minutes < 15 or duration_minutes > 240:
        errors.append("Длительность должна быть от 15 до 240 минут.")
    allowed_statuses = {
        Appointment.Status.PROPOSED,
        Appointment.Status.RESERVED,
        Appointment.Status.CONFIRMED,
    }
    if default_appointment_status not in allowed_statuses:
        errors.append("Для серии выбран недопустимый статус занятия.")
    if allow_unpaid_reserve and default_appointment_status != Appointment.Status.RESERVED:
        errors.append("Занятия сверх оплаты должны создаваться со статусом «Бронь».")
    if (allow_unpaid_reserve or allow_outside_availability) and not override_reason.strip():
        errors.append("Для исключения укажите основание.")
    if errors:
        raise ValidationError(errors)

    date_previews: list[GroupSeriesDatePreview] = []
    scheduled_in_preview = 0
    duration = timedelta(minutes=duration_minutes)
    capacities = _block_capacities(
        ordered_blocks,
        allow_unpaid_reserve=allow_unpaid_reserve,
    )
    for starts_at in _candidate_starts(start_date, end_date, selected_weekdays, start_time):
        ends_at = starts_at + duration
        limit_reason = _capacity_limit_reason(
            capacities,
            scheduled_in_preview,
            on_date=timezone.localtime(starts_at).date(),
        )
        if limit_reason:
            date_previews.append(
                GroupSeriesDatePreview(
                    starts_at=starts_at,
                    ends_at=ends_at,
                    ready=False,
                    reason_code=limit_reason[0],
                    reason=limit_reason[1],
                )
            )
            continue
        conflict = _slot_conflict(
            starts_at=starts_at,
            ends_at=ends_at,
            blocks=ordered_blocks,
            staff_members=ordered_staff,
            room=room,
            allow_outside_availability=allow_outside_availability,
        )
        if conflict[0]:
            date_previews.append(
                GroupSeriesDatePreview(
                    starts_at=starts_at,
                    ends_at=ends_at,
                    ready=False,
                    reason_code=conflict[0],
                    reason=conflict[1],
                    room_staff_occupancy=conflict[2],
                    room_recipient_occupancy=conflict[3],
                )
            )
            continue
        date_previews.append(
            GroupSeriesDatePreview(
                starts_at=starts_at,
                ends_at=ends_at,
                ready=True,
                room_staff_occupancy=conflict[2],
                room_recipient_occupancy=conflict[3],
            )
        )
        scheduled_in_preview += 1

    if not date_previews:
        raise ValidationError("В выбранном периоде нет дат для указанных дней недели.")
    return GroupSeriesPreview(
        blocks=ordered_blocks,
        staff_members=ordered_staff,
        room=room,
        title=title.strip(),
        start_date=start_date,
        end_date=end_date,
        weekdays=selected_weekdays,
        start_time=start_time,
        duration_minutes=duration_minutes,
        default_appointment_status=default_appointment_status,
        allow_unpaid_reserve=allow_unpaid_reserve,
        allow_outside_availability=allow_outside_availability,
        override_reason=override_reason.strip(),
        dates=tuple(date_previews),
    )


def _days_of_week_value(weekdays: Iterable[int]) -> str:
    labels = {value: label for label, value in AppointmentSeries.DAY_MAP.items()}
    return ",".join(labels[value] for value in sorted(set(weekdays)))


@transaction.atomic
def _create_series_definition(
    preview: GroupSeriesPreview,
    *,
    operation_key: UUID,
) -> tuple[AppointmentSeries, bool]:
    fingerprint = _group_series_fingerprint(preview)
    existing = AppointmentSeries.objects.filter(operation_key=operation_key).first()
    if existing:
        if (
            existing.materialization_mode
            != AppointmentSeries.MaterializationMode.CREATE_APPOINTMENTS
            or (
                existing.operation_fingerprint
                and existing.operation_fingerprint != fingerprint
            )
        ):
            raise ValidationError(
                "Ключ операции уже использован для другого состава серии."
            )
        return existing, True

    primary_block = preview.blocks[0]
    primary_staff = preview.staff_members[0]
    series = AppointmentSeries(
        operation_key=operation_key,
        child=primary_block.program.child,
        service=primary_block.service,
        staff_member=primary_staff,
        room=preview.room,
        program_block=primary_block,
        title=preview.title,
        start_date=preview.start_date,
        end_date=preview.end_date,
        days_of_week=_days_of_week_value(preview.weekdays),
        time=preview.start_time,
        duration_minutes=preview.duration_minutes,
        session_type=Appointment.SessionType.GROUP,
        materialization_mode=AppointmentSeries.MaterializationMode.CREATE_APPOINTMENTS,
        operation_fingerprint=fingerprint,
        default_appointment_status=preview.default_appointment_status,
        allow_unpaid_reserve=preview.allow_unpaid_reserve,
        allow_outside_availability=preview.allow_outside_availability,
        override_reason=preview.override_reason,
        status=AppointmentSeries.Status.ACTIVE,
    )
    series.full_clean()
    series.save()
    for position, block in enumerate(preview.blocks, start=1):
        AppointmentSeriesParticipant.objects.create(
            series=series,
            child=block.program.child,
            program_block=block,
            billing_account=block.balance_account,
            position=position,
        )
    for position, staff in enumerate(preview.staff_members, start=1):
        AppointmentSeriesStaffAssignment.objects.create(
            series=series,
            staff_member=staff,
            role=(
                AppointmentSeriesStaffAssignment.Role.PRIMARY
                if position == 1
                else AppointmentSeriesStaffAssignment.Role.ASSISTANT
            ),
            override_availability=preview.allow_outside_availability,
            override_reason=preview.override_reason if preview.allow_outside_availability else "",
        )
    return series, False


def _series_composition(
    series: AppointmentSeries,
) -> tuple[list[AppointmentSeriesParticipant], list[AppointmentSeriesStaffAssignment]]:
    participants = list(
        series.default_participants.select_related(
            "child",
            "program_block",
            "program_block__program",
            "program_block__service",
            "billing_account",
        ).order_by("position", "pk")
    )
    assignments = list(
        series.default_staff_assignments.select_related("staff_member").order_by("pk")
    )
    if len(participants) < 2 or not assignments:
        raise ValidationError("Состав групповой серии неполон.")
    if any(not participant.program_block_id for participant in participants):
        raise ValidationError("Для каждого участника групповой серии нужен каскад.")
    return participants, assignments


def _record_skipped(
    series: AppointmentSeries,
    starts_at: datetime,
    *,
    code: str,
    reason: str,
    actor: Any,
) -> tuple[AppointmentSeriesOccurrence, bool]:
    occurrence, created = AppointmentSeriesOccurrence.objects.get_or_create(
        series=series,
        scheduled_starts_at=starts_at,
        defaults={
            "outcome": AppointmentSeriesOccurrence.Outcome.SKIPPED,
            "reason_code": code,
            "reason": reason,
            "created_by": actor if getattr(actor, "pk", None) else None,
        },
    )
    return occurrence, created


@transaction.atomic
def _ensure_individual_revision(
    series: AppointmentSeries,
    *,
    actor: Any,
) -> AppointmentSeriesRevision:
    locked = AppointmentSeries.objects.select_for_update().get(pk=series.pk)
    if locked.session_type != Appointment.SessionType.INDIVIDUAL:
        raise ValidationError("Индивидуальный materializer не обслуживает групповые серии.")
    if (
        locked.materialization_mode
        != AppointmentSeries.MaterializationMode.CREATE_APPOINTMENTS
    ):
        raise ValidationError("Серия присоединения не создает новые занятия.")

    participants = list(locked.default_participants.order_by("position", "pk"))
    assignments = list(locked.default_staff_assignments.order_by("pk"))
    if locked.current_revision_id:
        if len(participants) != 1 or len(assignments) != 1:
            raise series_revisions.SeriesRevisionMismatch(
                "Нормализованный состав индивидуальной серии поврежден."
            )
    else:
        if not participants:
            billing_account_id = None
            if locked.program_block_id:
                program_block = ProgramBlock.objects.select_for_update().get(
                    pk=locked.program_block_id
                )
                billing_account_id = program_block.balance_account_id
                if billing_account_id:
                    BalanceAccount.all_objects.select_for_update().get(
                        pk=billing_account_id
                    )
            participants = [
                AppointmentSeriesParticipant.objects.create(
                    series=locked,
                    child=locked.child,
                    program_block=locked.program_block,
                    billing_account_id=billing_account_id,
                    position=1,
                )
            ]
        if not assignments:
            assignments = [
                AppointmentSeriesStaffAssignment.objects.create(
                    series=locked,
                    staff_member=locked.staff_member,
                    role=AppointmentSeriesStaffAssignment.Role.PRIMARY,
                )
            ]
    participant = participants[0]
    assignment = assignments[0]
    if (
        len(participants) != 1
        or participant.child_id != locked.child_id
        or participant.program_block_id != locked.program_block_id
        or len(assignments) != 1
        or assignment.staff_member_id != locked.staff_member_id
        or assignment.role != AppointmentSeriesStaffAssignment.Role.PRIMARY
    ):
        raise ValidationError(
            "Legacy-проекция индивидуальной серии не совпадает с нормализованным составом."
        )
    revision, _ = series_revisions.ensure_initial_revision(locked, actor=actor)
    return revision


def _materialize_individual_date(
    series: AppointmentSeries,
    revision: AppointmentSeriesRevision,
    starts_at: datetime,
    *,
    actor: Any,
) -> tuple[AppointmentSeriesOccurrence, bool]:
    try:
        with transaction.atomic():
            locked_series = AppointmentSeries.objects.select_for_update().get(pk=series.pk)
            series_revisions.assert_current_projection(locked_series, revision)
            existing = AppointmentSeriesOccurrence.objects.filter(
                series=locked_series,
                scheduled_starts_at=starts_at,
            ).first()
            if existing:
                return existing, False

            participants = list(
                revision.participants.select_related(
                    "child",
                    "program_block__program",
                    "program_block__balance_account",
                    "billing_account",
                )
            )
            assignments = list(
                revision.staff_assignments.select_related("staff_member")
            )
            if len(participants) != 1 or len(assignments) != 1:
                raise series_revisions.SeriesRevisionMismatch(
                    "Снимок индивидуальной редакции поврежден."
                )
            participant = participants[0]
            assignment = assignments[0]
            room_id = revision.room_id
            with schedule_writes.lock_schedule_write(room_ids=[room_id]) as locked:
                room = locked.room_for(room_id)
                block = None
                if participant.program_block_id:
                    block = (
                        ProgramBlock.objects.select_for_update(of=("self",))
                        .select_related("program__child", "service", "balance_account")
                        .get(pk=participant.program_block_id)
                    )
                    block.program = TreatmentProgram.objects.select_for_update().get(
                        pk=block.program_id
                    )
                    if (
                        block.program.child_id != participant.child_id
                        or block.service_id != revision.service_id
                    ):
                        raise _SkipOccurrence(
                            "series_composition",
                            "Каскад больше не соответствует снимку редакции серии.",
                        )
                    if block.balance_account_id != participant.billing_account_id:
                        raise _SkipOccurrence(
                            "funding_changed",
                            "Счет каскада изменился после фиксации редакции серии.",
                        )
                    if participant.billing_account_id:
                        block.balance_account = (
                            BalanceAccount.all_objects.select_for_update().get(
                                pk=participant.billing_account_id
                            )
                        )
                    limit_reason = _block_limit_reason(
                        [block],
                        0,
                        allow_unpaid_reserve=revision.allow_unpaid_reserve,
                        on_date=timezone.localtime(starts_at).date(),
                    )
                    if limit_reason:
                        raise _SkipOccurrence(*limit_reason)

                ends_at = starts_at + timedelta(minutes=revision.duration_minutes)
                report = scheduling.find_overlaps(
                    starts_at,
                    ends_at,
                    children=[participant.child],
                    staff_members=[assignment.staff_member],
                    room=room,
                )
                if report.has_conflict:
                    reason = ", ".join(report.human_messages()) or "конфликт расписания"
                    code = "capacity" if report.room_over_limit else "schedule_conflict"
                    raise _SkipOccurrence(code, reason)
                availability_reason = scheduling.is_within_availability(
                    assignment.staff_member,
                    starts_at,
                    ends_at,
                )
                if availability_reason and not assignment.override_availability:
                    raise _SkipOccurrence(
                        "staff_unavailable",
                        f"{assignment.staff_member}: {availability_reason}",
                    )
                schedule_writes.ensure_room_capacity(
                    starts_at=starts_at,
                    ends_at=ends_at,
                    children=[participant.child],
                    staff_members=[assignment.staff_member],
                    room=room,
                    status=revision.default_appointment_status,
                )
                appointment = Appointment.objects.create(
                    child=participant.child,
                    staff_member=assignment.staff_member,
                    service=revision.service,
                    room=room,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    session_type=Appointment.SessionType.INDIVIDUAL,
                    title=revision.title,
                    status=revision.default_appointment_status,
                    billing_account=participant.billing_account,
                    billing_decision=Appointment.BillingDecision.UNDECIDED,
                    series=locked_series,
                    program_block=block,
                    staff_availability_override=assignment.override_availability,
                    staff_availability_override_reason=(
                        assignment.override_reason
                        if assignment.override_availability
                        else ""
                    ),
                    admin_note="Создано единым materializer серии.",
                )
                if block and block.status == ProgramBlock.Status.PLANNED:
                    ProgramBlock.objects.filter(pk=block.pk).update(
                        status=ProgramBlock.Status.SCHEDULED,
                        updated_at=timezone.now(),
                    )
                return (
                    AppointmentSeriesOccurrence.objects.create(
                        series=locked_series,
                        scheduled_starts_at=starts_at,
                        appointment=appointment,
                        outcome=AppointmentSeriesOccurrence.Outcome.CREATED,
                        created_by=actor,
                    ),
                    True,
                )
    except _SkipOccurrence as exc:
        return _record_skipped(
            series,
            starts_at,
            code=exc.code,
            reason=exc.reason,
            actor=actor,
        )
    except series_revisions.SeriesRevisionMismatch:
        raise
    except (ValidationError, IntegrityError) as exc:
        return _record_skipped(
            series,
            starts_at,
            code="stale_conflict",
            reason=(
                "Расписание изменилось во время создания: " + "; ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else "Расписание изменилось во время создания; дата безопасно пропущена."
            ),
            actor=actor,
        )


def materialize_individual_series(
    series: AppointmentSeries,
    *,
    actor: Any,
) -> SeriesMaterializationResult:
    series_revisions.require_operator_role(actor)
    if series.status != AppointmentSeries.Status.ACTIVE:
        raise ValidationError("Создавать занятия можно только для активной серии.")
    revision = _ensure_individual_revision(series, actor=actor)
    starts = _candidate_starts(
        revision.start_date,
        revision.end_date,
        (
            AppointmentSeries.DAY_MAP[token.strip().upper()]
            for token in revision.days_of_week.split(",")
        ),
        revision.time,
    )
    run, _ = series_revisions.get_or_create_initial_run(
        series,
        revision,
        operation_key=series.operation_key,
        actor=actor,
        expected_result_count=len(starts),
    )
    created_count = 0
    skipped_count = 0
    unchanged_count = 0
    for starts_at in starts:
        with transaction.atomic():
            occurrence, created = _materialize_individual_date(
                series,
                revision,
                starts_at,
                actor=actor,
            )
            series_revisions.record_compatibility_result(run, occurrence)
        if not created:
            unchanged_count += 1
        elif occurrence.outcome == AppointmentSeriesOccurrence.Outcome.CREATED:
            created_count += 1
        else:
            skipped_count += 1
    series_revisions.complete_run(run)
    return SeriesMaterializationResult(
        series=series,
        created_count=created_count,
        skipped_count=skipped_count,
        unchanged_count=unchanged_count,
    )


def _materialize_one_date(
    series: AppointmentSeries,
    revision: AppointmentSeriesRevision,
    starts_at: datetime,
    *,
    actor: Any,
) -> tuple[AppointmentSeriesOccurrence, bool]:
    try:
        with transaction.atomic():
            locked_series = AppointmentSeries.objects.select_for_update().get(pk=series.pk)
            series_revisions.assert_current_projection(locked_series, revision)
            existing = AppointmentSeriesOccurrence.objects.filter(
                series=locked_series,
                scheduled_starts_at=starts_at,
            ).first()
            if existing:
                return existing, False

            participants = list(
                revision.participants.select_related(
                    "child",
                    "program_block__program",
                    "billing_account",
                ).order_by("position", "pk")
            )
            assignments = list(
                revision.staff_assignments.select_related("staff_member").order_by(
                    "pk"
                )
            )
            primary_assignments = [
                assignment
                for assignment in assignments
                if assignment.role == AppointmentSeriesStaffAssignment.Role.PRIMARY
            ]
            if (
                len(participants) < 2
                or any(not participant.program_block_id for participant in participants)
                or len(primary_assignments) != 1
            ):
                raise _SkipOccurrence(
                    "series_composition",
                    "Снимок состава групповой редакции неполон.",
                )
            room_id = revision.room_id
            with schedule_writes.lock_schedule_write(room_ids=[room_id]) as locked:
                room = locked.room_for(room_id)
                block_ids = sorted(
                    participant.program_block_id for participant in participants
                    if participant.program_block_id
                )
                locked_blocks = list(
                    ProgramBlock.objects.select_for_update(of=("self",))
                    .select_related("program__child", "service", "balance_account")
                    .filter(pk__in=block_ids)
                    .order_by("pk")
                )
                blocks_by_id = {block.pk: block for block in locked_blocks}
                blocks = [blocks_by_id[participant.program_block_id] for participant in participants]
                program_ids = sorted({block.program_id for block in blocks})
                locked_programs = {
                    program.pk: program
                    for program in TreatmentProgram.objects.select_for_update()
                    .filter(pk__in=program_ids)
                    .order_by("pk")
                }
                for block in blocks:
                    block.program = locked_programs[block.program_id]
                for participant, block in zip(participants, blocks, strict=True):
                    if (
                        block.program.child_id != participant.child_id
                        or block.service_id != revision.service_id
                    ):
                        raise _SkipOccurrence(
                            "series_composition",
                            "Каскад больше не соответствует снимку групповой редакции.",
                        )
                    if block.balance_account_id != participant.billing_account_id:
                        raise _SkipOccurrence(
                            "funding_changed",
                            "Счет каскада изменился после фиксации групповой редакции.",
                        )
                if not revision.allow_unpaid_reserve:
                    account_ids = sorted(
                        {block.balance_account_id for block in blocks if block.balance_account_id}
                    )
                    locked_accounts = {
                        account.pk: account
                        for account in BalanceAccount.all_objects.select_for_update()
                        .filter(pk__in=account_ids)
                        .order_by("pk")
                    }
                    for block in blocks:
                        if block.balance_account_id:
                            block.balance_account = locked_accounts[block.balance_account_id]
                limit_reason = _block_limit_reason(
                    blocks,
                    0,
                    allow_unpaid_reserve=revision.allow_unpaid_reserve,
                    on_date=timezone.localtime(starts_at).date(),
                )
                if limit_reason:
                    raise _SkipOccurrence(*limit_reason)

                staff_members = [assignment.staff_member for assignment in assignments]
                ends_at = starts_at + timedelta(minutes=revision.duration_minutes)
                conflict = _slot_conflict(
                    starts_at=starts_at,
                    ends_at=ends_at,
                    blocks=blocks,
                    staff_members=staff_members,
                    room=room,
                    allow_outside_availability=revision.allow_outside_availability,
                )
                if conflict[0]:
                    raise _SkipOccurrence(conflict[0], conflict[1])
                schedule_writes.ensure_room_capacity(
                    starts_at=starts_at,
                    ends_at=ends_at,
                    children=[participant.child for participant in participants],
                    staff_members=staff_members,
                    room=room,
                    status=revision.default_appointment_status,
                )

                primary = participants[0]
                primary_staff = primary_assignments[0]
                appointment = Appointment.objects.create(
                    child=primary.child,
                    staff_member=primary_staff.staff_member,
                    service=revision.service,
                    room=room,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    session_type=Appointment.SessionType.GROUP,
                    title=revision.title,
                    status=revision.default_appointment_status,
                    billing_account=primary.billing_account,
                    billing_decision=Appointment.BillingDecision.UNDECIDED,
                    series=locked_series,
                    program_block=primary.program_block,
                    staff_availability_override=revision.allow_outside_availability,
                    staff_availability_override_reason=(
                        revision.override_reason
                        if revision.allow_outside_availability
                        else ""
                    ),
                    admin_note="Создано из групповой серии программы.",
                )
                for membership in participants[1:]:
                    AppointmentParticipant.objects.create(
                        appointment=appointment,
                        child=membership.child,
                        billing_account=membership.billing_account,
                        billing_decision=Appointment.BillingDecision.UNDECIDED,
                        program_block=membership.program_block,
                        starts_at_snapshot=starts_at,
                        ends_at_snapshot=ends_at,
                        appointment_status=appointment.status,
                    )
                for assignment in assignments:
                    if assignment.pk == primary_staff.pk:
                        continue
                    AppointmentStaffAssignment.objects.create(
                        appointment=appointment,
                        staff_member=assignment.staff_member,
                        role=assignment.role,
                        starts_at_snapshot=starts_at,
                        ends_at_snapshot=ends_at,
                        appointment_status=appointment.status,
                        override_availability=assignment.override_availability,
                        override_reason=assignment.override_reason,
                    )
                for block in blocks:
                    if block.status == ProgramBlock.Status.PLANNED:
                        ProgramBlock.objects.filter(pk=block.pk).update(
                            status=ProgramBlock.Status.SCHEDULED,
                            updated_at=timezone.now(),
                        )
                return (
                    AppointmentSeriesOccurrence.objects.create(
                        series=locked_series,
                        scheduled_starts_at=starts_at,
                        appointment=appointment,
                        outcome=AppointmentSeriesOccurrence.Outcome.CREATED,
                        created_by=actor if getattr(actor, "pk", None) else None,
                    ),
                    True,
                )
    except _SkipOccurrence as exc:
        return _record_skipped(
            series,
            starts_at,
            code=exc.code,
            reason=exc.reason,
            actor=actor,
        )
    except series_revisions.SeriesRevisionMismatch:
        raise
    except (ValidationError, IntegrityError) as exc:
        return _record_skipped(
            series,
            starts_at,
            code="stale_conflict",
            reason="Расписание изменилось во время создания: " + "; ".join(exc.messages)
            if isinstance(exc, ValidationError)
            else "Расписание изменилось во время создания; дата безопасно пропущена.",
            actor=actor,
        )


def materialize_group_series(
    series: AppointmentSeries,
    *,
    actor: Any = None,
) -> GroupSeriesCreateResult:
    series_revisions.require_operator_role(actor)
    if series.status != AppointmentSeries.Status.ACTIVE:
        raise ValidationError("Создавать занятия можно только для активной серии.")
    if series.session_type != Appointment.SessionType.GROUP:
        raise ValidationError("Этот сервис предназначен для групповой серии.")
    if (
        series.materialization_mode
        != AppointmentSeries.MaterializationMode.CREATE_APPOINTMENTS
    ):
        raise ValidationError("Эта серия присоединяет к существующим занятиям.")

    created_count = 0
    skipped_count = 0
    unchanged_count = 0
    revision, _ = series_revisions.ensure_initial_revision(series, actor=actor)
    starts = _candidate_starts(
        revision.start_date,
        revision.end_date,
        (
            AppointmentSeries.DAY_MAP[token.strip().upper()]
            for token in revision.days_of_week.split(",")
        ),
        revision.time,
    )
    run, _ = series_revisions.get_or_create_initial_run(
        series,
        revision,
        operation_key=series.operation_key,
        actor=actor,
        expected_result_count=len(starts),
    )
    for starts_at in starts:
        with transaction.atomic():
            occurrence, created = _materialize_one_date(
                series,
                revision,
                starts_at,
                actor=actor,
            )
            series_revisions.record_compatibility_result(run, occurrence)
        if not created:
            unchanged_count += 1
        elif occurrence.outcome == AppointmentSeriesOccurrence.Outcome.CREATED:
            created_count += 1
        else:
            skipped_count += 1
    series_revisions.complete_run(run)
    return GroupSeriesCreateResult(
        series=series,
        created_count=created_count,
        skipped_count=skipped_count,
        unchanged_count=unchanged_count,
    )


def create_group_series(
    preview: GroupSeriesPreview,
    *,
    operation_key: UUID,
    actor: Any = None,
) -> GroupSeriesCreateResult:
    series_revisions.require_operator_role(actor)
    try:
        series, reused = _create_series_definition(preview, operation_key=operation_key)
    except IntegrityError as exc:
        series = AppointmentSeries.objects.filter(operation_key=operation_key).first()
        if series is None:
            raise
        fingerprint = _group_series_fingerprint(preview)
        if (
            series.materialization_mode
            != AppointmentSeries.MaterializationMode.CREATE_APPOINTMENTS
            or (
                series.operation_fingerprint
                and series.operation_fingerprint != fingerprint
            )
        ):
            raise ValidationError(
                "Ключ операции уже использован для другого состава серии."
            ) from exc
        reused = True
    result = materialize_group_series(series, actor=actor)
    return GroupSeriesCreateResult(
        series=series,
        created_count=result.created_count,
        skipped_count=result.skipped_count,
        unchanged_count=result.unchanged_count,
        reused_series=reused,
    )


_JOINABLE_GROUP_STATUSES = {
    Appointment.Status.PROPOSED,
    Appointment.Status.CONFIRMED,
    Appointment.Status.RESERVED,
}
_MAX_JOIN_CANDIDATES = 500
_MAX_JOIN_SELECTION = 200


def _appointment_children(appointment: Appointment) -> list[Any]:
    prefetched = getattr(appointment, "_prefetched_objects_cache", {}).get(
        "participants"
    )
    participants = (
        list(prefetched)
        if prefetched is not None
        else list(appointment.participants.select_related("child").order_by("pk"))
    )
    if participants:
        return [participant.child for participant in participants]
    return [appointment.child]


def _appointment_staff_members(appointment: Appointment) -> list[StaffMember]:
    prefetched = getattr(appointment, "_prefetched_objects_cache", {}).get(
        "staff_assignments"
    )
    assignments = (
        list(prefetched)
        if prefetched is not None
        else list(
            appointment.staff_assignments.select_related("staff_member").order_by("pk")
        )
    )
    if assignments:
        return [assignment.staff_member for assignment in assignments]
    return [appointment.staff_member]


def _group_join_candidate(
    block: ProgramBlock,
    appointment: Appointment,
    *,
    planned_remaining: int,
    funded_remaining: int,
) -> GroupJoinCandidate:
    child = block.program.child
    existing_children = _appointment_children(appointment)
    existing_staff = _appointment_staff_members(appointment)
    recipient_count_after = len({item.pk for item in [*existing_children, child]})
    staff_count = len({item.pk for item in existing_staff})

    def blocked(code: str, reason: str) -> GroupJoinCandidate:
        return GroupJoinCandidate(
            appointment=appointment,
            ready=False,
            reason_code=code,
            reason=reason,
            recipient_count_after=recipient_count_after,
            staff_count=staff_count,
        )

    if appointment.session_type != Appointment.SessionType.GROUP:
        return blocked("not_group", "Занятие больше не является групповым.")
    if appointment.status not in _JOINABLE_GROUP_STATUSES:
        return blocked("status", "Статус занятия не допускает присоединение.")
    if appointment.service_id != block.service_id:
        return blocked("service", "Услуга занятия не соответствует каскаду.")
    if appointment.starts_at <= timezone.now():
        return blocked("not_future", "Присоединять можно только к будущему занятию.")
    if appointment.room_id is None:
        return blocked("room_missing", "У группового занятия не выбран кабинет.")
    if not appointment.room.is_active:
        return blocked("room_inactive", "Кабинет занятия неактивен.")
    if any(existing.pk == child.pk for existing in existing_children):
        return blocked("already_participant", "Получатель уже участвует в занятии.")

    on_date = timezone.localtime(appointment.starts_at).date()
    program_reason = program_wizard.program_availability_reason(block, on_date=on_date)
    if program_reason:
        return blocked("program_unavailable", f"Программа недоступна: {program_reason}.")
    account_reason = program_wizard.account_availability_reason(
        block.balance_account,
        block.service,
        on_date=on_date,
    )
    if account_reason:
        return blocked("funding_unavailable", f"Оплата недоступна: {account_reason}.")
    if planned_remaining <= 0:
        return blocked("plan_limit", "План каскада исчерпан.")
    if funded_remaining <= 0:
        return blocked("funding_limit", "Доступная оплата каскада исчерпана.")

    child_report = scheduling.find_overlaps(
        appointment.starts_at,
        appointment.ends_at,
        children=[child],
        staff_members=[],
        exclude_pk=appointment.pk,
    )
    if child_report.child_conflict:
        local_start = timezone.localtime(child_report.child_conflict.starts_at)
        return blocked(
            "recipient_conflict",
            f"У получателя уже есть занятие {local_start:%d.%m.%Y %H:%M}.",
        )

    room_report = scheduling.find_overlaps(
        appointment.starts_at,
        appointment.ends_at,
        children=[*existing_children, child],
        staff_members=existing_staff,
        room=appointment.room,
        exclude_pk=appointment.pk,
    )
    room_reasons = room_report.room_limit_reasons or {}
    recipient_count_after = int(
        room_reasons.get("recipient_total", recipient_count_after)
    )
    staff_count = int(room_reasons.get("staff_total", staff_count))
    if room_report.room_over_limit:
        return GroupJoinCandidate(
            appointment=appointment,
            ready=False,
            reason_code="capacity",
            reason=(
                "Ограничение кабинета: "
                + schedule_writes.room_limit_message(
                    appointment.room,
                    {"room_limit_reasons": room_reasons},
                )
                + "."
            ),
            recipient_count_after=recipient_count_after,
            staff_count=staff_count,
        )
    return GroupJoinCandidate(
        appointment=appointment,
        ready=True,
        recipient_count_after=recipient_count_after,
        staff_count=staff_count,
    )


def preview_group_joins(
    *,
    block: ProgramBlock,
    date_from: date,
    date_to: date,
    appointments: Iterable[Appointment] | None = None,
) -> GroupJoinPreview:
    errors: list[str] = []
    today = timezone.localdate()
    if date_from < today:
        errors.append("Период поиска не может начинаться в прошлом.")
    if date_to < date_from:
        errors.append("Дата окончания не может быть раньше даты начала.")
    if date_to - date_from > timedelta(days=366):
        errors.append("Период поиска не может превышать 366 дней.")
    if block.status in {ProgramBlock.Status.COMPLETED, ProgramBlock.Status.CANCELLED}:
        errors.append("Завершенный или отмененный каскад нельзя присоединять к группам.")
    if errors:
        raise ValidationError(errors)

    if appointments is None:
        queryset = (
            Appointment.objects.select_related(
                "child",
                "service",
                "staff_member",
                "room",
            )
            .prefetch_related(
                "participants__child",
                "staff_assignments__staff_member",
            )
            .filter(
                service=block.service,
                session_type=Appointment.SessionType.GROUP,
                status__in=_JOINABLE_GROUP_STATUSES,
                starts_at__gt=timezone.now(),
                starts_at__date__gte=date_from,
                starts_at__date__lte=date_to,
            )
            .order_by("starts_at", "pk")
        )
        candidate_appointments = list(queryset[: _MAX_JOIN_CANDIDATES + 1])
    else:
        candidate_appointments = sorted(
            _unique_in_input_order(appointments),
            key=lambda item: (item.starts_at, item.pk),
        )
    if len(candidate_appointments) > _MAX_JOIN_CANDIDATES:
        raise ValidationError(
            "Найдено слишком много групповых занятий. Сократите период поиска."
        )

    planned_remaining = max(block.planned_sessions - block.scheduled_count, 0)
    funded_remaining = program_wizard.funded_sessions_remaining(block)
    candidates = tuple(
        _group_join_candidate(
            block,
            appointment,
            planned_remaining=planned_remaining,
            funded_remaining=funded_remaining,
        )
        for appointment in candidate_appointments
    )
    return GroupJoinPreview(
        block=block,
        candidates=candidates,
        planned_remaining=planned_remaining,
        funded_remaining=funded_remaining,
    )


def _validate_join_selection(appointments: Iterable[Appointment]) -> tuple[Appointment, ...]:
    ordered = tuple(
        sorted(
            _unique_in_input_order(appointments),
            key=lambda item: (item.starts_at, item.pk),
        )
    )
    if not ordered:
        raise ValidationError("Выберите хотя бы одно групповое занятие.")
    if len(ordered) > _MAX_JOIN_SELECTION:
        raise ValidationError(
            f"За одну операцию можно выбрать не более {_MAX_JOIN_SELECTION} занятий."
        )
    for previous, current in pairwise(ordered):
        if current.starts_at < previous.ends_at:
            raise ValidationError(
                "Выбранные групповые занятия пересекаются между собой."
            )
    return ordered


def _validate_reused_join_series(
    series: AppointmentSeries,
    *,
    fingerprint: str,
) -> None:
    if (
        series.materialization_mode
        != AppointmentSeries.MaterializationMode.JOIN_EXISTING
        or series.operation_fingerprint != fingerprint
    ):
        raise ValidationError(
            "Ключ операции уже использован для другого набора групповых занятий."
        )


@transaction.atomic
def _create_join_series_definition(
    block: ProgramBlock,
    appointments: tuple[Appointment, ...],
    *,
    operation_key: UUID,
) -> tuple[AppointmentSeries, bool]:
    fingerprint = _group_join_fingerprint(block, appointments)
    existing = AppointmentSeries.objects.filter(operation_key=operation_key).first()
    if existing:
        _validate_reused_join_series(existing, fingerprint=fingerprint)
        return existing, True

    first = appointments[0]
    local_starts = [timezone.localtime(item.starts_at) for item in appointments]
    series = AppointmentSeries(
        operation_key=operation_key,
        operation_fingerprint=fingerprint,
        materialization_mode=AppointmentSeries.MaterializationMode.JOIN_EXISTING,
        child=block.program.child,
        service=block.service,
        staff_member=first.staff_member,
        room=first.room,
        program_block=block,
        title=f"{block.service.name}: присоединение {block.program.child}"[:200],
        start_date=min(item.date() for item in local_starts),
        end_date=max(item.date() for item in local_starts),
        days_of_week=_days_of_week_value(item.weekday() for item in local_starts),
        time=local_starts[0].time().replace(tzinfo=None),
        duration_minutes=first.duration_minutes,
        session_type=Appointment.SessionType.GROUP,
        default_appointment_status=first.status,
        status=AppointmentSeries.Status.ACTIVE,
    )
    series.full_clean()
    series.save()
    AppointmentSeriesParticipant.objects.create(
        series=series,
        child=block.program.child,
        program_block=block,
        billing_account=block.balance_account,
        position=1,
    )
    return series, False


def _join_one_appointment(
    series: AppointmentSeries,
    revision: AppointmentSeriesRevision,
    appointment: Appointment,
    *,
    actor: Any,
) -> tuple[AppointmentSeriesOccurrence, bool]:
    expected_starts_at = appointment.starts_at
    try:
        with transaction.atomic():
            locked_series = AppointmentSeries.objects.select_for_update().get(pk=series.pk)
            series_revisions.assert_current_projection(locked_series, revision)
            existing = AppointmentSeriesOccurrence.objects.filter(
                series=locked_series,
                scheduled_starts_at=expected_starts_at,
            ).first()
            if existing:
                return existing, False
            if (
                locked_series.materialization_mode
                != AppointmentSeries.MaterializationMode.JOIN_EXISTING
            ):
                raise _SkipOccurrence("series_mode", "Серия не предназначена для присоединения.")

            room_id = (
                Appointment.objects.filter(pk=appointment.pk)
                .values_list("room_id", flat=True)
                .first()
            )
            if room_id is None:
                raise _SkipOccurrence(
                    "target_missing",
                    "Групповое занятие удалено или осталось без кабинета.",
                )
            with schedule_writes.lock_schedule_write(
                appointment_id=appointment.pk,
                room_ids=[room_id],
            ) as locked:
                target = locked.appointment
                if target is None:
                    raise _SkipOccurrence("target_missing", "Групповое занятие удалено.")
                if target.starts_at != expected_starts_at:
                    raise _SkipOccurrence(
                        "target_changed",
                        "Дата или время группового занятия изменились после выбора.",
                    )
                if (
                    target.room_id != revision.room_id
                    or target.service_id != revision.service_id
                    or target.duration_minutes != revision.duration_minutes
                ):
                    raise _SkipOccurrence(
                        "target_changed",
                        "Кабинет, услуга или длительность занятия изменились после выбора.",
                    )

                memberships = list(
                    revision.participants.select_related(
                        "child",
                        "program_block",
                        "billing_account",
                    ).order_by("position", "pk")
                )
                if len(memberships) != 1 or not memberships[0].program_block_id:
                    raise _SkipOccurrence(
                        "series_composition",
                        "Состав серии присоединения поврежден.",
                    )
                membership = memberships[0]
                block = (
                    ProgramBlock.objects.select_for_update(of=("self",))
                    .select_related("program__child", "service", "balance_account")
                    .get(pk=membership.program_block_id)
                )
                block.program = TreatmentProgram.objects.select_for_update().get(
                    pk=block.program_id
                )
                if block.program.child_id != membership.child_id:
                    raise _SkipOccurrence(
                        "series_composition",
                        "Каскад больше не принадлежит получателю серии.",
                    )
                if membership.billing_account_id != block.balance_account_id:
                    raise _SkipOccurrence(
                        "funding_changed",
                        "Счет каскада изменился после выбора групповых занятий.",
                    )
                if block.balance_account_id:
                    block.balance_account = BalanceAccount.all_objects.select_for_update().get(
                        pk=block.balance_account_id
                    )

                candidate = _group_join_candidate(
                    block,
                    target,
                    planned_remaining=max(
                        block.planned_sessions - block.scheduled_count,
                        0,
                    ),
                    funded_remaining=program_wizard.funded_sessions_remaining(
                        block,
                        on_date=timezone.localtime(target.starts_at).date(),
                    ),
                )
                if not candidate.ready:
                    raise _SkipOccurrence(candidate.reason_code, candidate.reason)

                room = locked.room_for(target.room_id)
                schedule_writes.ensure_room_capacity(
                    starts_at=target.starts_at,
                    ends_at=target.ends_at,
                    children=[*_appointment_children(target), membership.child],
                    staff_members=_appointment_staff_members(target),
                    room=room,
                    status=target.status,
                    exclude_pk=target.pk,
                )
                participant = AppointmentParticipant.objects.create(
                    appointment=target,
                    child=membership.child,
                    billing_account=block.balance_account,
                    billing_decision=Appointment.BillingDecision.UNDECIDED,
                    price_snapshot=target.service.default_price,
                    program_block=block,
                    starts_at_snapshot=target.starts_at,
                    ends_at_snapshot=target.ends_at,
                    appointment_status=target.status,
                    admin_note="Присоединено мастером групповых занятий.",
                )
                if block.status == ProgramBlock.Status.PLANNED:
                    ProgramBlock.objects.filter(pk=block.pk).update(
                        status=ProgramBlock.Status.SCHEDULED,
                        updated_at=timezone.now(),
                    )
                return (
                    AppointmentSeriesOccurrence.objects.create(
                        series=locked_series,
                        scheduled_starts_at=target.starts_at,
                        appointment=target,
                        appointment_participant=participant,
                        outcome=AppointmentSeriesOccurrence.Outcome.JOINED,
                        created_by=actor if getattr(actor, "pk", None) else None,
                    ),
                    True,
                )
    except _SkipOccurrence as exc:
        return _record_skipped(
            series,
            expected_starts_at,
            code=exc.code,
            reason=exc.reason,
            actor=actor,
        )
    except series_revisions.SeriesRevisionMismatch:
        raise
    except Appointment.DoesNotExist:
        return _record_skipped(
            series,
            expected_starts_at,
            code="target_missing",
            reason="Групповое занятие было удалено во время присоединения.",
            actor=actor,
        )
    except (ValidationError, IntegrityError) as exc:
        return _record_skipped(
            series,
            expected_starts_at,
            code="stale_conflict",
            reason=(
                "Расписание изменилось во время присоединения: "
                + "; ".join(exc.messages)
                if isinstance(exc, ValidationError)
                else "Расписание изменилось во время присоединения; занятие безопасно пропущено."
            ),
            actor=actor,
        )


def join_program_block_to_groups(
    *,
    block: ProgramBlock,
    appointments: Iterable[Appointment],
    operation_key: UUID,
    actor: Any = None,
) -> GroupJoinCreateResult:
    series_revisions.require_operator_role(actor)
    selected = _validate_join_selection(appointments)
    try:
        series, reused = _create_join_series_definition(
            block,
            selected,
            operation_key=operation_key,
        )
    except IntegrityError:
        series = AppointmentSeries.objects.filter(operation_key=operation_key).first()
        if series is None:
            raise
        _validate_reused_join_series(
            series,
            fingerprint=_group_join_fingerprint(
                block,
                selected,
            ),
        )
        reused = True

    revision, _ = series_revisions.ensure_initial_revision(series, actor=actor)
    run, _ = series_revisions.get_or_create_initial_run(
        series,
        revision,
        operation_key=series.operation_key,
        actor=actor,
        expected_result_count=len(selected),
        target_appointment_ids=[appointment.pk for appointment in selected],
    )
    joined_count = 0
    skipped_count = 0
    unchanged_count = 0
    for appointment in selected:
        with transaction.atomic():
            occurrence, created = _join_one_appointment(
                series,
                revision,
                appointment,
                actor=actor,
            )
            series_revisions.record_compatibility_result(run, occurrence)
        if not created:
            unchanged_count += 1
        elif occurrence.outcome == AppointmentSeriesOccurrence.Outcome.JOINED:
            joined_count += 1
        else:
            skipped_count += 1
    series_revisions.complete_run(run)
    return GroupJoinCreateResult(
        series=series,
        joined_count=joined_count,
        skipped_count=skipped_count,
        unchanged_count=unchanged_count,
        reused_series=reused,
    )
