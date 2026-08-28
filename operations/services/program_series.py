"""Fixed-composition program series preview and materialization."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
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
    AppointmentSeriesStaffAssignment,
    AppointmentStaffAssignment,
    BalanceAccount,
    ProgramBlock,
    Room,
    StaffMember,
    TreatmentProgram,
)
from operations.services import program_wizard, scheduling


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


def _unique_in_input_order(items: Iterable[Any]) -> tuple[Any, ...]:
    result = []
    seen_pks = set()
    for item in items:
        if item.pk not in seen_pks:
            result.append(item)
            seen_pks.add(item.pk)
    return tuple(result)


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
    existing = AppointmentSeries.objects.filter(operation_key=operation_key).first()
    if existing:
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


def _materialize_one_date(
    series: AppointmentSeries,
    starts_at: datetime,
    *,
    actor: Any,
) -> tuple[AppointmentSeriesOccurrence, bool]:
    try:
        with transaction.atomic():
            locked_series = AppointmentSeries.objects.select_for_update().get(pk=series.pk)
            existing = AppointmentSeriesOccurrence.objects.filter(
                series=locked_series,
                scheduled_starts_at=starts_at,
            ).first()
            if existing:
                return existing, False

            participants, assignments = _series_composition(locked_series)
            room_id = locked_series.room_id
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
                if not locked_series.allow_unpaid_reserve:
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
                    allow_unpaid_reserve=locked_series.allow_unpaid_reserve,
                    on_date=timezone.localtime(starts_at).date(),
                )
                if limit_reason:
                    raise _SkipOccurrence(*limit_reason)

                staff_members = [assignment.staff_member for assignment in assignments]
                ends_at = starts_at + timedelta(minutes=locked_series.duration_minutes)
                conflict = _slot_conflict(
                    starts_at=starts_at,
                    ends_at=ends_at,
                    blocks=blocks,
                    staff_members=staff_members,
                    room=room,
                    allow_outside_availability=locked_series.allow_outside_availability,
                )
                if conflict[0]:
                    raise _SkipOccurrence(conflict[0], conflict[1])
                schedule_writes.ensure_room_capacity(
                    starts_at=starts_at,
                    ends_at=ends_at,
                    children=[participant.child for participant in participants],
                    staff_members=staff_members,
                    room=room,
                    status=locked_series.default_appointment_status,
                )

                primary = participants[0]
                primary_staff = assignments[0]
                appointment = Appointment.objects.create(
                    child=primary.child,
                    staff_member=primary_staff.staff_member,
                    service=locked_series.service,
                    room=room,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    session_type=Appointment.SessionType.GROUP,
                    title=locked_series.title,
                    status=locked_series.default_appointment_status,
                    billing_account=primary.billing_account,
                    billing_decision=Appointment.BillingDecision.UNDECIDED,
                    series=locked_series,
                    program_block=primary.program_block,
                    staff_availability_override=locked_series.allow_outside_availability,
                    staff_availability_override_reason=(
                        locked_series.override_reason
                        if locked_series.allow_outside_availability
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
                for assignment in assignments[1:]:
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
    if series.status != AppointmentSeries.Status.ACTIVE:
        raise ValidationError("Создавать занятия можно только для активной серии.")
    if series.session_type != Appointment.SessionType.GROUP:
        raise ValidationError("Этот сервис предназначен для групповой серии.")

    created_count = 0
    skipped_count = 0
    unchanged_count = 0
    starts = _candidate_starts(
        series.start_date,
        series.end_date,
        (AppointmentSeries.DAY_MAP[token.strip().upper()] for token in series.days_of_week.split(",")),
        series.time,
    )
    for starts_at in starts:
        occurrence, created = _materialize_one_date(series, starts_at, actor=actor)
        if not created:
            unchanged_count += 1
        elif occurrence.outcome == AppointmentSeriesOccurrence.Outcome.CREATED:
            created_count += 1
        else:
            skipped_count += 1
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
    try:
        series, reused = _create_series_definition(preview, operation_key=operation_key)
    except IntegrityError:
        series = AppointmentSeries.objects.filter(operation_key=operation_key).first()
        if series is None:
            raise
        reused = True
    result = materialize_group_series(series, actor=actor)
    return GroupSeriesCreateResult(
        series=series,
        created_count=result.created_count,
        skipped_count=result.skipped_count,
        unchanged_count=result.unchanged_count,
        reused_series=reused,
    )
