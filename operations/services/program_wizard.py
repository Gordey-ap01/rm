"""Мастер подбора расписания и финансовых переносов для каскадов занятий."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from django.db import transaction
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

    reserved_without_charge = (
        block.appointments.exclude(status=Appointment.Status.CANCELLED)
        .exclude(billing_decision=Appointment.BillingDecision.CHARGE)
        .count()
    )
    return max(capacity - reserved_without_charge, 0)


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


def suggest_program_block_slots(
    block: ProgramBlock,
    *,
    date_from: date,
    date_to: date,
    weekdays: set[int],
    time_from: time,
    time_until: time,
    duration_minutes: int,
    staff_member: StaffMember,
    room: Room,
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

    for day in _iter_days(date_from, date_to, weekdays):
        if day < timezone.localdate():
            continue
        for starts_at, ends_at in _iter_day_starts(day, time_from, time_until, duration_minutes, step_minutes):
            report = scheduling.find_overlaps(
                starts_at,
                ends_at,
                child=block.program.child,
                staff_member=staff_member,
                room=room,
            )
            if report.has_conflict:
                skipped_conflicts += 1
                continue

            availability_warning = scheduling.is_within_availability(staff_member, starts_at, ends_at)
            if availability_warning and not allow_outside_availability:
                skipped_availability += 1
                continue

            slots.append(
                ScheduleSlot(
                    starts_at=starts_at,
                    ends_at=ends_at,
                    staff_member=staff_member,
                    room=room,
                    room_capacity=report.room_capacity,
                    room_occupancy=report.room_occupancy,
                    availability_warning=availability_warning,
                )
            )
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
