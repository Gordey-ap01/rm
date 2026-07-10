"""Мастер подбора расписания и финансовых переносов для каскадов занятий."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from operations.models import Appointment, BalanceAccount, ProgramBlock, Room, StaffMember
from operations.services import scheduling


@dataclass(frozen=True)
class ScheduleSlot:
    starts_at: datetime
    ends_at: datetime
    staff_member: StaffMember
    room: Room
    room_capacity: int
    room_occupancy: int
    availability_warning: str = ""
    selection_note: str = ""


@dataclass(frozen=True)
class SchedulePreview:
    block: ProgramBlock
    requested_count: int
    allowed_count: int
    funded_remaining: int | None
    slots: list[ScheduleSlot]
    skipped_conflicts: int = 0
    skipped_availability: int = 0

    @property
    def missing_count(self) -> int:
        return max(self.allowed_count - len(self.slots), 0)

    @property
    def limited_by_balance(self) -> bool:
        return self.funded_remaining is not None and self.allowed_count < self.requested_count


@dataclass(frozen=True)
class ScheduleCreateResult:
    preview: SchedulePreview
    appointments: list[Appointment]


def _local_datetime(day: date, clock: time) -> datetime:
    return timezone.make_aware(datetime.combine(day, clock), timezone.get_current_timezone())


def _iter_days(date_from: date, date_to: date, weekdays: set[int]):
    cursor = date_from
    while cursor <= date_to:
        if cursor.weekday() in weekdays:
            yield cursor
        cursor += timedelta(days=1)


def _iter_day_starts(day: date, start_at: time, end_at: time, duration_minutes: int, step_minutes: int):
    cursor = _local_datetime(day, start_at)
    limit = _local_datetime(day, end_at)
    duration = timedelta(minutes=duration_minutes)
    step = timedelta(minutes=step_minutes)
    while cursor + duration <= limit:
        yield cursor, cursor + duration
        cursor += step


def account_session_capacity(account: BalanceAccount | None, service) -> int | None:
    """Сколько занятий можно покрыть текущим остатком счета.

    ``None`` означает, что счета нет или стоимость в рублях не задана, поэтому
    мастер не может честно ограничить количество по деньгам.
    """
    if account is None:
        return None
    if not account.can_pay_for(service):
        return 0

    balance = account.current_balance
    if balance <= 0:
        return 0
    if account.unit == BalanceAccount.Unit.SESSIONS:
        return int(balance.to_integral_value(rounding=ROUND_FLOOR))

    price = service.default_price
    if not price or price <= 0:
        return None
    return int((balance / price).to_integral_value(rounding=ROUND_FLOOR))


def funded_sessions_remaining(block: ProgramBlock) -> int | None:
    capacity = account_session_capacity(block.balance_account, block.service)
    if capacity is None:
        return None

    inactive_statuses = [Appointment.Status.CANCELLED, Appointment.Status.RESCHEDULED]
    reserved_participants_without_charge = (
        block.appointment_participants.exclude(appointment_status__in=inactive_statuses)
        .exclude(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account__isnull=False,
        )
        .count()
    )
    legacy_appointments_without_charge = (
        block.appointments.filter(participants__isnull=True)
        .exclude(status__in=inactive_statuses)
        .exclude(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account__isnull=False,
        )
        .count()
    )
    return max(
        capacity - reserved_participants_without_charge - legacy_appointments_without_charge,
        0,
    )


def estimate_sessions_for_amount(account: BalanceAccount | None, service, amount: Decimal | None = None) -> int | None:
    if account is None:
        return None
    value = amount if amount is not None else account.current_balance
    if value <= 0:
        return 0
    if account.unit == BalanceAccount.Unit.SESSIONS:
        return int(value.to_integral_value(rounding=ROUND_FLOOR))
    if not service.default_price or service.default_price <= 0:
        return None
    return int((value / service.default_price).to_integral_value(rounding=ROUND_FLOOR))


def _overlaps_selected_slots(starts_at: datetime, ends_at: datetime, slots: list[ScheduleSlot]) -> bool:
    return any(starts_at < slot.ends_at and ends_at > slot.starts_at for slot in slots)


def _ordered_staff_candidates(block: ProgramBlock, selected: StaffMember | None) -> list[StaffMember]:
    if selected is not None:
        return [selected]

    qs: QuerySet[StaffMember] = StaffMember.objects.filter(status=StaffMember.Status.ACTIVE).order_by("full_name")
    staff = list(qs)
    if block.staff_member_id:
        staff.sort(key=lambda item: (item.pk != block.staff_member_id, item.full_name))
    return staff


def _ordered_room_candidates(selected: Room | None) -> list[Room]:
    if selected is not None:
        return [selected]
    return list(Room.objects.filter(is_active=True).order_by("-capacity", "name"))


def _service_match_score(block: ProgramBlock, staff_member: StaffMember) -> int:
    haystack = f"{staff_member.specializations} {staff_member.full_name}".lower()
    service_name = block.service.name.lower()
    category_label = block.service.get_category_display().lower()
    if service_name and service_name in haystack:
        return 0
    if category_label and category_label in haystack:
        return 1
    if block.staff_member_id and staff_member.pk == block.staff_member_id:
        return 2
    return 3


def _slot_rank(block: ProgramBlock, slot: ScheduleSlot) -> tuple[int, int, int, str]:
    return (
        1 if slot.availability_warning else 0,
        _service_match_score(block, slot.staff_member),
        slot.room_occupancy,
        slot.staff_member.full_name,
    )


def suggest_program_block_slots(
    block: ProgramBlock,
    *,
    date_from: date,
    date_to: date,
    weekdays: set[int],
    time_from: time,
    time_until: time,
    duration_minutes: int,
    staff_member: StaffMember | None = None,
    room: Room | None = None,
    requested_count: int,
    allow_outside_availability: bool = False,
    allow_unpaid_reserve: bool = False,
    step_minutes: int = 30,
) -> SchedulePreview:
    funded_remaining = funded_sessions_remaining(block)
    allowed_count = requested_count
    if not allow_unpaid_reserve and funded_remaining is not None:
        allowed_count = min(requested_count, funded_remaining)

    slots: list[ScheduleSlot] = []
    skipped_conflicts = 0
    skipped_availability = 0

    if allowed_count <= 0:
        return SchedulePreview(
            block=block,
            requested_count=requested_count,
            allowed_count=allowed_count,
            funded_remaining=funded_remaining,
            slots=[],
        )

    staff_candidates = _ordered_staff_candidates(block, staff_member)
    room_candidates = _ordered_room_candidates(room)

    for day in _iter_days(date_from, date_to, weekdays):
        if day < timezone.localdate():
            continue
        for starts_at, ends_at in _iter_day_starts(day, time_from, time_until, duration_minutes, step_minutes):
            if _overlaps_selected_slots(starts_at, ends_at, slots):
                skipped_conflicts += 1
                continue

            candidates_for_start: list[ScheduleSlot] = []
            had_conflict = False
            had_availability_skip = False
            for staff_candidate in staff_candidates:
                for room_candidate in room_candidates:
                    report = scheduling.find_overlaps(
                        starts_at,
                        ends_at,
                        child=block.program.child,
                        staff_member=staff_candidate,
                        room=room_candidate,
                    )
                    if report.has_conflict:
                        had_conflict = True
                        continue

                    availability_warning = scheduling.is_within_availability(staff_candidate, starts_at, ends_at)
                    if availability_warning and not allow_outside_availability:
                        had_availability_skip = True
                        continue

                    candidates_for_start.append(
                        ScheduleSlot(
                            starts_at=starts_at,
                            ends_at=ends_at,
                            staff_member=staff_candidate,
                            room=room_candidate,
                            room_capacity=report.room_capacity,
                            room_occupancy=report.room_occupancy,
                            availability_warning=availability_warning,
                            selection_note=_selection_note(
                                block,
                                staff_member=staff_member,
                                room=room,
                                staff_candidate=staff_candidate,
                                room_candidate=room_candidate,
                            ),
                        )
                    )

            if not candidates_for_start:
                if had_availability_skip:
                    skipped_availability += 1
                elif had_conflict:
                    skipped_conflicts += 1
                continue

            slots.append(sorted(candidates_for_start, key=lambda candidate: _slot_rank(block, candidate))[0])
            if len(slots) >= allowed_count:
                return SchedulePreview(
                    block=block,
                    requested_count=requested_count,
                    allowed_count=allowed_count,
                    funded_remaining=funded_remaining,
                    slots=slots,
                    skipped_conflicts=skipped_conflicts,
                    skipped_availability=skipped_availability,
                )

    return SchedulePreview(
        block=block,
        requested_count=requested_count,
        allowed_count=allowed_count,
        funded_remaining=funded_remaining,
        slots=slots,
        skipped_conflicts=skipped_conflicts,
        skipped_availability=skipped_availability,
    )


def _selection_note(
    block: ProgramBlock,
    *,
    staff_member: StaffMember | None,
    room: Room | None,
    staff_candidate: StaffMember,
    room_candidate: Room,
) -> str:
    notes: list[str] = []
    if staff_member is None:
        if block.staff_member_id and staff_candidate.pk == block.staff_member_id:
            notes.append("специалист из каскада")
        elif _service_match_score(block, staff_candidate) <= 1:
            notes.append("похожая специализация")
        else:
            notes.append("свободный специалист")
    if room is None:
        notes.append("свободный кабинет")
    return ", ".join(notes)


def _format_slot_range(starts_at: datetime, ends_at: datetime) -> str:
    local_start = timezone.localtime(starts_at)
    local_end = timezone.localtime(ends_at)
    if local_start.date() == local_end.date():
        return f"{local_start:%d.%m.%Y %H:%M}-{local_end:%H:%M}"
    return f"{local_start:%d.%m.%Y %H:%M}-{local_end:%d.%m.%Y %H:%M}"


def _validate_slot_still_free(block: ProgramBlock, slot: ScheduleSlot) -> None:
    report = scheduling.find_overlaps(
        slot.starts_at,
        slot.ends_at,
        child=block.program.child,
        staff_member=slot.staff_member,
        room=slot.room,
    )
    if not report.has_conflict:
        return

    messages = ", ".join(report.human_messages())
    raise ValidationError(
        f"Окно {_format_slot_range(slot.starts_at, slot.ends_at)} уже занято: {messages}. "
        "Нажмите «Подобрать окна» ещё раз."
    )


@transaction.atomic
def create_schedule_from_preview(
    preview: SchedulePreview,
    *,
    status: str = Appointment.Status.PROPOSED,
    actor: Any = None,
) -> ScheduleCreateResult:
    appointments: list[Appointment] = []
    block = preview.block
    for slot in preview.slots:
        _validate_slot_still_free(block, slot)
        override_reason = ""
        if slot.availability_warning:
            override_reason = f"Создано мастером расписания вне графика: {slot.availability_warning}."
            if actor:
                override_reason += f" Администратор: {actor}."
        appointment = Appointment.objects.create(
            child=block.program.child,
            service=block.service,
            staff_member=slot.staff_member,
            room=slot.room,
            starts_at=slot.starts_at,
            ends_at=slot.ends_at,
            status=status,
            billing_account=block.balance_account if block.balance_account_id else None,
            billing_decision=Appointment.BillingDecision.UNDECIDED,
            program_block=block,
            staff_availability_override=bool(slot.availability_warning),
            staff_availability_override_reason=override_reason,
            admin_note="Создано мастером автоподбора каскада.",
        )
        appointments.append(appointment)

    if appointments:
        block.status = ProgramBlock.Status.SCHEDULED
        block.save(update_fields=["status", "updated_at"])

    return ScheduleCreateResult(preview=preview, appointments=appointments)
