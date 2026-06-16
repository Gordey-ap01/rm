"""Бизнес-логика расписания: проверка пересечений, поиск слотов, массовый перенос."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from operations.forms import build_local_datetime
from operations.models import (
    ACTIVE_APPOINTMENT_STATUSES,
    Appointment,
    AppointmentConfirmation,
    StaffAvailability,
    StaffMember,
    TimeOffRequest,
)


@dataclass(frozen=True)
class ConflictReport:
    """Результат проверки пересечений."""

    child_conflict: Appointment | None
    staff_conflict: Appointment | None
    room_conflict: Appointment | None
    room_capacity: int = 1
    room_occupancy: int = 0

    @property
    def has_conflict(self) -> bool:
        return any((self.child_conflict, self.staff_conflict, self.room_conflict))

    def human_messages(self) -> list[str]:
        out: list[str] = []
        if self.child_conflict:
            out.append(
                f"у получателя уже есть занятие в это время "
                f"({self.child_conflict.starts_at:%d.%m %H:%M})"
            )
        if self.staff_conflict:
            out.append("специалист уже занят в это время")
        if self.room_conflict:
            out.append("кабинет уже занят в это время")
        return out


def find_overlaps(
    starts_at: datetime,
    ends_at: datetime,
    *,
    child: Any = None,
    staff_member: Any = None,
    room: Any = None,
    exclude_pk: int | None = None,
) -> ConflictReport:
    """Ищет пересекающиеся активные занятия (по ребёнку, специалисту или кабинету)."""
    qs = (
        Appointment.objects.filter(
            status__in=ACTIVE_APPOINTMENT_STATUSES,
            starts_at__lt=ends_at,
            ends_at__gt=starts_at,
        )
        .select_related("child", "staff_member", "service", "room")
        .order_by("starts_at")
    )
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    room_qs = qs.filter(room=room) if room else qs.none()
    room_occupancy = room_qs.count() if room else 0
    room_capacity = max(getattr(room, "capacity", 1) or 1, 1)
    return ConflictReport(
        child_conflict=qs.filter(child=child).first() if child else None,
        staff_conflict=qs.filter(staff_member=staff_member).first() if staff_member else None,
        room_conflict=room_qs.first() if room and room_occupancy >= room_capacity else None,
        room_capacity=room_capacity,
        room_occupancy=room_occupancy,
    )


def is_within_availability(
    staff_member: StaffMember | None,
    starts_at: datetime | None,
    ends_at: datetime | None,
) -> str:
    """Возвращает ``""`` если время попадает в доступность, иначе — человекочитаемую причину.

    Учитывает:
    - активные ``TimeOffRequest`` (одобренные);
    - ``StaffAvailability`` (если есть) или базовое окно 09:00–18:00 (fallback).
    """
    if not staff_member or not starts_at or not ends_at:
        return ""

    local_start = timezone.localtime(starts_at)
    local_end = timezone.localtime(ends_at)
    day = local_start.date()
    if local_end.date() != day:
        return "занятие должно помещаться в один рабочий день"

    if TimeOffRequest.objects.filter(
        staff_member=staff_member,
        status=TimeOffRequest.Status.APPROVED,
        starts_on__lte=day,
        ends_on__gte=day,
    ).exists():
        return "у специалиста согласован отпуск/отгул на эту дату"

    windows = list(
        StaffAvailability.objects.filter(
            staff_member=staff_member,
            weekday=day.weekday(),
            is_active=True,
        ).order_by("starts_at")
    )
    start_time = local_start.time().replace(second=0, microsecond=0)
    end_time = local_end.time().replace(second=0, microsecond=0)

    if not windows:
        if time(9, 0) <= start_time and end_time <= time(18, 0):
            return ""
        return "время вне базового рабочего окна 09:00-18:00"

    if any(w.starts_at <= start_time and end_time <= w.ends_at for w in windows):
        return ""
    return "время вне рабочего графика специалиста"


def find_free_slots(
    day: date,
    duration_minutes: int,
    *,
    staff_member: StaffMember | None = None,
    child: Any = None,
    room: Any = None,
    slot_step_minutes: int = 30,
    start_hour: int = 9,
    end_hour: int = 18,
) -> list[datetime]:
    """Возвращает список свободных ``starts_at`` (datetime) на день.

    Алгоритм:
    - генерируем слоты с шагом ``slot_step_minutes`` от ``start_hour`` до ``end_hour - duration``;
    - исключаем слоты, которые пересекаются с любым активным занятием по (child, staff, room);
    - исключаем слоты вне окна доступности (если указан ``staff_member``).
    """
    if day < timezone.localdate():
        return []

    starts: list[datetime] = []
    step = timedelta(minutes=slot_step_minutes)
    window = timedelta(minutes=duration_minutes)
    cursor = build_local_datetime(day, time(start_hour, 0))
    window_end = build_local_datetime(day, time(end_hour, 0))

    while cursor + window <= window_end:
        slot_end = cursor + window
        if not find_overlaps(cursor, slot_end, child=child, staff_member=staff_member, room=room).has_conflict:
            if not is_within_availability(staff_member, cursor, slot_end):
                pass
            else:
                starts.append(cursor)
        cursor += step
    return starts


@dataclass
class MassRescheduleResult:
    """Результат массового переноса при отсутствии специалиста."""

    staff: StaffMember
    date_from: date
    date_to: date
    cancelled: list[Appointment]
    confirmations: list[AppointmentConfirmation]
    suggested_slots_by_appointment: dict[int, list[datetime]]


@transaction.atomic
def mass_reschedule(
    staff: StaffMember,
    *,
    date_from: date,
    date_to: date,
    reason: str,
    actor: Any = None,
    same_category_only: bool = True,
    max_suggestions_per_appointment: int = 5,
) -> MassRescheduleResult:
    """Отменяет все активные занятия специалиста в диапазоне и предлагает слоты.

    Шаги:
    1. Все ``Appointment`` со статусом ``proposed/confirmed/reserved`` в диапазоне → ``cancelled``;
       (завершённые/уже отменённые не трогаем).
    2. Для каждого отменённого — создаём ``AppointmentConfirmation`` представителю
       с шаблоном "Ваше занятие отменено, предлагаем слоты: ..."
    3. Ищем свободные слоты у других активных специалистов той же ``Service.Category``
       (если ``same_category_only``) на ближайшие 14 дней.
    """
    if date_to < date_from:
        raise ValueError("Дата окончания не может быть раньше даты начала.")

    tz = timezone.get_current_timezone()
    start_dt = timezone.make_aware(datetime.combine(date_from, time.min), tz)
    end_dt = timezone.make_aware(datetime.combine(date_to, time.max), tz)

    appointments = list(
        Appointment.objects.filter(
            staff_member=staff,
            starts_at__gte=start_dt,
            starts_at__lte=end_dt,
            status__in=[Appointment.Status.PROPOSED, Appointment.Status.CONFIRMED, Appointment.Status.RESERVED],
        ).select_related("child", "service", "child__primary_parent")
    )

    cancelled: list[Appointment] = []
    confirmations: list[AppointmentConfirmation] = []
    for appt in appointments:
        appt.status = Appointment.Status.CANCELLED
        appt.admin_note = "\n".join(
            part for part in [appt.admin_note, f"Массовая отмена ({reason})."] if part
        )
        appt.save(update_fields=["status", "admin_note", "updated_at"])
        cancelled.append(appt)

        representative = appt.child.primary_parent
        if representative and representative.email:
            local_start = timezone.localtime(appt.starts_at)
            confirmation = AppointmentConfirmation.objects.create(
                appointment=appt,
                target_type=AppointmentConfirmation.TargetType.REPRESENTATIVE,
                representative=representative,
                email=representative.email,
                subject=f"Занятие {local_start:%d.%m.%Y %H:%M} отменено",
                message=(
                    f"Занятие {appt.child} у {staff.full_name} "
                    f"{local_start:%d.%m.%Y в %H:%M} отменено по причине: {reason}. "
                    "Администратор свяжется с вами для согласования нового времени."
                ),
                sent_by=actor,
            )
            confirmations.append(confirmation)

    suggestions: dict[int, list[datetime]] = {}
    if same_category_only and cancelled:
        peer_staff = list(
            StaffMember.objects.filter(
                status=StaffMember.Status.ACTIVE,
            )
            .exclude(pk=staff.pk)
            .order_by("full_name")
        )
        for appt in cancelled:
            slots: list[datetime] = []
            for peer in peer_staff:
                slots.extend(
                    find_free_slots(
                        date_from,
                        appt.duration_minutes,
                        staff_member=peer,
                        child=appt.child,
                        room=None,
                    )[: max_suggestions_per_appointment - len(slots)]
                )
                if len(slots) >= max_suggestions_per_appointment:
                    break
            suggestions[appt.pk] = slots

    return MassRescheduleResult(
        staff=staff,
        date_from=date_from,
        date_to=date_to,
        cancelled=cancelled,
        confirmations=confirmations,
        suggested_slots_by_appointment=suggestions,
    )


def filter_staff_by_category(
    category: str,
    *,
    exclude: StaffMember | None = None,
) -> QuerySet[StaffMember]:
    qs = StaffMember.objects.filter(status=StaffMember.Status.ACTIVE)
    if exclude:
        qs = qs.exclude(pk=exclude.pk)
    if category:
        from operations.models import Service

        # специалисты, которые вели хотя бы одно занятие этой категории за последние 90 дней
        recent = Appointment.objects.filter(
            service__category=category,
            starts_at__gte=timezone.now() - timedelta(days=90),
        ).values_list("staff_member_id", flat=True)
        qs = qs.filter(Q(pk__in=set(recent)) | Q(specializations__icontains=Service.Category.labels[0]))
    return qs.order_by("full_name")
