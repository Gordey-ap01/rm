"""Бизнес-логика занятий: создание, перенос, отмена, отметка специалиста."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from operations import schedule_writes as schedule_write_svc
from operations.models import (
    Appointment,
    AppointmentParticipant,
    AppointmentStaffAssignment,
    BalanceAccount,
    Child,
    LedgerEntry,
    Service,
    StaffMember,
)


@dataclass(frozen=True)
class MoveResult:
    """Результат переноса занятия."""

    old: Appointment
    new: Appointment


def _copy_rescheduled_participants(old: Appointment, new: Appointment) -> None:
    for participant in old.participants.select_related(
        "child", "billing_account", "program_block"
    ).order_by("pk"):
        AppointmentParticipant.objects.update_or_create(
            appointment=new,
            child=participant.child,
            defaults={
                "attendance_status": Appointment.AttendanceStatus.UNKNOWN,
                "billing_decision": Appointment.BillingDecision.UNDECIDED,
                "billing_account": participant.billing_account,
                "price_snapshot": None,
                "program_block": participant.program_block,
                "sequence_number": participant.sequence_number,
                "source_participant": participant,
                "admin_note": participant.admin_note,
                "specialist_note": "",
                "marked_by_staff_at": None,
                "starts_at_snapshot": new.starts_at,
                "ends_at_snapshot": new.ends_at,
                "appointment_status": new.status,
            },
        )


def _copy_rescheduled_staff_assignments(
    old: Appointment, new: Appointment, primary_staff: StaffMember
) -> None:
    assignments = list(old.staff_assignments.select_related("staff_member").order_by("pk"))
    if not assignments:
        return
    primary_replaced = False
    seen = set()
    for assignment in assignments:
        staff = assignment.staff_member
        if assignment.role == AppointmentStaffAssignment.Role.PRIMARY and not primary_replaced:
            staff = primary_staff
            primary_replaced = True
        if staff.pk in seen:
            continue
        seen.add(staff.pk)
        AppointmentStaffAssignment.objects.update_or_create(
            appointment=new,
            staff_member=staff,
            defaults={
                "role": assignment.role,
                "starts_at_snapshot": new.starts_at,
                "ends_at_snapshot": new.ends_at,
                "appointment_status": new.status,
                "override_availability": False,
                "override_reason": "",
            },
        )


def _rescheduled_staff_members(
    appointment: Appointment,
    primary_staff: StaffMember,
) -> list[StaffMember]:
    """Return the assignment set that will exist on a rescheduled appointment."""

    assignments = list(
        appointment.staff_assignments.select_related("staff_member").order_by("pk")
    )
    if not assignments:
        return [primary_staff]

    members: list[StaffMember] = []
    primary_replaced = False
    for assignment in assignments:
        staff = assignment.staff_member
        if assignment.role == AppointmentStaffAssignment.Role.PRIMARY and not primary_replaced:
            staff = primary_staff
            primary_replaced = True
        if staff not in members:
            members.append(staff)
    if primary_staff not in members:
        members.insert(0, primary_staff)
    return members


def create_appointment(
    *,
    child: Child,
    staff_member: StaffMember,
    service: Service,
    starts_at: datetime,
    ends_at: datetime,
    room: Any = None,
    status: str = Appointment.Status.CONFIRMED,
    billing_account: BalanceAccount | None = None,
    admin_note: str = "",
    validate_schedule: bool = True,
) -> Appointment:
    """Создаёт занятие.

    ``validate_schedule=True`` (по умолчанию) запускает
    :py:meth:`Appointment.full_clean`, который проверяет пересечения и бизнес-правила.
    Для bulk-операций передавайте ``validate_schedule=False``.
    """
    room_id = getattr(room, "pk", None)
    with schedule_write_svc.lock_schedule_write(room_ids=[room_id]) as locked:
        locked_room = locked.room_for(room_id)
        if validate_schedule:
            schedule_write_svc.ensure_room_capacity(
                starts_at=starts_at,
                ends_at=ends_at,
                children=[child],
                staff_members=[staff_member],
                room=locked_room,
                status=status,
            )
        appointment = Appointment(
            child=child,
            staff_member=staff_member,
            service=service,
            room=locked_room,
            starts_at=starts_at,
            ends_at=ends_at,
            status=status,
            billing_account=billing_account,
            admin_note=admin_note,
        )
        appointment.save(validate_schedule=validate_schedule)
        return appointment


@transaction.atomic
def reschedule(
    appointment: Appointment,
    *,
    starts_at: datetime,
    ends_at: datetime,
    staff_member: StaffMember,
    room: Any,
    note: str = "",
    actor: Any = None,
) -> MoveResult:
    """Переносит занятие: помечает исходное как ``RESCHEDULED`` и создаёт новое."""
    room_id = getattr(room, "pk", None)
    with schedule_write_svc.lock_schedule_write(
        appointment_id=appointment.pk,
        room_ids=[room_id],
    ) as locked:
        appointment = locked.appointment
        if appointment is None:  # Defensive: appointment_id is required above.
            raise Appointment.DoesNotExist

        participants = list(
            appointment.participants.select_related(
                "child", "billing_account", "program_block"
            ).order_by("pk")
        )
        children = [participant.child for participant in participants] or [appointment.child]
        target_room = locked.room_for(room_id)
        schedule_write_svc.ensure_room_capacity(
            starts_at=starts_at,
            ends_at=ends_at,
            children=children,
            staff_members=_rescheduled_staff_members(appointment, staff_member),
            room=target_room,
            status=Appointment.Status.CONFIRMED,
            exclude_pk=appointment.pk,
        )

        legacy_child = appointment.child
        legacy_billing_account = appointment.billing_account
        if participants and all(
            participant.child_id != appointment.child_id for participant in participants
        ):
            legacy_child = participants[0].child
            legacy_billing_account = participants[0].billing_account

        local_start = timezone.localtime(starts_at)
        note_lines = [
            appointment.admin_note,
            f"Перенесено на {local_start:%d.%m.%Y %H:%M}.",
            note,
        ]
        appointment.admin_note = "\n".join(part for part in note_lines if part)
        appointment.status = Appointment.Status.RESCHEDULED
        appointment.save(update_fields=["status", "admin_note", "updated_at"], sync_legacy=False)
        now = timezone.now()
        appointment.participants.update(
            appointment_status=Appointment.Status.RESCHEDULED,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            updated_at=now,
        )
        appointment.staff_assignments.update(
            appointment_status=Appointment.Status.RESCHEDULED,
            starts_at_snapshot=appointment.starts_at,
            ends_at_snapshot=appointment.ends_at,
            updated_at=now,
        )

        new = Appointment.objects.create(
            child=legacy_child,
            service=appointment.service,
            staff_member=staff_member,
            room=target_room,
            starts_at=starts_at,
            ends_at=ends_at,
            status=Appointment.Status.CONFIRMED,
            attendance_status=Appointment.AttendanceStatus.UNKNOWN,
            billing_decision=Appointment.BillingDecision.UNDECIDED,
            billing_account=legacy_billing_account,
            source_appointment=appointment,
            series=appointment.series,
            program_block=appointment.program_block,
            sequence_number=appointment.sequence_number,
            session_type=appointment.session_type,
            title=appointment.title,
            admin_note=note,
        )
        _copy_rescheduled_participants(appointment, new)
        _copy_rescheduled_staff_assignments(appointment, new, staff_member)
        return MoveResult(old=appointment, new=new)


@transaction.atomic
def cancel(
    appointment: Appointment,
    *,
    status: str,
    reason_text: str,
    admin_note: str = "",
) -> Appointment:
    """Отменяет или помечает как no-show. ``reason_text`` — человекочитаемое описание."""
    appointment.status = status
    note_lines = [appointment.admin_note, f"Причина отмены: {reason_text}.", admin_note]
    appointment.admin_note = "\n".join(part for part in note_lines if part)
    appointment.save(update_fields=["status", "admin_note", "updated_at"])
    return appointment


@transaction.atomic
def record_attendance(
    appointment: Appointment,
    *,
    action: str,
    note: str = "",
) -> Appointment:
    """Отмечает факт проведения/неявки со стороны специалиста.

    ``action`` — ``"completed"`` или ``"not_completed"``.
    Решение по списанию остаётся за администратором.
    """
    if action == "completed":
        appointment.status = Appointment.Status.COMPLETED
        appointment.attendance_status = Appointment.AttendanceStatus.ATTENDED
    elif action == "not_completed":
        appointment.status = Appointment.Status.NO_SHOW
        appointment.attendance_status = Appointment.AttendanceStatus.MISSED
    else:
        raise ValueError(f"Неизвестное действие: {action!r}")
    if note:
        appointment.specialist_note = note
    marked_at = timezone.now()
    appointment.specialist_marked_at = marked_at
    appointment.save(
        update_fields=[
            "status",
            "attendance_status",
            "specialist_marked_at",
            "specialist_note",
            "updated_at",
        ]
    )
    participant_updates = {
        "appointment_status": appointment.status,
        "attendance_status": appointment.attendance_status,
        "starts_at_snapshot": appointment.starts_at,
        "ends_at_snapshot": appointment.ends_at,
        "marked_by_staff_at": marked_at,
        "updated_at": marked_at,
    }
    if note:
        participant_updates["specialist_note"] = note
    appointment.participants.update(**participant_updates)
    return appointment


def materialize_series(series_id: int, date_from: datetime, date_to: datetime) -> list[Appointment]:
    """Создаёт занятия по серии в диапазоне дат (используется в следующих этапах)."""
    raise NotImplementedError("materialize_series будет реализован на этапе 2.7")


def single_participant_or_none(appointment: Appointment) -> AppointmentParticipant | None:
    participants = list(appointment.participants.order_by("pk")[:2])
    if len(participants) > 1:
        raise ValueError("Для группового занятия нужно выбрать конкретного участника.")
    return participants[0] if participants else None


def sync_ledger_for_decision(
    appointment: Appointment,
    *,
    account: BalanceAccount,
    amount: Decimal,
    reason: str,
    actor: Any = None,
) -> LedgerEntry:
    """Создаёт или обновляет запись в журнале при смене решения по списанию.

    Используется из :py:mod:`operations.services.billing` для атомарной замены
    существующих операций по занятию.
    """
    participant = single_participant_or_none(appointment)
    ledger_qs = LedgerEntry.objects.filter(appointment=appointment)
    if participant is not None:
        ledger_qs = ledger_qs.filter(
            Q(appointment_participant=participant) | Q(appointment_participant__isnull=True)
        )
    else:
        ledger_qs = ledger_qs.filter(appointment_participant__isnull=True)

    if appointment.billing_account_id and appointment.billing_account_id != account.id:
        existing = ledger_qs.exclude(account=account)
        for entry in existing:
            entry.appointment = None
            entry.save(update_fields=["appointment", "updated_at"])

    entry = ledger_qs.filter(account=account).first()
    if entry is None:
        entry = LedgerEntry.objects.create(
            appointment=appointment,
            account=account,
            entry_type=LedgerEntry.EntryType.DEBIT,
            amount=amount,
            appointment_participant=participant,
            price_snapshot=appointment.service.default_price,
            reason=reason,
            created_by=actor,
        )
    else:
        entry.entry_type = LedgerEntry.EntryType.DEBIT
        entry.amount = amount
        entry.appointment_participant = participant
        entry.price_snapshot = appointment.service.default_price
        entry.reason = reason
        entry.created_by = actor
        entry.save(
            update_fields=[
                "entry_type",
                "amount",
                "appointment_participant",
                "price_snapshot",
                "reason",
                "created_by",
                "updated_at",
            ]
        )
    return entry


def bulk_unlink_ledger(appointments: Iterable[Appointment]) -> int:
    """Снимает привязку занятие→ledger при массовой отмене (для безопасного переноса)."""
    count = 0
    for appt in appointments:
        LedgerEntry.objects.filter(appointment=appt).update(appointment=None)
        count += 1
    return count
