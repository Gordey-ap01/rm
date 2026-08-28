"""Мастер подбора расписания и финансовых переносов для каскадов занятий."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from operations import schedule_writes
from operations.models import (
    Appointment,
    AppointmentParticipant,
    BalanceAccount,
    ProgramBlock,
    Room,
    StaffMember,
    TreatmentProgram,
)
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
    allow_unpaid_reserve: bool = False
    skipped_conflicts: int = 0
    skipped_availability: int = 0

    @property
    def missing_count(self) -> int:
        return max(self.allowed_count - len(self.slots), 0)

    @property
    def limited_by_balance(self) -> bool:
        planned_remaining = max(self.block.planned_sessions - self.block.scheduled_count, 0)
        return self.funded_remaining is not None and self.funded_remaining < min(
            self.requested_count,
            planned_remaining,
        )

    @property
    def limited_by_plan(self) -> bool:
        planned_remaining = max(self.block.planned_sessions - self.block.scheduled_count, 0)
        return planned_remaining < self.requested_count


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


def account_availability_reason(
    account: BalanceAccount | None,
    service,
    *,
    on_date: date | None = None,
) -> str:
    if account is None:
        return "счет оплаты не выбран"
    if account.is_archived:
        return "счет оплаты архивирован"
    if account.status != BalanceAccount.Status.ACTIVE:
        return f"счет оплаты имеет статус «{account.get_status_display()}»"
    if not account.can_pay_for(service):
        return "счет оплаты не подходит для услуги"
    if on_date and account.valid_from and on_date < account.valid_from:
        return f"счет оплаты действует с {account.valid_from:%d.%m.%Y}"
    if on_date and account.valid_until and on_date > account.valid_until:
        return f"срок счета оплаты истек {account.valid_until:%d.%m.%Y}"
    return ""


def program_availability_reason(
    block: ProgramBlock,
    *,
    on_date: date | None = None,
) -> str:
    if block.status in {ProgramBlock.Status.COMPLETED, ProgramBlock.Status.CANCELLED}:
        return f"каскад имеет статус «{block.get_status_display()}»"
    program = block.program
    if program.status != TreatmentProgram.Status.ACTIVE:
        return f"программа имеет статус «{program.get_status_display()}»"
    if on_date and program.starts_on and on_date < program.starts_on:
        return f"программа действует с {program.starts_on:%d.%m.%Y}"
    if on_date and program.ends_on and on_date > program.ends_on:
        return f"программа завершилась {program.ends_on:%d.%m.%Y}"
    return ""


def account_session_capacity(account: BalanceAccount | None, service) -> int:
    """Return a fail-closed session capacity for the account's current balance."""
    if account_availability_reason(account, service):
        return 0

    assert account is not None
    balance = account.current_balance
    if balance <= 0:
        return 0
    if account.unit == BalanceAccount.Unit.SESSIONS:
        return int(balance.to_integral_value(rounding=ROUND_FLOOR))

    price = service.default_price
    if not price or price <= 0:
        return 0
    return int((balance / price).to_integral_value(rounding=ROUND_FLOOR))


def _account_reservations(account: BalanceAccount):
    inactive_statuses = [Appointment.Status.CANCELLED, Appointment.Status.RESCHEDULED]
    account_match = Q(billing_account=account) | Q(
        billing_account__isnull=True,
        program_block__balance_account=account,
    )
    participants = list(
        AppointmentParticipant.objects.filter(account_match)
        .exclude(appointment_status__in=inactive_statuses)
        .exclude(billing_decision=Appointment.BillingDecision.DO_NOT_CHARGE)
        .exclude(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=account,
        )
        .select_related("appointment__service")
    )
    legacy_appointments = list(
        Appointment.objects.filter(participants__isnull=True)
        .filter(
            Q(billing_account=account)
            | Q(billing_account__isnull=True, program_block__balance_account=account)
        )
        .exclude(status__in=inactive_statuses)
        .exclude(billing_decision=Appointment.BillingDecision.DO_NOT_CHARGE)
        .exclude(
            billing_decision=Appointment.BillingDecision.CHARGE,
            billing_account=account,
        )
        .select_related("service")
    )
    return participants, legacy_appointments


def funded_sessions_remaining(
    block: ProgramBlock,
    *,
    on_date: date | None = None,
) -> int:
    if account_availability_reason(block.balance_account, block.service, on_date=on_date):
        return 0

    account = block.balance_account
    assert account is not None
    capacity = account_session_capacity(account, block.service)
    participants, legacy_appointments = _account_reservations(account)
    if account.unit == BalanceAccount.Unit.SESSIONS:
        return max(capacity - len(participants) - len(legacy_appointments), 0)

    target_price = block.service.default_price
    if not target_price or target_price <= 0:
        return 0
    reserved_amount = Decimal("0")
    for participant in participants:
        price = participant.price_snapshot or participant.appointment.service.default_price
        if not price or price <= 0:
            return 0
        reserved_amount += price
    for appointment in legacy_appointments:
        price = appointment.service.default_price
        if not price or price <= 0:
            return 0
        reserved_amount += price
    available_amount = max(account.current_balance - reserved_amount, Decimal("0"))
    return int((available_amount / target_price).to_integral_value(rounding=ROUND_FLOOR))


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
    planned_remaining = max(block.planned_sessions - block.scheduled_count, 0)
    allowed_count = min(requested_count, planned_remaining)
    if program_availability_reason(block):
        allowed_count = 0
    if not allow_unpaid_reserve and funded_remaining is not None:
        allowed_count = min(allowed_count, funded_remaining)

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
            allow_unpaid_reserve=allow_unpaid_reserve,
        )

    staff_candidates = _ordered_staff_candidates(block, staff_member)
    room_candidates = _ordered_room_candidates(room)

    for day in _iter_days(date_from, date_to, weekdays):
        if day < timezone.localdate():
            continue
        if program_availability_reason(block, on_date=day):
            continue
        if not allow_unpaid_reserve and account_availability_reason(
            block.balance_account,
            block.service,
            on_date=day,
        ):
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
                    allow_unpaid_reserve=allow_unpaid_reserve,
                    skipped_conflicts=skipped_conflicts,
                    skipped_availability=skipped_availability,
                )

    return SchedulePreview(
        block=block,
        requested_count=requested_count,
        allowed_count=allowed_count,
        funded_remaining=funded_remaining,
        slots=slots,
        allow_unpaid_reserve=allow_unpaid_reserve,
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
    allowed_statuses = {
        Appointment.Status.PROPOSED,
        Appointment.Status.RESERVED,
        Appointment.Status.CONFIRMED,
    }
    if status not in allowed_statuses:
        raise ValidationError("Для мастера выбран недопустимый статус занятия.")
    if preview.allow_unpaid_reserve and status != Appointment.Status.RESERVED:
        raise ValidationError("Занятия сверх оплаты должны создаваться со статусом «Бронь».")

    appointments: list[Appointment] = []
    with schedule_writes.lock_schedule_write(
        room_ids=[slot.room.pk for slot in preview.slots],
    ) as locked:
        block = (
            ProgramBlock.objects.select_for_update(of=("self",))
            .select_related("program__child", "service", "balance_account")
            .get(pk=preview.block.pk)
        )
        block.program = TreatmentProgram.objects.select_for_update().get(
            pk=block.program_id
        )
        for slot in preview.slots:
            reason = program_availability_reason(
                block,
                on_date=timezone.localtime(slot.starts_at).date(),
            )
            if reason:
                raise ValidationError(
                    f"Программа изменилась: {reason}. "
                    "Нажмите «Подобрать окна» ещё раз."
                )
        if len(preview.slots) > max(block.planned_sessions - block.scheduled_count, 0):
            raise ValidationError(
                "План каскада изменился. Нажмите «Подобрать окна» ещё раз."
            )

        if not preview.allow_unpaid_reserve:
            if block.balance_account_id:
                locked_account = BalanceAccount.all_objects.select_for_update().get(
                    pk=block.balance_account_id
                )
                block.balance_account = locked_account
            for slot in preview.slots:
                reason = account_availability_reason(
                    block.balance_account,
                    block.service,
                    on_date=timezone.localtime(slot.starts_at).date(),
                )
                if reason:
                    raise ValidationError(
                        f"Оплата изменилась: {reason}. Нажмите «Подобрать окна» ещё раз."
                    )
            if funded_sessions_remaining(block) < len(preview.slots):
                raise ValidationError(
                    "Доступная оплата изменилась. Нажмите «Подобрать окна» ещё раз."
                )

        for slot in preview.slots:
            _validate_slot_still_free(block, slot)
            room = locked.room_for(slot.room.pk)
            schedule_writes.ensure_room_capacity(
                starts_at=slot.starts_at,
                ends_at=slot.ends_at,
                children=[block.program.child],
                staff_members=[slot.staff_member],
                room=room,
                status=status,
            )
            override_reason = ""
            if slot.availability_warning:
                override_reason = (
                    "Создано мастером расписания вне графика: "
                    f"{slot.availability_warning}."
                )
                if actor:
                    override_reason += f" Администратор: {actor}."
            appointment = Appointment.objects.create(
                child=block.program.child,
                service=block.service,
                staff_member=slot.staff_member,
                room=room,
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
