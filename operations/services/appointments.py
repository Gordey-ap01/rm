"""Бизнес-логика занятий: создание, перенос, отмена, отметка специалиста."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from operations.models import Appointment, BalanceAccount, Child, LedgerEntry, Service, StaffMember


@dataclass(frozen=True)
class MoveResult:
    """Результат переноса занятия."""

    old: Appointment
    new: Appointment


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
    appointment = Appointment(
        child=child,
        staff_member=staff_member,
        service=service,
        room=room,
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
    local_start = timezone.localtime(starts_at)
    note_lines = [appointment.admin_note, f"Перенесено на {local_start:%d.%m.%Y %H:%M}.", note]
    appointment.admin_note = "\n".join(part for part in note_lines if part)
    appointment.status = Appointment.Status.RESCHEDULED
    appointment.save(update_fields=["status", "admin_note", "updated_at"])

    new = Appointment.objects.create(
        child=appointment.child,
        service=appointment.service,
        staff_member=staff_member,
        room=room,
        starts_at=starts_at,
        ends_at=ends_at,
        status=Appointment.Status.CONFIRMED,
        attendance_status=Appointment.AttendanceStatus.UNKNOWN,
        billing_decision=Appointment.BillingDecision.UNDECIDED,
        billing_account=appointment.billing_account,
        source_appointment=appointment,
        series=appointment.series,
        program_block=appointment.program_block,
        sequence_number=appointment.sequence_number,
        admin_note=note,
    )
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
    appointment.specialist_marked_at = timezone.now()
    if note:
        appointment.specialist_note = note
    appointment.save(
        update_fields=[
            "status",
            "attendance_status",
            "specialist_marked_at",
            "specialist_note",
            "updated_at",
        ]
    )
    return appointment


def materialize_series(series_id: int, date_from: datetime, date_to: datetime) -> list[Appointment]:
    """Создаёт занятия по серии в диапазоне дат (используется в следующих этапах)."""
    raise NotImplementedError("materialize_series будет реализован на этапе 2.7")


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
    if appointment.billing_account_id and appointment.billing_account_id != account.id:
        existing = LedgerEntry.objects.filter(appointment=appointment).exclude(account=account)
        for entry in existing:
            entry.appointment = None
            entry.save(update_fields=["appointment", "updated_at"])

    entry, _ = LedgerEntry.objects.update_or_create(
        appointment=appointment,
        account=account,
        defaults={
            "entry_type": LedgerEntry.EntryType.DEBIT,
            "amount": amount,
            "reason": reason,
            "created_by": actor,
        },
    )
    return entry


def bulk_unlink_ledger(appointments: Iterable[Appointment]) -> int:
    """Снимает привязку занятие→ledger при массовой отмене (для безопасного переноса)."""
    count = 0
    for appt in appointments:
        LedgerEntry.objects.filter(appointment=appt).update(appointment=None)
        count += 1
    return count
